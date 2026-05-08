"""Mock pipecat services for benchmarking.

Timing matches the LiveKit mock plugins so pipeline latency is comparable
across the benchmark targets:

  STT:  streaming audio, final transcript after end-of-turn (~200ms processing)
  LLM:  150ms TTFT, then ~15 tokens at 40ms each — fully inline (no HTTP)
  TTS:  80ms TTFB, then streams ~600ms of audio in 20ms chunks
"""

import asyncio
import io
import math
import struct
import wave
from typing import AsyncGenerator

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

# Timing constants
STT_PROCESSING_DELAY = 0.2
TTS_FIRST_CHUNK_MS = 0.08
TTS_CHUNK_INTERVAL = 0.02
TTS_SAMPLE_RATE = 8000
TTS_NUM_CHANNELS = 1

PHRASES = [
    "hello how are you",
    "I need help with my account",
    "can you transfer me to billing",
    "thank you very much",
    "yes that sounds good",
]

RESPONSES = [
    "Hello! I'd be happy to help you with that. Let me look into your account right away.",
    "Sure, I can transfer you to our billing department. Please hold for just a moment.",
    "Thank you for calling. Is there anything else I can help you with today?",
    "I understand your concern. Let me check the details and get back to you shortly.",
    "That's a great question. The answer is that we offer several options for your needs.",
]

LLM_FIRST_TOKEN_S = 0.15
LLM_TOKEN_INTERVAL_S = 0.04

_FALLBACK_MIN_SPEECH_FRAMES = 8
_FALLBACK_END_SILENCE_FRAMES = 25
_FALLBACK_VOICED_RMS = 700.0


def _pcm_rms(audio: bytes) -> float:
    sample_count = len(audio) // 2
    if sample_count == 0:
        return 0.0
    samples = struct.unpack(f"<{sample_count}h", audio)
    energy = sum(sample * sample for sample in samples) / sample_count
    return energy ** 0.5


class BenchPipecatSTT(SegmentedSTTService):
    """Mock segmented STT driven by speech-stop events.

    When upstream VAD is present we finalize only on VAD stop. For no-VAD paths,
    we fall back to simple RMS-based speech/silence detection so the transcript
    timing still follows utterance boundaries instead of a fixed frame counter.
    """

    def __init__(self) -> None:
        super().__init__(sample_rate=8000, ttfs_p99_latency=0.25)
        self._phrase_idx = 0
        self._saw_upstream_vad = False
        self._fallback_voiced_frames = 0
        self._fallback_silence_frames = 0
        self._fallback_turn_started = False

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

        await asyncio.sleep(STT_PROCESSING_DELAY)
        text = PHRASES[self._phrase_idx % len(PHRASES)]
        self._phrase_idx += 1
        yield TranscriptionFrame(
            text=text,
            user_id="bench-user",
            timestamp="",
            finalized=True,
        )


_SAMPLES_PER_CHUNK = TTS_SAMPLE_RATE // 50
_SINE_CHUNK = struct.pack(
    f"<{_SAMPLES_PER_CHUNK}h",
    *[int(16000 * math.sin(2 * math.pi * 440 * i / TTS_SAMPLE_RATE))
      for i in range(_SAMPLES_PER_CHUNK)]
)


class BenchPipecatTTS(TTSService):
    """Mock TTS that streams audio chunks with realistic timing."""

    def __init__(self) -> None:
        super().__init__(sample_rate=TTS_SAMPLE_RATE)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        yield TTSStartedFrame(context_id=context_id)

        n_chunks = max(10, min(30, len(text) // 2))
        await asyncio.sleep(TTS_FIRST_CHUNK_MS)

        for _ in range(n_chunks):
            yield TTSAudioRawFrame(
                audio=_SINE_CHUNK,
                sample_rate=TTS_SAMPLE_RATE,
                num_channels=TTS_NUM_CHANNELS,
                context_id=context_id,
            )
            await asyncio.sleep(TTS_CHUNK_INTERVAL)

        yield TTSStoppedFrame(context_id=context_id)


class BenchPipecatLLM(LLMService):
    """Mock LLM service that streams canned responses token-by-token."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._response_idx = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self.push_frame(LLMFullResponseStartFrame())
            await self.start_ttfb_metrics()
            await self.start_processing_metrics()
            try:
                await self._stream_canned_response()
            finally:
                await self.stop_processing_metrics()
                await self.stop_ttfb_metrics()
                await self.push_frame(LLMFullResponseEndFrame())
        else:
            await self.push_frame(frame, direction)

    async def _stream_canned_response(self) -> None:
        response = RESPONSES[self._response_idx % len(RESPONSES)]
        self._response_idx += 1

        await asyncio.sleep(LLM_FIRST_TOKEN_S)

        tokens = response.split()
        first = True
        for token in tokens:
            if first:
                await self.stop_ttfb_metrics()
                first = False
            await self.push_frame(LLMTextFrame(token + " "))
            await asyncio.sleep(LLM_TOKEN_INTERVAL_S)
