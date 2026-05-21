"""Mock pipecat services for benchmarking — talk to wire-mock backends.

Each plugin connects to the real STT/LLM/TTS endpoints exposed by
`load_test.servers.mock_services` over real WebSocket / HTTP. The
inline `asyncio.sleep` shims that previously simulated the timing
have been removed: real socket I/O now provides the same back-pressure
the production code would see against Deepgram / OpenAI / etc.

Endpoint timings (set in `mock_services.py`):
  STT:  partials every 200ms, final after 100 frames (~2s) of audio.
  LLM:  150 ms time-to-first-token, ~25 tok/s thereafter.
  TTS:  80 ms time-to-first-chunk, 20 ms between chunks, ~600 ms total.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import wave
from typing import AsyncGenerator

import aiohttp

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService

from load_test.servers.mock_clients import (
    llm_stream_tokens,
    stt_url,
    tts_url,
)

# Constants kept here for documentation / external references; the actual
# timings live in mock_services.py.
TTS_SAMPLE_RATE = 8000
TTS_NUM_CHANNELS = 1

_FALLBACK_MIN_SPEECH_FRAMES = 8
_FALLBACK_END_SILENCE_FRAMES = 25
_FALLBACK_VOICED_RMS = 700.0
_AUDIO_CHUNK_BYTES = 320  # 20 ms PCM16 LE at 8 kHz


def _pcm_rms(audio: bytes) -> float:
    sample_count = len(audio) // 2
    if sample_count == 0:
        return 0.0
    samples = struct.unpack(f"<{sample_count}h", audio)
    energy = sum(sample * sample for sample in samples) / sample_count
    return energy ** 0.5


# ── STT: wire-talking ────────────────────────────────────────────────────────
class BenchPipecatSTT(SegmentedSTTService):
    """Mock segmented STT — sends the buffered turn over WS to the mock STT
    service and waits for the final transcript.

    The VAD-fallback turn detection is unchanged from the inline version;
    only the actual "STT call" is over the wire now.
    """

    def __init__(self) -> None:
        super().__init__(sample_rate=8000, ttfs_p99_latency=0.5)
        self._saw_upstream_vad = False
        self._fallback_voiced_frames = 0
        self._fallback_silence_frames = 0
        self._fallback_turn_started = False
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def cleanup(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            self._saw_upstream_vad = True
        await super().process_frame(frame, direction)

    async def process_audio_frame(self, frame: AudioRawFrame, direction: FrameDirection):
        voiced = False
        if not self._saw_upstream_vad:
            voiced = _pcm_rms(frame.audio) >= _FALLBACK_VOICED_RMS
            if voiced:
                self._fallback_voiced_frames += 1
                self._fallback_silence_frames = 0
                if self._fallback_voiced_frames >= _FALLBACK_MIN_SPEECH_FRAMES:
                    self._user_speaking = True
                    self._fallback_turn_started = True
            elif self._fallback_turn_started:
                self._fallback_silence_frames += 1

        await super().process_audio_frame(frame, direction)

        if (
            not self._saw_upstream_vad
            and self._fallback_turn_started
            and not voiced
            and self._fallback_silence_frames >= _FALLBACK_END_SILENCE_FRAMES
        ):
            self._fallback_turn_started = False
            self._fallback_voiced_frames = 0
            self._fallback_silence_frames = 0
            self._user_speaking = False
            await self._flush_buffered_turn()

    async def _handle_user_started_speaking(self, frame: VADUserStartedSpeakingFrame):
        self._fallback_voiced_frames = 0
        self._fallback_silence_frames = 0
        self._fallback_turn_started = True
        await super()._handle_user_started_speaking(frame)

    async def _handle_user_stopped_speaking(self, frame: VADUserStoppedSpeakingFrame):
        self._fallback_voiced_frames = 0
        self._fallback_silence_frames = 0
        self._fallback_turn_started = False
        await super()._handle_user_stopped_speaking(frame)

    async def _flush_buffered_turn(self) -> None:
        if not self._audio_buffer:
            return

        content = io.BytesIO()
        wav = wave.open(content, "wb")
        wav.setsampwidth(2)
        wav.setnchannels(1)
        wav.setframerate(self.sample_rate)
        wav.writeframes(self._audio_buffer)
        wav.close()
        content.seek(0)

        self._audio_buffer.clear()
        await self.process_generator(self.run_stt(content.read()))

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if not audio:
            return

        session = await self._get_session()
        # WAV header is 44 bytes — skip it so we only ship PCM payload.
        pcm = audio[44:] if audio[:4] == b"RIFF" else audio
        transcript_text = ""

        try:
            async with session.ws_connect(stt_url(), heartbeat=None) as ws:
                # Stream the audio in 20 ms chunks so the mock sees a
                # production-shaped frame cadence rather than one giant blob.
                for i in range(0, len(pcm), _AUDIO_CHUNK_BYTES):
                    await ws.send_bytes(pcm[i : i + _AUDIO_CHUNK_BYTES])
                # Tell the mock the utterance is done.
                await ws.send_str(json.dumps({"type": "Finalize"}))

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            obj = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") != "Results":
                            continue
                        if not obj.get("is_final"):
                            continue
                        alts = obj.get("channel", {}).get("alternatives", [])
                        if alts:
                            transcript_text = alts[0].get("transcript", "")
                        break
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
        except (aiohttp.ClientError, asyncio.TimeoutError):  # noqa: F821
            transcript_text = ""

        if transcript_text:
            yield TranscriptionFrame(
                text=transcript_text,
                user_id="bench-user",
                timestamp="",
                finalized=True,
            )


# ── TTS: wire-talking ────────────────────────────────────────────────────────
class BenchPipecatTTS(TTSService):
    """Mock TTS — opens a WS to the mock TTS service per phrase and streams
    audio chunks back.
    """

    def __init__(self) -> None:
        super().__init__(sample_rate=TTS_SAMPLE_RATE)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def cleanup(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        await super().cleanup()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        yield TTSStartedFrame(context_id=context_id)

        session = await self._get_session()
        try:
            async with session.ws_connect(tts_url(), heartbeat=None) as ws:
                await ws.send_str(
                    json.dumps({"text": text, "sample_rate": TTS_SAMPLE_RATE})
                )
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        yield TTSAudioRawFrame(
                            audio=msg.data,
                            sample_rate=TTS_SAMPLE_RATE,
                            num_channels=TTS_NUM_CHANNELS,
                            context_id=context_id,
                        )
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            obj = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("event") == "done":
                            break
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
        except (aiohttp.ClientError, asyncio.TimeoutError):  # noqa: F821
            pass

        yield TTSStoppedFrame(context_id=context_id)


# ── LLM: wire-talking ────────────────────────────────────────────────────────
class BenchPipecatLLM(LLMService):
    """Mock LLM — POSTs to the mock /v1/chat/completions endpoint and
    streams content tokens as LLMTextFrame.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def cleanup(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self.push_frame(LLMFullResponseStartFrame())
            await self.start_ttfb_metrics()
            await self.start_processing_metrics()
            try:
                await self._stream_response(frame)
            finally:
                await self.stop_processing_metrics()
                await self.stop_ttfb_metrics()
                await self.push_frame(LLMFullResponseEndFrame())
        else:
            await self.push_frame(frame, direction)

    async def _stream_response(self, frame: LLMContextFrame) -> None:
        ctx = frame.context
        messages: list[dict] = []
        # pipecat's LLMContext exposes `.messages` (list of dicts already in
        # OpenAI shape). Fall back to .get_messages() if .messages is absent.
        if hasattr(ctx, "messages") and isinstance(ctx.messages, list):
            for m in ctx.messages:
                if isinstance(m, dict):
                    messages.append({
                        "role": m.get("role", "user"),
                        "content": str(m.get("content", "")),
                    })
        elif hasattr(ctx, "get_messages"):
            for m in ctx.get_messages():
                messages.append({
                    "role": getattr(m, "role", "user"),
                    "content": str(getattr(m, "content", "")),
                })
        # Ensure at least one user message exists (some pipecat flows defer
        # context population until after the first user turn).
        if not messages:
            messages = [{"role": "user", "content": "hello"}]

        first = True
        session = await self._get_session()
        async for token in llm_stream_tokens(messages, session=session):
            if first:
                await self.stop_ttfb_metrics()
                first = False
            await self.push_frame(LLMTextFrame(token))
