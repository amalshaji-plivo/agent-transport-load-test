"""Mock livekit-agents plugins for benchmarking.

Timing matches the mock HTTP/WebSocket services used by the other benchmark
servers so that pipeline latency is comparable across all three implementations:

  STT:  streaming, emits final transcript on VAD flush (~200ms processing)
  LLM:  150ms TTFT, then ~15 tokens at 40ms each
  TTS:  80ms TTFB, then streams ~600ms of audio in 20ms chunks (24kHz)
"""

import asyncio
import uuid

from livekit import rtc
from livekit.agents import utils
from livekit.agents.stt import (
    STT,
    RecognizeStream,
    SpeechData,
    SpeechEvent,
    SpeechEventType,
    STTCapabilities,
)
from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    LLMStream,
    Tool,
    ToolChoice,
)
from livekit.agents.tts import (
    TTS,
    SynthesizeStream,
    TTSCapabilities,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)

# ── Timing constants (match mock_services/) ──────────────────────────────────

STT_PROCESSING_DELAY = 0.2       # 200ms from end-of-speech to final transcript
LLM_FIRST_TOKEN_MS = 0.15        # 150ms time-to-first-token
LLM_TOKEN_INTERVAL = 0.04        # 40ms between tokens (~25 tok/s)
TTS_FIRST_CHUNK_MS = 0.08        # 80ms synthesis startup
TTS_CHUNK_INTERVAL = 0.02        # 20ms between audio chunks
# Match BenchPipecatTTS: 8 kHz output, no resampling on the way to Plivo's 8 kHz
# mu-law wire format. At 24 kHz the LiveKit output pipeline resamples 3:1 per
# chunk in Python, which is a measurable per-chunk cost that pipecat doesn't pay.
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


# ══════════════════════════════════════════════════════════════════════════════
#  BENCH STT — streaming, emits final on flush
# ══════════════════════════════════════════════════════════════════════════════


class BenchSTT(STT):
    def __init__(self) -> None:
        super().__init__(
            capabilities=STTCapabilities(streaming=True, interim_results=False),
        )
        self._phrase_idx = 0

    async def _recognize_impl(self, buffer, *, language, conn_options):
        await asyncio.sleep(STT_PROCESSING_DELAY)
        text = PHRASES[self._phrase_idx % len(PHRASES)]
        self._phrase_idx += 1
        return SpeechEvent(
            type=SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[SpeechData(text=text, language="en")],
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "BenchRecognizeStream":
        return BenchRecognizeStream(stt=self, conn_options=conn_options)


class BenchRecognizeStream(RecognizeStream):
    """Mock streaming STT — emits a final transcript every ~2 seconds of audio.

    We use a frame counter instead of VAD flushes because the benchmark client
    sends a continuous 440Hz tone which may not trigger Silero VAD's speech/silence
    boundaries. This mirrors the 100-frame-per-turn pattern used by the raw AT
    and direct-pipecat benchmark servers.
    """

    FRAMES_PER_TURN = 100  # 100 × 20ms = 2 seconds

    def __init__(self, *, stt: BenchSTT, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt, conn_options=conn_options)

    async def _run(self) -> None:
        stt = self._stt
        assert isinstance(stt, BenchSTT)

        def _emit_turn() -> None:
            text = PHRASES[stt._phrase_idx % len(PHRASES)]
            stt._phrase_idx += 1
            # Real STTs emit START_OF_SPEECH → FINAL_TRANSCRIPT → END_OF_SPEECH.
            # turn_detection="stt" on AgentSession keys off the END_OF_SPEECH
            # event to commit the user turn; without it the pipeline never
            # advances to LLM/TTS.
            self._event_ch.send_nowait(SpeechEvent(type=SpeechEventType.START_OF_SPEECH))
            self._event_ch.send_nowait(
                SpeechEvent(
                    type=SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[SpeechData(text=text, language="en")],
                )
            )
            self._event_ch.send_nowait(SpeechEvent(type=SpeechEventType.END_OF_SPEECH))

        audio_frames = 0
        async for data in self._input_ch:
            if isinstance(data, rtc.AudioFrame):
                audio_frames += 1
                if audio_frames >= self.FRAMES_PER_TURN:
                    audio_frames = 0
                    await asyncio.sleep(STT_PROCESSING_DELAY)
                    _emit_turn()
            elif isinstance(data, self._FlushSentinel):
                if audio_frames > 0:
                    audio_frames = 0
                    await asyncio.sleep(STT_PROCESSING_DELAY)
                    _emit_turn()


# ══════════════════════════════════════════════════════════════════════════════
#  BENCH LLM — streams tokens with realistic timing
# ══════════════════════════════════════════════════════════════════════════════


class BenchLLM(LLM):
    def __init__(self) -> None:
        super().__init__()
        self._response_idx = 0

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict] = NOT_GIVEN,
    ) -> "BenchLLMStream":
        return BenchLLMStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class BenchLLMStream(LLMStream):
    def __init__(
        self,
        llm: BenchLLM,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)

    async def _run(self) -> None:
        assert isinstance(self._llm, BenchLLM)
        response_text = RESPONSES[self._llm._response_idx % len(RESPONSES)]
        self._llm._response_idx += 1
        tokens = response_text.split()

        # First-token delay
        await asyncio.sleep(LLM_FIRST_TOKEN_MS)

        # Stream tokens
        for token in tokens:
            self._event_ch.send_nowait(
                ChatChunk(
                    id=str(uuid.uuid4()),
                    delta=ChoiceDelta(role="assistant", content=token + " "),
                )
            )
            await asyncio.sleep(LLM_TOKEN_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
#  BENCH TTS — streams audio chunks with realistic timing
# ══════════════════════════════════════════════════════════════════════════════


class BenchTTS(TTS):
    def __init__(
        self,
        *,
        sample_rate: int = TTS_SAMPLE_RATE,
        num_channels: int = TTS_NUM_CHANNELS,
    ) -> None:
        super().__init__(
            capabilities=TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=num_channels,
        )

    def synthesize(self, text, *, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        raise NotImplementedError("Use stream() for benchmarking")

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> "BenchSynthesizeStream":
        return BenchSynthesizeStream(tts=self, conn_options=conn_options)


class BenchSynthesizeStream(SynthesizeStream):
    def __init__(self, *, tts: BenchTTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)

    async def _run(self, output_emitter) -> None:
        output_emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=True,
        )

        input_text = ""
        async for data in self._input_ch:
            if isinstance(data, str):
                input_text += data
                continue
            elif isinstance(data, SynthesizeStream._FlushSentinel) and not input_text:
                continue

            # Synthesize the collected text
            self._mark_started()

            # Number of chunks scales with text length (~1 chunk per 2 chars)
            n_chunks = max(10, min(30, len(input_text) // 2))
            input_text = ""

            # First-chunk delay (synthesis startup)
            await asyncio.sleep(TTS_FIRST_CHUNK_MS)

            # 20ms of silence per chunk at TTS sample rate
            samples_per_chunk = self._tts.sample_rate // 50  # 20ms
            chunk_bytes = b"\x00\x00" * samples_per_chunk

            output_emitter.start_segment(segment_id=str(uuid.uuid4()))

            for _ in range(n_chunks):
                output_emitter.push(chunk_bytes)
                await asyncio.sleep(TTS_CHUNK_INTERVAL)

            output_emitter.flush()
