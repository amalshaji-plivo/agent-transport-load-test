"""livekit-gateway benchmark server.

Wires stock `livekit-agents` to Plivo audio-stream via the
`livekit-gateway` FFI shim + standalone Rust gateway. There is no
agent-transport adapter in this path — that's the whole point of the
A/B against `agent_transport_livekit_server.py`:

  Plivo WS  →  livekit-gateway (Rust)  →  liblivekit_ffi  ↔  worker proto
            →  livekit-agents AgentSession (VAD → STT → LLM → TTS)
            →  liblivekit_ffi  →  gateway  →  Plivo WS

Ports (unique vs the other benchmark servers in this repo):
  PLIVO_WS_ADDR=0.0.0.0:8084            Plivo-facing port
  LIVEKIT_AGENT_WS_ADDR=0.0.0.0:7884    Agent worker registration port

Both can be overridden via env vars before launching.

The STT/LLM/TTS plugins (`BenchSTT`, `BenchLLM`, `BenchTTS`) talk to the
mock services (`load_test.servers.mock_services`) over the wire so the
agent pipeline sees realistic streaming back-pressure, not inline sleeps.

Usage:
    python -m load_test.servers.livekit_gateway_server start
    python -m load_test.servers.livekit_gateway_server dev
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys

from loguru import logger


# ── Wire the gateway ports BEFORE importing livekit_gateway / livekit ────────
# livekit_gateway/__init__.py reads PLIVO_WS_ADDR + LIVEKIT_AGENT_WS_ADDR at
# import time to decide whether to auto-spawn its bundled gateway binary and
# to set LIVEKIT_URL for stock livekit-agents. If we set these AFTER import,
# the gateway either spawns on the wrong port or refuses to spawn because the
# default port (7880) is already taken by another benchmark server.
os.environ.setdefault("PLIVO_WS_ADDR", "0.0.0.0:8084")
os.environ.setdefault("LIVEKIT_AGENT_WS_ADDR", "0.0.0.0:7884")

_agent_port = os.environ["LIVEKIT_AGENT_WS_ADDR"].rsplit(":", 1)[-1]
os.environ.setdefault("LIVEKIT_URL", f"ws://localhost:{_agent_port}")
# Loopback gateway doesn't verify these — provide harmless defaults so
# livekit-agents doesn't fail an "API_KEY missing" assertion before we
# even reach the entrypoint.
os.environ.setdefault("LIVEKIT_API_KEY", "bench")
os.environ.setdefault("LIVEKIT_API_SECRET", "bench")

# The gateway's [http] section advertises a `public_host` in the <Stream>
# XML reply — irrelevant to the benchmark client (we connect directly to
# the WS) but the gateway logs warnings if it's the placeholder value.
os.environ.setdefault("PLIVO_PUBLIC_HOST", f"localhost:{os.environ['PLIVO_WS_ADDR'].split(':')[-1]}")

# Critical: import livekit_gateway BEFORE any `livekit.*` so its
# __init__.py sets LIVEKIT_LIB_PATH to the bundled `liblivekit_ffi`
# (the Plivo-backed drop-in) instead of the upstream LiveKit one.
import livekit_gateway  # noqa: F401, E402

from livekit.agents import (  # noqa: E402
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    cli,
)

from load_test.servers._metrics_hook import attach as attach_metrics_hook  # noqa: E402
from load_test.servers.livekit_plugins import BenchLLM, BenchSTT, BenchTTS  # noqa: E402

ENABLE_VAD = os.getenv("ENABLE_VAD", "false").lower() == "true"
ENABLE_TURN_DETECTOR = os.getenv("ENABLE_TURN_DETECTOR", "false").lower() == "true"


class BenchAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="You are a helpful voice assistant for benchmarking.",
        )


def prewarm(proc: JobProcess) -> None:
    """Match the livekit-python prewarm so the comparison isolates transport,
    not plugin-load latency."""
    if ENABLE_VAD:
        from livekit.plugins import silero
        logger.info("Pre-loading Silero VAD model (8 kHz)...")
        proc.userdata["vad"] = silero.VAD.load(sample_rate=8000)
    else:
        proc.userdata["vad"] = None
        logger.info("VAD disabled (ENABLE_VAD=false)")

    if ENABLE_TURN_DETECTOR:
        try:
            from livekit.plugins.turn_detector.multilingual import (
                _EUORunnerMultilingual,
            )
            _EUORunnerMultilingual._download_files()
            logger.info("Multilingual EOU model files ready")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"EOU predownload failed: {e}")


server = AgentServer(
    num_idle_processes=int(os.getenv("NUM_IDLE_PROCESSES", "2")),
    # AgentServer's introspection HTTP normally binds :8081. Pick a non-
    # default port so the livekit-python server can run on the same host
    # without an "address already in use" collision.
    port=int(os.getenv("AGENT_SERVER_HTTP_PORT", "8181")),
)
server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    """Handle one voice agent session via stock livekit-agents."""
    session_kwargs: dict = {
        "stt": BenchSTT(),
        "llm": BenchLLM(),
        "tts": BenchTTS(),
        "turn_handling": TurnHandlingOptions(turn_detection="stt"),
        # These features have no pipecat counterpart; agent-transport-livekit
        # disables them too so the apples-to-apples comparison stays fair.
        "preemptive_generation": False,
        "aec_warmup_duration": None,
        "user_away_timeout": None,
    }
    if ctx.proc.userdata.get("vad") is not None:
        session_kwargs["vad"] = ctx.proc.userdata["vad"]

    if ENABLE_TURN_DETECTOR:
        from livekit.plugins.turn_detector.multilingual import MultilingualModel
        session_kwargs["turn_detection"] = MultilingualModel()
        session_kwargs.pop("turn_handling", None)

    session = AgentSession(**session_kwargs)
    attach_metrics_hook(session)
    await ctx.connect()
    await session.start(agent=BenchAgent(), room=ctx.room)


def _expand_default_executor() -> None:
    """livekit-agents' FFI path uses `loop.run_in_executor(None, …)` for the
    blocking `recv_audio_bytes_blocking` call into our liblivekit_ffi. The
    default executor pool is `min(32, cpu+4)` which is 8 on 4-vCPU boxes —
    that serializes audio-input threads across all concurrent sessions and
    is the single biggest scaling cliff observed on the AT-livekit path.

    Mirror agent-transport-livekit's fix so the comparison isn't biased by
    one side having more recv-thread headroom than the other.
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
    try:
        import uvloop
        uvloop.install()
        logger.info("uvloop installed as default event loop policy")
    except ImportError:
        logger.warning("uvloop not available — falling back to stdlib asyncio")

    _expand_default_executor()
    cli.run_app(server)
