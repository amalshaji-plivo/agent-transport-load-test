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
# Python Silero VAD inside the AgentSession (distinct from the Rust endpoint VAD
# enabled by ENABLE_VAD). Auto-used when the turn detector is on and Rust VAD is
# off, since the semantic EOU turn detector refines VAD endpoints and needs VAD
# events to fire. Can also be forced on independently with ENABLE_PY_VAD=true.
ENABLE_PY_VAD = os.getenv("ENABLE_PY_VAD", "false").lower() == "true"

# Register the multilingual EOU inference runner at IMPORT time. AudioStreamServer
# only spins up an InferenceProcExecutor when `_InferenceRunner.registered_runners`
# is non-empty, and it checks that BEFORE the prewarm/setup hook runs. Importing the
# turn detector lazily (inside prewarm/entrypoint) registers the runner too late, so
# ctx.inference_executor stays None and EOU prediction fails with
# "'NoneType' object has no attribute 'do_inference'". Importing here registers
# 'lk_end_of_utterance_multilingual' before server.run(), so the executor is built.
if ENABLE_TURN_DETECTOR:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel  # noqa: F401  (registers EOU runner)

# Endpointing delays for the EOU turn detector (seconds). Capping max keeps each
# user turn committing within the bench's inter-utterance silence window even
# when the model is uncertain on synthetic audio — the EOU inference still runs
# every turn (that cost is the point), we just don't wait the full default.
MIN_ENDPOINTING_DELAY = float(os.getenv("MIN_ENDPOINTING_DELAY", "0.5"))
MAX_ENDPOINTING_DELAY = float(os.getenv("MAX_ENDPOINTING_DELAY", "6.0"))


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
        proc.userdata["vad"] = None
    elif ENABLE_TURN_DETECTOR or ENABLE_PY_VAD:
        # No Rust VAD, but the AgentSession needs VAD events: the semantic EOU
        # turn detector refines VAD endpoints rather than replacing them, so it
        # requires a VAD. Load the LiveKit Silero (Python ONNX) VAD once and
        # share it across sessions via proc.userdata.
        logger.info("Loading LiveKit Silero VAD (Python ONNX) for AgentSession turn detection")
        proc.userdata["vad"] = silero.VAD.load()
    else:
        logger.info("VAD disabled (ENABLE_VAD=false, no turn detector)")
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
        # Commit the user turn within the bench's silence gap; the EOU inference
        # still runs each turn (the load we want to measure).
        session_kwargs["min_endpointing_delay"] = MIN_ENDPOINTING_DELAY
        session_kwargs["max_endpointing_delay"] = MAX_ENDPOINTING_DELAY
        session_kwargs.pop("turn_handling", None)

    _t("before_AgentSession()")
    session = AgentSession(**session_kwargs)
    _t("after_AgentSession()")

    # Per-turn component breakdown (EOU endpointing delay, STT transcription
    # delay, LLM time-to-first-token, TTS time-to-first-byte). Gated on
    # METRICS_LOG so it's off during high-concurrency sweeps. This is the TRUE
    # response-latency source — the bench's send/recv RTT assumes continuous
    # output and mismeasures a sparse speak-then-pause turn cadence.
    if os.getenv("METRICS_LOG", "false").lower() == "true":
        def _on_metrics(ev) -> None:
            m = getattr(ev, "metrics", ev)
            f = {
                a: round(getattr(m, a) * 1000)
                for a in ("end_of_utterance_delay", "transcription_delay", "ttft", "ttfb", "duration", "audio_duration")
                if isinstance(getattr(m, a, None), (int, float))
            }
            logger.info(f"[METRICS {sid}] {type(m).__name__} {f}")
        try:
            session.on("metrics_collected", _on_metrics)
        except Exception as e:  # pragma: no cover
            logger.warning(f"metrics hook registration failed: {e}")

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
