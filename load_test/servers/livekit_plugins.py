"""Mock livekit-agents plugins for benchmarking — talk to wire-mock backends.

Each plugin connects to the real STT/LLM/TTS endpoints exposed by
`load_test.servers.mock_services` over real WebSocket / HTTP. The
inline `asyncio.sleep` simulations have been removed: socket I/O now
provides the same back-pressure the production code would see against
Deepgram / OpenAI / etc.

Endpoint timings (see `mock_services.py`):
  STT:  partials every 200 ms, final after ~2 s of audio.
  LLM:  150 ms time-to-first-token, ~25 tok/s thereafter.
  TTS:  80 ms time-to-first-chunk, 20 ms between chunks, ~600 ms total.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import aiohttp

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

from load_test.servers.mock_clients import (
    llm_stream_tokens,
    stt_url,
    tts_url,
)

# Match BenchPipecatTTS: 8 kHz output, no resampling on the way to Plivo's 8 kHz
# mu-law wire format. At 24 kHz the LiveKit output pipeline resamples 3:1 per
# chunk in Python, which is a measurable per-chunk cost pipecat doesn't pay.
TTS_SAMPLE_RATE = 8000
TTS_NUM_CHANNELS = 1


# ══════════════════════════════════════════════════════════════════════════════
#  BENCH STT — persistent WS to mock STT, streams audio frames in real-time
# ══════════════════════════════════════════════════════════════════════════════


class BenchSTT(STT):
    def __init__(self) -> None:
        super().__init__(
            capabilities=STTCapabilities(streaming=True, interim_results=False),
        )

    async def _recognize_impl(self, buffer, *, language, conn_options):
        # One-shot recognize is never invoked by the bench (streaming only),
        # but the base class requires it. Open a fresh WS for safety.
        text = ""
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(stt_url(), heartbeat=None) as ws:
                    audio_bytes = buffer.data.tobytes() if hasattr(buffer, "data") else bytes(buffer)
                    for i in range(0, len(audio_bytes), 320):
                        await ws.send_bytes(audio_bytes[i : i + 320])
                    await ws.send_str(json.dumps({"type": "Finalize"}))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            obj = json.loads(msg.data)
                            if obj.get("is_final"):
                                alts = obj.get("channel", {}).get("alternatives", [])
                                if alts:
                                    text = alts[0].get("transcript", "")
                                break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass

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
    """Streams every incoming AudioFrame to the mock STT over WebSocket.

    Real Deepgram / similar STT vendors keep one WS per session for the
    call's lifetime; we mirror that here. The mock decides when to emit
    interim vs final transcripts (every 200 ms / every ~2 s of audio).
    """

    def __init__(self, *, stt: BenchSTT, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt, conn_options=conn_options)

    async def _run(self) -> None:
        async with aiohttp.ClientSession() as sess:
            try:
                async with sess.ws_connect(stt_url(), heartbeat=None) as ws:
                    await asyncio.gather(
                        self._send_audio(ws),
                        self._read_results(ws),
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # Drop the stream — base class will mark it errored.
                return

    async def _send_audio(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for data in self._input_ch:
                if isinstance(data, rtc.AudioFrame):
                    # rtc.AudioFrame.data is a ctypes-backed buffer; .tobytes()
                    # copies once into a heap bytes object the WS can send.
                    pcm = bytes(data.data) if not isinstance(data.data, bytes) else data.data
                    await ws.send_bytes(pcm)
                elif isinstance(data, self._FlushSentinel):
                    # End of utterance — tell the mock to commit the current
                    # turn so it emits a final transcript promptly.
                    await ws.send_str(json.dumps({"type": "Finalize"}))
        finally:
            # Half-close so the read side knows no more audio is coming.
            try:
                await ws.send_str(json.dumps({"type": "Finalize"}))
            except Exception:
                pass

    async def _read_results(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    obj = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "Results":
                    continue
                alts = obj.get("channel", {}).get("alternatives", [])
                if not alts:
                    continue
                text = alts[0].get("transcript", "")
                if not text:
                    continue
                if obj.get("is_final"):
                    # Real STTs emit START_OF_SPEECH → FINAL_TRANSCRIPT → END_OF_SPEECH.
                    # turn_detection="stt" on AgentSession keys off the END_OF_SPEECH
                    # event to commit the user turn; without it the pipeline never
                    # advances to LLM/TTS.
                    self._event_ch.send_nowait(
                        SpeechEvent(type=SpeechEventType.START_OF_SPEECH)
                    )
                    self._event_ch.send_nowait(
                        SpeechEvent(
                            type=SpeechEventType.FINAL_TRANSCRIPT,
                            alternatives=[SpeechData(text=text, language="en")],
                        )
                    )
                    self._event_ch.send_nowait(
                        SpeechEvent(type=SpeechEventType.END_OF_SPEECH)
                    )
                # Interim results are not forwarded (interim_results=False).
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                break


# ══════════════════════════════════════════════════════════════════════════════
#  BENCH LLM — POST per turn, stream SSE tokens
# ══════════════════════════════════════════════════════════════════════════════


class BenchLLM(LLM):
    def __init__(self) -> None:
        super().__init__()
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

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


def _messages_from_chat_ctx(chat_ctx: ChatContext) -> list[dict]:
    """Best-effort conversion of livekit ChatContext to OpenAI message dicts.

    livekit-agents stores messages as ChatItem; the public API for the
    list varies by version (.items vs .messages vs .to_chat_completion).
    Fall back gracefully to a synthetic "hello" turn if extraction fails
    so the LLM call always has something to chew on.
    """
    out: list[dict] = []
    items = getattr(chat_ctx, "items", None) or getattr(chat_ctx, "messages", None) or []
    for item in items:
        role = getattr(item, "role", None) or getattr(item, "type", "user")
        content = getattr(item, "content", None) or getattr(item, "text_content", None)
        # `content` can be list[str] | str | None depending on version
        if isinstance(content, list):
            content_text = " ".join(str(c) for c in content if c)
        elif content is None:
            content_text = ""
        else:
            content_text = str(content)
        if content_text:
            out.append({"role": role, "content": content_text})
    if not out:
        out = [{"role": "user", "content": "hello"}]
    return out


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
        messages = _messages_from_chat_ctx(self._chat_ctx)
        session = await self._llm._get_session()

        async for token in llm_stream_tokens(messages, session=session):
            self._event_ch.send_nowait(
                ChatChunk(
                    id=str(uuid.uuid4()),
                    delta=ChoiceDelta(role="assistant", content=token),
                )
            )


# ══════════════════════════════════════════════════════════════════════════════
#  BENCH TTS — WS per phrase, streams audio chunks back
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
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

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

        assert isinstance(self._tts, BenchTTS)
        session = await self._tts._get_session()
        input_text = ""

        async for data in self._input_ch:
            if isinstance(data, str):
                input_text += data
                continue
            elif isinstance(data, SynthesizeStream._FlushSentinel) and not input_text:
                continue

            text_to_synth = input_text
            input_text = ""
            self._mark_started()

            try:
                async with session.ws_connect(tts_url(), heartbeat=None) as ws:
                    await ws.send_str(
                        json.dumps({
                            "text": text_to_synth,
                            "sample_rate": self._tts.sample_rate,
                        })
                    )
                    output_emitter.start_segment(segment_id=str(uuid.uuid4()))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            output_emitter.push(msg.data)
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
                    output_emitter.flush()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                output_emitter.flush()
                continue
