"""Agent-Transport + Pipecat benchmark server — uses the proper AT pipecat framework.

Production code path:
  Plivo WebSocket → Rust transport (codec, pacing) → Pipecat Pipeline
    (optional VAD → STT → aggregator → LLM → TTS) → Rust transport → Plivo

Pattern matches: examples/pipecat/audio_stream_agent.py

The LLM, STT, TTS here are inline bench implementations with timing matched to
the mock_services values. No external HTTP/WS dependencies.

Usage:
    python -m load_test.servers.agent_transport_server start
    python -m load_test.servers.agent_transport_server dev
"""

import asyncio
import concurrent.futures
import os

from loguru import logger

from agent_transport.audio_stream.pipecat import (
    PlivoFrameSerializer,
    WebsocketServerTransport,
)

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from load_test.servers.pipecat_plugins import (
    BenchPipecatLLM,
    BenchPipecatSTT,
    BenchPipecatTTS,
)

WS_PORT = os.getenv("WS_PORT", "8081")

# VAD mode: "off" | "rust" | "python"
#   off    - no transport VAD; BenchPipecatSTT falls back to RMS-based turn detection
#   rust   - Rust Silero VAD on the endpoint (shared session pool in Rust)
#   python - Python Silero VADProcessor in the pipeline (one analyzer per session)
#
# For back-compat: ENABLE_VAD=true with no VAD_BACKEND defaults to "rust".
VAD_BACKEND = os.getenv("VAD_BACKEND", "").lower()
if not VAD_BACKEND:
    VAD_BACKEND = "rust" if os.getenv("ENABLE_VAD", "false").lower() == "true" else "off"

VAD_POOL_SIZE = int(os.getenv("VAD_POOL_SIZE", "8"))

# Smart-turn ML end-of-utterance model. Shared across sessions; loaded once.
ENABLE_TURN_DETECTOR = os.getenv("ENABLE_TURN_DETECTOR", "false").lower() == "true"


def _new_vad_analyzer() -> SileroVADAnalyzer:
    vad = SileroVADAnalyzer(sample_rate=8000)
    vad.set_sample_rate(8000)
    return vad


_TURN_ANALYZER = None
if ENABLE_TURN_DETECTOR:
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
        LocalSmartTurnAnalyzerV3,
    )
    logger.info("Loading smart-turn-v3 model (shared across sessions)...")
    _TURN_ANALYZER = LocalSmartTurnAnalyzerV3()
    logger.info("smart-turn-v3 ready")


def _user_aggregator_params() -> LLMUserAggregatorParams:
    if _TURN_ANALYZER is not None:
        # Smart-turn-v3 needs VAD frames flowing through the aggregator's
        # VADController, so the analyzer is configured here (per-session).
        return LLMUserAggregatorParams(
            vad_analyzer=_new_vad_analyzer(),
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=_TURN_ANALYZER)],
            ),
        )
    return LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.3)],
        ),
    )


serializer_kwargs = dict(
    auth_id="",
    auth_token="",
    listen_addr=f"0.0.0.0:{WS_PORT}",
)
if VAD_BACKEND == "rust":
    # Rust Silero VAD: inference in Rust, shared session pool sized by VAD_POOL_SIZE.
    serializer_kwargs.update(
        vad=True,
        vad_threshold=0.5,
        vad_min_speech_ms=250,
        vad_min_silence_ms=500,
        vad_speech_pad_ms=100,
        vad_pool_size=VAD_POOL_SIZE,
    )
serializer = PlivoFrameSerializer(**serializer_kwargs)
server = WebsocketServerTransport(serializer=serializer)


@server.setup()
def prewarm():
    """Prewarm hook — announce the mode and prime Python VAD model loading."""
    if VAD_BACKEND == "python":
        logger.info("VAD_BACKEND=python — per-session VADProcessor with fresh SileroVADAnalyzer")
        _new_vad_analyzer()
    elif VAD_BACKEND == "rust":
        logger.info(f"VAD_BACKEND=rust — Rust VAD on endpoint, pool_size={VAD_POOL_SIZE}")
    else:
        logger.info("VAD_BACKEND=off — no VAD")
    return {}


@server.handler()
async def run_bot(transport, userdata):
    """Handle one voice agent session via pipecat Pipeline."""
    stt = BenchPipecatSTT()
    llm = BenchPipecatLLM()
    tts = BenchPipecatTTS()

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=_user_aggregator_params(),
    )

    processors = [transport.input()]
    # If smart-turn is on, the user aggregator owns VAD — don't double-VAD.
    if VAD_BACKEND == "python" and _TURN_ANALYZER is None:
        processors.append(VADProcessor(vad_analyzer=_new_vad_analyzer()))
    processors.extend([
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
    ])

    pipeline = Pipeline(processors)

    task = PipelineTask(pipeline, params=PipelineParams(
        audio_in_sample_rate=8000,
        allow_interruptions=True,
    ))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport):
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)


def _expand_default_executor():
    """Same problem as the LiveKit server: the AT pipecat adapter's audio forwarder
    calls `recv_audio_bytes_blocking` via `loop.run_in_executor(None, ...)`. On a
    4-CPU container Python's default pool is `min(32, cpu+4) = 8` threads — all
    stuck in 20ms blocking recvs at ~50 concurrent sessions, serializing audio
    flow and causing 49-second first-frame latency at c=25.

    Bumping the default executor to 256 threads gives headroom for 100+ sessions.
    """
    max_workers = int(os.getenv("AUDIO_RECV_THREADS", "256"))
    logger.info(f"Expanding default ThreadPoolExecutor to {max_workers} workers")
    loop = asyncio.new_event_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="audio-recv",
    ))
    asyncio.set_event_loop(loop)


if __name__ == "__main__":
    # uvloop: C-level asyncio replacement, typically 2-4× faster for
    # network + scheduling heavy workloads. Installs globally so every
    # asyncio.new_event_loop() / asyncio.run() in this process picks it up.
    try:
        import uvloop
        uvloop.install()
        logger.info("uvloop installed as default event loop policy")
    except ImportError:
        logger.warning("uvloop not available — falling back to stdlib asyncio")
    _expand_default_executor()
    server.run()
