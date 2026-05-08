"""Agent-Transport + LiveKit adapter benchmark server.

Uses the full LiveKit agents framework on top of agent-transport's Rust
transport layer. This tests the real production code path:

  Plivo WebSocket → Rust transport → AudioStreamInput → AgentSession
    (VAD → STT → LLM → TTS) → AudioStreamOutput → Rust pacing → Plivo

Compared to agent_transport_server.py (which manually wires the pipeline),
this adds the overhead of:
  - AudioStreamServer event loop + session lifecycle
  - AgentSession orchestration (turn detection, interruption handling)
  - AudioStreamInput / AudioStreamOutput adapters
  - TransportRoom facade

Usage:
    python -m load_test.servers.agent_transport_livekit_server start
    python -m load_test.servers.agent_transport_livekit_server dev
"""

import asyncio
import concurrent.futures
import os
import sys

from loguru import logger

from livekit.agents import AgentSession, Agent, TurnHandlingOptions
from livekit.plugins import silero

from agent_transport.audio_stream.livekit import (
    AudioStreamServer,
    JobContext,
    JobProcess,
)

from load_test.servers.livekit_plugins import BenchLLM, BenchSTT, BenchTTS

SAMPLE_RATE = 8000
WS_PORT = int(os.getenv("WS_PORT", "8083"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8184"))

# VAD is disabled by default for equal footing with the other benchmark servers.
# BenchSTT emits transcripts on a frame-count timer, so AgentSession doesn't need
# VAD-detected turn boundaries. Set ENABLE_VAD=true to measure VAD pressure.
ENABLE_VAD = os.getenv("ENABLE_VAD", "false").lower() == "true"
# ML turn detector (livekit-plugins-turn-detector multilingual ONNX). When on,
# replaces the STT-based turn handling with semantic end-of-utterance detection.
ENABLE_TURN_DETECTOR = os.getenv("ENABLE_TURN_DETECTOR", "false").lower() == "true"


class BenchAgent(Agent):
    """Minimal agent for benchmarking — no custom logic, just instructions."""

    def __init__(self) -> None:
        super().__init__(
            instructions="You are a helpful voice assistant for benchmarking.",
        )


server_kwargs = dict(
    listen_addr=f"0.0.0.0:{WS_PORT}",
    plivo_auth_id="",
    plivo_auth_token="",
)
if ENABLE_VAD:
    # Rust Silero VAD: inference in Rust, no Python ONNX per session.
    server_kwargs.update(
        vad=True,
        vad_threshold=0.5,
        vad_min_speech_ms=250,
        vad_min_silence_ms=500,
        vad_speech_pad_ms=100,
    )

server = AudioStreamServer(**server_kwargs)


def prewarm(proc: JobProcess) -> None:
    """Setup hook. With Rust VAD enabled on the endpoint, AgentSession doesn't need
    its own Python VAD — that would double VAD cost. BenchSTT emits transcripts on
    a frame-count timer so AgentSession can drive LLM/TTS without VAD flushes.
    """
    if ENABLE_VAD:
        logger.info("Rust Silero VAD enabled on endpoint (no Python VAD in AgentSession)")
    else:
        logger.info("VAD disabled (ENABLE_VAD=false)")
    proc.userdata["vad"] = None

    # Pre-download the multilingual EOU model files at process start so the
    # first session doesn't pay the HuggingFace download cost. The actual
    # MultilingualModel instance is created inside the entrypoint (it needs a
    # JobContext to bind to the InferenceProcExecutor).
    if ENABLE_TURN_DETECTOR:
        logger.info("Pre-downloading multilingual turn-detector model files...")
        try:
            from livekit.plugins.turn_detector.multilingual import (
                _EUORunnerMultilingual,
            )
            _EUORunnerMultilingual._download_files()
            logger.info("Multilingual turn-detector files ready")
        except Exception as e:
            logger.warning(f"Turn-detector predownload failed: {e}")


server.setup_fnc = prewarm


@server.audio_stream_session()
async def entrypoint(ctx: JobContext) -> None:
    """Handle one voice agent session via the LiveKit agents framework."""
    import time
    sid = ctx.session_id
    t0 = time.perf_counter()

    def _t(label: str) -> None:
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(f"[LK-TRACE {sid}] +{elapsed:>8.1f}ms {label}")

    _t("entrypoint_start")

    # Match AT pipecat bench: VAD off, no turn-detector model, and strip the
    # LiveKit-specific orchestration that's on by default but has no pipecat
    # counterpart — otherwise the comparison measures those features, not the
    # framework's session density.
    session_kwargs = {
        "stt": BenchSTT(),
        "llm": BenchLLM(),
        "tts": BenchTTS(),
        "turn_handling": TurnHandlingOptions(turn_detection="stt"),
        # preemptive_generation + aec_warmup + user_away_timeout all spawn
        # per-session background tasks/coroutines. Pipecat has no equivalents
        # in its bench config, so turning these off levels the playing field.
        "preemptive_generation": False,
        "aec_warmup_duration": None,
        "user_away_timeout": None,
    }
    if ctx.proc.userdata.get("vad") is not None:
        session_kwargs["vad"] = ctx.proc.userdata["vad"]
    if ENABLE_TURN_DETECTOR:
        # MultilingualModel binds to the JobContext.inference_executor, so it
        # has to be constructed inside the entrypoint (not in prewarm).
        from livekit.plugins.turn_detector.multilingual import MultilingualModel
        session_kwargs["turn_detection"] = MultilingualModel()
        session_kwargs.pop("turn_handling", None)

    _t("before_AgentSession()")
    session = AgentSession(**session_kwargs)
    _t("after_AgentSession()")

    # Auto-wire: replaces session.input.audio / session.output.audio with
    # AudioStreamInput / AudioStreamOutput backed by Rust transport.
    _t("before_ctx.session=session")
    ctx.session = session
    _t("after_ctx.session=session")

    _t("before_session.start()")
    await session.start(agent=BenchAgent(), room=ctx.room)
    _t("after_session.start()")


def _expand_default_executor():
    """The AT LiveKit adapter's AudioStreamInput calls `recv_audio_bytes_blocking`
    via `loop.run_in_executor(None, ...)`. The default executor pool is tiny
    (min(32, cpu+4) = 8 on 4-CPU containers), which serializes all audio-input
    threads across N concurrent sessions. Blow it out so per-session blocking
    recv calls don't queue.

    Capacity budget: each recv blocks ~20ms, so 1 thread sustains ~50 recvs/sec.
    Need N_sessions × 50 fps = capacity; sizing the pool at 2-3× max concurrency
    gives headroom for occasional longer blocks.
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
