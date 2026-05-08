"""Direct Pipecat benchmark server — proper pipecat framework, no agent-transport.

Uses pipecat's official telephony pattern:
  FastAPI → FastAPIWebsocketTransport (per connection) → Pipecat Pipeline
    (optional VAD → STT → aggregator → LLM → TTS) → FastAPIWebsocketTransport → Plivo

This mirrors pipecat.runner.utils.create_telephony_transport() but runs inline
in this process instead of via the runner CLI. Each incoming WebSocket connection
gets a fresh transport + pipeline, which is how real Plivo+pipecat deployments work.

Usage:
    python -m load_test.servers.direct_pipecat_server --port 8080
"""

import argparse
import asyncio
import json
import os
import sys

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger

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
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from load_test.servers.pipecat_plugins import (
    BenchPipecatLLM,
    BenchPipecatSTT,
    BenchPipecatTTS,
)

SAMPLE_RATE = 8000

# VAD is disabled by default — see README / plan. Set ENABLE_VAD=true to measure
# "VAD pressure" scenarios.
ENABLE_VAD = os.getenv("ENABLE_VAD", "false").lower() == "true"
# Smart-turn ML end-of-utterance model (Whisper feature extractor + ONNX
# classifier on the last ~8 s of audio). Single shared analyzer instance.
ENABLE_TURN_DETECTOR = os.getenv("ENABLE_TURN_DETECTOR", "false").lower() == "true"


def _new_vad_analyzer() -> SileroVADAnalyzer:
    vad = SileroVADAnalyzer(sample_rate=SAMPLE_RATE)
    vad.set_sample_rate(SAMPLE_RATE)
    return vad


if ENABLE_VAD:
    logger.info("Prewarming Silero VAD model cache for per-session analyzers...")
    _new_vad_analyzer()
else:
    logger.info("VAD disabled (ENABLE_VAD=false)")


_TURN_ANALYZER = None
if ENABLE_TURN_DETECTOR:
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
        LocalSmartTurnAnalyzerV3,
    )
    logger.info("Loading smart-turn-v3 model (shared across sessions)...")
    _TURN_ANALYZER = LocalSmartTurnAnalyzerV3()
    logger.info("smart-turn-v3 ready")
else:
    logger.info("Turn detector disabled (ENABLE_TURN_DETECTOR=false)")


def _user_aggregator_params() -> LLMUserAggregatorParams:
    if _TURN_ANALYZER is not None:
        # Smart-turn-v3 reads VADUserStartedSpeakingFrame /
        # VADUserStoppedSpeakingFrame from the aggregator's VADController, so
        # the analyzer has to live inside the aggregator (not in a separate
        # VADProcessor upstream). One analyzer per session — Silero state is
        # not safe to share across sessions.
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


app = FastAPI()


@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Handle one Plivo WebSocket session via pipecat Pipeline."""
    await websocket.accept()

    # Read the Plivo 'start' event to extract stream_id + call_id.
    # Pipecat's PlivoFrameSerializer requires these at construction.
    stream_id = None
    call_id = None
    try:
        while stream_id is None:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("event") == "start":
                start = msg.get("start", {})
                stream_id = start.get("streamId", "unknown")
                call_id = start.get("callId", "unknown")
    except WebSocketDisconnect:
        return

    serializer = PlivoFrameSerializer(
        stream_id=stream_id,
        call_id=call_id,
        params=PlivoFrameSerializer.InputParams(auto_hang_up=False),
    )
    params = FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=SAMPLE_RATE,
        audio_out_sample_rate=SAMPLE_RATE,
        add_wav_header=False,
        serializer=serializer,
    )
    transport = FastAPIWebsocketTransport(websocket=websocket, params=params)

    stt = BenchPipecatSTT()
    llm = BenchPipecatLLM()
    tts = BenchPipecatTTS()

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=_user_aggregator_params(),
    )

    processors = [transport.input()]
    # When the smart-turn analyzer is active, the VAD lives inside the user
    # aggregator (see _user_aggregator_params) — adding a VADProcessor here
    # would double-VAD and the aggregator's TurnController would never see
    # the VAD frames it expects.
    if ENABLE_VAD and _TURN_ANALYZER is None:
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
        audio_in_sample_rate=SAMPLE_RATE,
        allow_interruptions=True,
    ))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await task.cancel()

    runner = PipelineRunner()
    try:
        await runner.run(task)
    except WebSocketDisconnect:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("WORKERS", "1")),
        help="Number of uvicorn worker processes (matches production plivo-cx-pipecat; prod=8, non-prod=4).",
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info(
        f"Direct pipecat server on port {args.port} "
        f"(FastAPI + Pipeline per connection, workers={args.workers}, loop=uvloop)"
    )
    # Multi-worker requires the app as a string (each worker imports it fresh).
    # loop="uvloop" matches production `plivo-cx-pipecat` launch.
    uvicorn.run(
        "load_test.servers.direct_pipecat_server:app",
        host="0.0.0.0",
        port=args.port,
        log_level="warning",
        workers=args.workers,
        loop="uvloop",
        backlog=100,
    )


if __name__ == "__main__":
    main()
