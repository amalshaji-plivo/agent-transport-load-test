"""Mock STT -> LLM -> TTS pipeline with realistic STREAMING behavior.

Real services don't sleep — they stream data over WebSockets, triggering
callbacks on the asyncio event loop. This mock simulates that pressure:

  STT:  fires partial transcript callbacks every ~200ms
  LLM:  fires token callbacks every ~40ms (25 tokens/sec)
  TTS:  fires audio chunk callbacks in bursts as phrases complete

Each callback is tiny CPU work, but the EVENT LOOP SCHEDULING of thousands
of callbacks per second across N sessions is what creates contention.

Two interfaces:
  - AsyncMockPipeline: for the Python server (asyncio callbacks on event loop)
  - SyncMockPipeline: for the Rust server (blocking in thread, no event loop)
"""

import asyncio
import math
import struct
import time
from dataclasses import dataclass
from typing import Generator

# Audio parameters
SAMPLE_RATE = 8000
SAMPLES_PER_FRAME = 160  # 20ms at 8kHz
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2  # int16


@dataclass
class PipelineConfig:
    # STT
    turn_frames: int = 100              # 100 × 20ms = 2s user turn
    stt_partial_interval_ms: float = 200  # partial transcript every 200ms

    # LLM
    llm_first_token_ms: float = 150     # time-to-first-token
    llm_token_interval_ms: float = 40   # ~25 tokens/sec
    llm_tokens_per_response: int = 20   # ~20 tokens

    # TTS
    tts_tokens_per_phrase: int = 5      # buffer 5 tokens before generating audio
    tts_frames_per_phrase: int = 30     # 30 × 20ms = 600ms audio per phrase
    tts_chunk_interval_ms: float = 20   # audio chunks arrive every 20ms from TTS


def _generate_pcm_frame(freq: float = 440.0) -> bytes:
    """One 20ms PCM16-LE frame."""
    samples = [
        int(16000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        for i in range(SAMPLES_PER_FRAME)
    ]
    return struct.pack(f"<{SAMPLES_PER_FRAME}h", *samples)


PCM_FRAME = _generate_pcm_frame()


# ══════════════════════════════════════════════════════════════════════════════
#  ASYNC VERSION — for Python server (callbacks on asyncio event loop)
# ══════════════════════════════════════════════════════════════════════════════

class AsyncMockPipeline:
    """Simulates STT→LLM→TTS with realistic async streaming callbacks.

    Each stage fires callbacks at realistic intervals using asyncio.sleep.
    These sleeps represent WAITING FOR NETWORK DATA, not CPU work — but they
    occupy the event loop's task scheduler, which is the contention source.
    """

    def __init__(self, config: PipelineConfig | None = None):
        self.config = config or PipelineConfig()
        self._frame_count = 0

    def feed_audio(self) -> bool:
        """Feed one incoming audio frame. Returns True when turn is complete."""
        self._frame_count += 1
        if self._frame_count >= self.config.turn_frames:
            self._frame_count = 0
            return True
        return False

    async def generate_response(self):
        """Simulate LLM→TTS streaming: yields (pcm_frames, phrase_idx) as they become available.

        This is the realistic version: instead of sleeping for the total delay,
        it fires many small callbacks simulating the streaming nature of each service.
        """
        cfg = self.config
        tokens_generated = 0
        phrase_idx = 0

        # Phase 1: Wait for LLM first token (TTFT)
        # In reality: HTTP request in flight, waiting for first SSE chunk
        await asyncio.sleep(cfg.llm_first_token_ms / 1000)

        # Phase 2: LLM streams tokens, TTS buffers and generates audio
        while tokens_generated < cfg.llm_tokens_per_response:
            # Accumulate tokens for one TTS phrase
            phrase_tokens = min(cfg.tts_tokens_per_phrase,
                              cfg.llm_tokens_per_response - tokens_generated)

            # Simulate LLM streaming tokens one at a time
            # Each token fires an event loop callback (SSE/WebSocket message)
            for _ in range(phrase_tokens):
                await asyncio.sleep(cfg.llm_token_interval_ms / 1000)
                tokens_generated += 1

            # TTS processes the phrase and streams audio chunks back
            # Each chunk fires an event loop callback (WebSocket message)
            frames = []
            for _ in range(cfg.tts_frames_per_phrase):
                await asyncio.sleep(cfg.tts_chunk_interval_ms / 1000)
                frames.append(PCM_FRAME)

            yield frames, phrase_idx
            phrase_idx += 1


# ══════════════════════════════════════════════════════════════════════════════
#  SYNC VERSION — for Rust server (blocking in thread, no event loop)
# ══════════════════════════════════════════════════════════════════════════════

class SyncMockPipeline:
    """Same pipeline but with blocking sleeps (for use in threads)."""

    def __init__(self, config: PipelineConfig | None = None):
        self.config = config or PipelineConfig()
        self._frame_count = 0

    def feed_audio(self) -> bool:
        self._frame_count += 1
        if self._frame_count >= self.config.turn_frames:
            self._frame_count = 0
            return True
        return False

    def generate_response(self) -> Generator[tuple[list[bytes], int], None, None]:
        """Simulate LLM→TTS streaming with blocking sleeps (runs in thread)."""
        cfg = self.config
        tokens_generated = 0
        phrase_idx = 0

        # LLM first token
        time.sleep(cfg.llm_first_token_ms / 1000)

        while tokens_generated < cfg.llm_tokens_per_response:
            phrase_tokens = min(cfg.tts_tokens_per_phrase,
                              cfg.llm_tokens_per_response - tokens_generated)

            # LLM streams tokens
            for _ in range(phrase_tokens):
                time.sleep(cfg.llm_token_interval_ms / 1000)
                tokens_generated += 1

            # TTS streams audio chunks
            frames = []
            for _ in range(cfg.tts_frames_per_phrase):
                time.sleep(cfg.tts_chunk_interval_ms / 1000)
                frames.append(PCM_FRAME)

            yield frames, phrase_idx
            phrase_idx += 1


# Keep backward compat — old code that imports MockPipeline
MockPipeline = SyncMockPipeline
