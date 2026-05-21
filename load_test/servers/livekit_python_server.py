"""Vanilla `livekit-agents` benchmark server (no agent-transport, no gateway).

The agent worker registers with an open-source LiveKit Server (an SFU) and
handles RTC sessions exactly as it would against LiveKit Cloud. This is
the apples-to-apples comparator for `livekit_gateway_server.py`: same
`AgentSession` pipeline, same wire-mock STT/LLM/TTS — only the transport
layer differs (LiveKit RTC vs Plivo audio-stream via livekit-gateway).

Required environment:
  LIVEKIT_URL=ws://localhost:7880   (or whatever the SFU is on)
  LIVEKIT_API_KEY=devkey
  LIVEKIT_API_SECRET=secret         (dev-mode placeholder credentials)

The bench client (`load_test.client.livekit_rtc_client.LivekitRtcClient`)
connects as a participant; the SFU auto-dispatches this agent into the
participant's room.

Usage:
    python -m load_test.servers.livekit_python_server start
    python -m load_test.servers.livekit_python_server dev
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os

from loguru import logger

# These default to a local livekit-server in dev mode. Override via env
# before invoking the script when pointing at a different SFU.
os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "secret")

# Critical: do NOT `import livekit_gateway` here — that would override
# LIVEKIT_LIB_PATH and route audio through the gateway shim instead of
# the upstream LiveKit FFI. We want the SFU path stock.
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    cli,
)

from load_test.servers._metrics_hook import attach as attach_metrics_hook
from load_test.servers.livekit_plugins import BenchLLM, BenchSTT, BenchTTS

ENABLE_VAD = os.getenv("ENABLE_VAD", "false").lower() == "true"
ENABLE_TURN_DETECTOR = os.getenv("ENABLE_TURN_DETECTOR", "false").lower() == "true"


class BenchAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="You are a helpful voice assistant for benchmarking.",
        )


def prewarm(proc: JobProcess) -> None:
    """Match the livekit-gateway server's prewarm so the comparison isolates
    transport-layer cost rather than plugin-load latency."""
    if ENABLE_VAD:
        from livekit.plugins import silero
        logger.info("Pre-loading Silero VAD model...")
        proc.userdata["vad"] = silero.VAD.load()
    else:
        proc.userdata["vad"] = None
        logger.info("VAD disabled (ENABLE_VAD=false)")

    if ENABLE_TURN_DETECTOR:
        # MultilingualModel itself cannot be pre-warmed (its constructor binds
        # to the per-job JobContext.inference_executor). What we *can* prewarm
        # is the on-disk model cache so the first session per worker doesn't
        # pay HuggingFace download cost on the hot path.
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
    # Pick a non-default introspection port so we can run alongside the
    # livekit-gateway server (which would otherwise also bind :8081).
    port=int(os.getenv("AGENT_SERVER_HTTP_PORT", "8281")),
)
server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    """Handle one room via stock livekit-agents."""
    session_kwargs: dict = {
        "stt": BenchSTT(),
        "llm": BenchLLM(),
        "tts": BenchTTS(),
        "turn_handling": TurnHandlingOptions(turn_detection="stt"),
        # Mirror the livekit-gateway server: disable features that pipecat
        # has no counterpart for so neither side gets free wins.
        "preemptive_generation": False,
        "aec_warmup_duration": None,
        "user_away_timeout": None,
    }
    if ctx.proc.userdata.get("vad") is not None:
        session_kwargs["vad"] = ctx.proc.userdata["vad"]

    if ENABLE_TURN_DETECTOR:
        # When the ML turn detector is on, livekit-agents drives turn commit
        # off MultilingualModel's predict() rather than off the STT's
        # END_OF_SPEECH event. We drop turn_handling to avoid double-driving.
        from livekit.plugins.turn_detector.multilingual import MultilingualModel
        session_kwargs["turn_detection"] = MultilingualModel()
        session_kwargs.pop("turn_handling", None)

    session = AgentSession(**session_kwargs)
    attach_metrics_hook(session)
    await ctx.connect()
    await session.start(agent=BenchAgent(), room=ctx.room)


def _expand_default_executor() -> None:
    """livekit-agents uses run_in_executor for some blocking calls; pump up
    the default pool so per-session waits don't queue at high concurrency.

    Same fix the livekit-gateway server applies — kept symmetric so neither
    side starts with a thread-pool disadvantage.
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
