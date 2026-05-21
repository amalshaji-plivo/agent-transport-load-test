"""Single-session LiveKit RTC client simulator.

Drives the same workload `PlivoWsClient` does — 20 ms audio frames at
real-time pace, drain TTS audio, record per-frame metrics — but speaks
the LiveKit WebRTC signaling + media protocol instead of Plivo's
audio-stream WebSocket. This is the bench-side counterpart to
`livekit_python_server.py`.

URL shape: ``livekit://<host>:<port>?<query>``. Recognised query params
are parsed by `_parse_livekit_url` below — auth credentials default to
LIVEKIT_API_KEY / LIVEKIT_API_SECRET env vars (LiveKit dev-mode
placeholders work) and ``room_prefix`` defaults to ``bench``.

The harness routes one client per concurrent session; each client joins
its own unique room so the SFU dispatches a fresh agent job.

Metrics emitted via `SessionMetrics` match the Plivo client exactly:
  - record_send(seq)  on each published audio frame
  - record_recv(seq, phrase_id=…) on each received frame
This keeps the downstream aggregator / report code identical across
client kinds.
"""

from __future__ import annotations

import asyncio
import audioop
import os
import time
import uuid
from urllib.parse import urlparse, parse_qs

from livekit import api, rtc

from load_test.client.audio_gen import pregenerate_turn_chunks
from load_test.metrics.collector import SessionMetrics


# Phrase boundary detection — matches PlivoWsClient
PHRASE_GAP_THRESHOLD = 0.500

# Local audio source is 8 kHz mono PCM16-LE (160 samples / 20 ms).
SAMPLE_RATE = 8000
SAMPLES_PER_FRAME = SAMPLE_RATE // 50
NUM_CHANNELS = 1


def _parse_livekit_url(raw: str) -> tuple[str, str, str, str]:
    """Parse a `livekit://` URL into (signaling_url, api_key, api_secret, room_prefix).

    The signaling URL is rewritten to `ws://host:port` (or `wss://`) so
    livekit-rtc accepts it. Credentials and room prefix come from the
    query string; sensible dev defaults apply when absent.
    """
    p = urlparse(raw)
    qs = parse_qs(p.query)

    scheme = "wss" if p.scheme in ("livekit+wss", "livekit-tls") else "ws"
    host = p.hostname or "localhost"
    port = p.port or 7880
    sig_url = f"{scheme}://{host}:{port}"

    api_key = (qs.get("key") or [os.environ.get("LIVEKIT_API_KEY", "devkey")])[0]
    api_secret = (qs.get("secret") or [os.environ.get("LIVEKIT_API_SECRET", "secret")])[0]
    room_prefix = (qs.get("room_prefix") or ["bench"])[0]
    return sig_url, api_key, api_secret, room_prefix


def _decode_mulaw_to_pcm16(mulaw: bytes) -> bytes:
    """The shared `pregenerate_turn_chunks()` returns 20 ms mu-law frames
    (160 bytes each — what Plivo wants). LiveKit's audio path wants
    PCM16-LE at the chosen sample rate, so decompand on send.
    """
    return audioop.ulaw2lin(mulaw, 2)


class LivekitRtcClient:
    """Simulates one LiveKit RTC participant for the load test."""

    def __init__(
        self,
        url: str,
        session_id: str,
        metrics: SessionMetrics,
        duration_sec: float = 10.0,
    ):
        self._raw_url = url
        self._session_id = session_id
        self._metrics = metrics
        self._duration = duration_sec
        self._running = False
        self._chunks_mulaw = pregenerate_turn_chunks()
        # Pre-decode mu-law to PCM16 so the send loop only does packing
        self._chunks_pcm = [_decode_mulaw_to_pcm16(c) for c in self._chunks_mulaw]

    async def run(self) -> None:
        self._metrics.record_session_start()
        total_budget = (self._duration * 2) + 15
        try:
            await asyncio.wait_for(self._run_session(), timeout=total_budget)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            # The benchmark should never crash on a single bad session;
            # surface anything unexpected via SessionMetrics' end record
            # and move on.
            import traceback
            traceback.print_exc()
            print(f"[{self._session_id}] session error: {e}")
        finally:
            self._metrics.record_session_end()

    async def _run_session(self) -> None:
        sig_url, api_key, api_secret, room_prefix = _parse_livekit_url(self._raw_url)
        room_name = f"{room_prefix}-{uuid.uuid4().hex[:10]}"
        identity = f"caller-{uuid.uuid4().hex[:8]}"

        token = (
            api.AccessToken(api_key, api_secret)
            .with_identity(identity)
            .with_name(identity)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )

        room = rtc.Room()
        recv_state = {
            "seq": 0,
            "current_phrase": 0,
            "last_play_time": 0.0,
        }
        consumer_tasks: list[asyncio.Task] = []

        @room.on("track_subscribed")
        def _on_track(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                consumer_tasks.append(
                    asyncio.create_task(self._consume_audio(track, recv_state))
                )

        await room.connect(sig_url, token, options=rtc.RoomOptions(auto_subscribe=True))

        # Stock livekit-agents with `agent_name=""` (the default) is
        # auto-dispatched by the SFU as soon as a participant joins. We
        # deliberately do NOT call AgentDispatchService.create_dispatch
        # here — it fires a SECOND job for the same room and the bench
        # ends up with two agents producing TTS, doubling output rate
        # and contaminating jitter measurements.

        # Publish an outbound audio track.
        audio_source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track("bench-mic", audio_source)
        await room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )

        self._running = True
        send_task = asyncio.create_task(self._send_loop(audio_source))

        try:
            await send_task
            # Drain receive for a while — the agent's TTS bursts may still
            # be in flight after we stop publishing.
            drain_deadline = time.perf_counter() + self._duration
            while time.perf_counter() < drain_deadline:
                await asyncio.sleep(0.05)
                # Stop draining early once we've gone silent for a beat
                if (
                    recv_state["last_play_time"] > 0
                    and time.perf_counter() - recv_state["last_play_time"] > 1.5
                ):
                    break
        finally:
            self._running = False
            for t in consumer_tasks:
                t.cancel()
            try:
                await room.disconnect()
            except Exception:
                pass

    async def _send_loop(self, audio_source: rtc.AudioSource) -> None:
        """Pace 20 ms PCM16 frames at real-time rate."""
        start = time.perf_counter()
        seq = 0
        num_chunks = len(self._chunks_pcm)

        # Pre-build a memoryview of each PCM chunk in 'b' (signed-byte) format
        # so per-frame copy is just a strided assign — no per-iter allocation.
        # `frame.data` is a memoryview of int16; `.cast('b')` lets us treat
        # both sides as raw bytes for a structurally-compatible slice assign.
        pcm_views = [memoryview(b).cast("b") for b in self._chunks_pcm]

        while time.perf_counter() - start < self._duration:
            pcm_view = pcm_views[seq % num_chunks]
            frame = rtc.AudioFrame.create(SAMPLE_RATE, NUM_CHANNELS, SAMPLES_PER_FRAME)
            bview = memoryview(frame.data).cast("b")
            n = min(len(pcm_view), len(bview))
            bview[:n] = pcm_view[:n]

            self._metrics.record_send(seq)
            try:
                await audio_source.capture_frame(frame)
            except Exception:
                break

            seq += 1
            elapsed = time.perf_counter() - start
            expected = seq * 0.020
            sleep_time = expected - elapsed
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    async def _consume_audio(self, track: rtc.Track, recv_state: dict) -> None:
        """Drain the agent's audio track, record per-frame metrics.

        We pin `frame_size_ms=20` so the receive cadence matches the
        send cadence (50 fps); without this, AudioStream defaults to a
        smaller chunk size (typically 10 ms) and the bench's "within-
        phrase gap" metric is comparing 10 ms chunks against the gateway
        path's 20 ms chunks. Sample rate is pinned to the source-side
        8 kHz so AudioStream doesn't redundantly resample to 48 kHz.
        """
        stream = rtc.AudioStream(
            track,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            frame_size_ms=20,
        )
        try:
            async for evt in stream:
                if not self._running and recv_state["seq"] > 0:
                    break
                now = time.perf_counter()

                last = recv_state["last_play_time"]
                if last > 0 and (now - last) > PHRASE_GAP_THRESHOLD:
                    recv_state["current_phrase"] += 1

                self._metrics.record_recv(
                    recv_state["seq"],
                    server_send_wall=0.0,
                    pipeline_latency=0.0,
                    phrase_id=recv_state["current_phrase"],
                )
                recv_state["seq"] += 1
                recv_state["last_play_time"] = now
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

