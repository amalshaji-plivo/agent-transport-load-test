"""Single-session Plivo WebSocket client simulator.

Speaks the Plivo audio stream protocol: sends start -> media frames -> stop.
Tracks per-burst output delivery jitter — the key metric for voice quality.

The server simulates a voice agent:
  - Client sends audio at 20ms intervals (user speaking)
  - Server responds with TTS bursts every ~1s of input
  - Client measures how smoothly the burst frames are delivered

Key metric: output inter-frame jitter. Perfect delivery = exactly 20ms
between frames. Python asyncio.sleep degrades under concurrency; Rust
tokio::time::interval stays steady.
"""

import asyncio
import base64
import json
import time
import uuid

import websockets

from load_test.client.audio_gen import pregenerate_turn_chunks
from load_test.metrics.collector import SessionMetrics
from load_test.servers.benchmark_metadata import PendingMetaQueue


class PlivoWsClient:
    """Simulates one Plivo WebSocket audio stream session."""

    def __init__(
        self,
        url: str,
        session_id: str,
        metrics: SessionMetrics,
        duration_sec: float = 10.0,
    ):
        self._url = url
        self._session_id = session_id
        self._metrics = metrics
        self._duration = duration_sec
        self._running = False
        self._chunks = pregenerate_turn_chunks()

    async def run(self):
        self._metrics.record_session_start()
        # Hard upper bound: send + drain + teardown grace. Servers like LiveKit
        # keep the WS open indefinitely after TTS finishes and wait for the
        # client to send "stop"; if the close handshake stalls (e.g., server
        # task queue backed up), __aexit__ can block for ages. Cap the whole
        # session so the harness always advances to the next step.
        total_budget = (self._duration * 2) + 15
        try:
            await asyncio.wait_for(self._run_session(), timeout=total_budget)
        except (
            asyncio.TimeoutError,
            websockets.ConnectionClosed,
            ConnectionRefusedError,
            OSError,
        ):
            pass
        finally:
            self._metrics.record_session_end()

    async def _run_session(self):
        async with websockets.connect(self._url, close_timeout=2) as ws:
            call_id = str(uuid.uuid4())
            stream_id = str(uuid.uuid4())
            await ws.send(json.dumps({
                "event": "start",
                "start": {
                    "callId": call_id,
                    "streamId": stream_id,
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                    },
                },
            }))

            self._running = True
            send_task = asyncio.create_task(self._send_loop(ws))
            recv_task = asyncio.create_task(self._recv_loop(ws))

            await send_task

            # Keep receiving burst responses — the server's paced send loop
            # is still delivering frames after we stop sending input.
            # Wait up to session_duration to drain (bursts take time to deliver).
            drain_timeout = self._duration
            try:
                await asyncio.wait_for(asyncio.shield(recv_task), timeout=drain_timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

            self._running = False

            try:
                await asyncio.wait_for(
                    ws.send(json.dumps({"event": "stop"})), timeout=2
                )
            except (websockets.ConnectionClosed, asyncio.TimeoutError):
                pass

            recv_task.cancel()
            try:
                await asyncio.wait_for(recv_task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    async def _send_loop(self, ws):
        """Send 20ms mu-law audio frames at real-time pace."""
        start = time.perf_counter()
        seq = 0
        num_chunks = len(self._chunks)

        while time.perf_counter() - start < self._duration:
            chunk = self._chunks[seq % num_chunks]
            payload = base64.b64encode(chunk).decode()

            self._metrics.record_send(seq)

            try:
                await ws.send(json.dumps({
                    "event": "media",
                    "media": {"payload": payload},
                }))
            except websockets.ConnectionClosed:
                break

            seq += 1

            elapsed = time.perf_counter() - start
            expected = seq * 0.020
            sleep_time = expected - elapsed
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    async def _recv_loop(self, ws):
        """Receive TTS burst frames and record delivery jitter + component latencies.

        Handles two server message formats:
        - Direct pipecat: _lt_send and _lt_pipeline embedded in playAudio JSON
        - Agent-transport: separate _lt_meta message before each playAudio
        """
        seq = 0
        current_phrase = 0
        last_play_time = 0.0
        pending_frame_meta = PendingMetaQueue()
        PHRASE_GAP_THRESHOLD = 0.500  # 500ms gap = new phrase (raised from 100ms to uncap silence metric at high concurrency)
        try:
            async for raw_msg in ws:
                if not self._running and seq > 0:
                    break
                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                event = data.get("event")

                if event == "_lt_meta":
                    pending_frame_meta.append_from_message(data)

                elif event == "playAudio":
                    now = time.perf_counter()

                    # Detect phrase boundaries by time gap
                    if last_play_time > 0 and (now - last_play_time) > PHRASE_GAP_THRESHOLD:
                        current_phrase += 1

                    pending_meta = pending_frame_meta.pop_next()

                    # Extract embedded metadata (direct pipecat server)
                    send_wall = data.get(
                        "_lt_send",
                        pending_meta.server_send_wall if pending_meta else 0.0,
                    )
                    pipeline_lat = data.get(
                        "_lt_pipeline",
                        pending_meta.pipeline_latency if pending_meta else 0.0,
                    )
                    phrase_id = data.get(
                        "_lt_phrase",
                        pending_meta.phrase_id if pending_meta else current_phrase,
                    )

                    if phrase_id >= 0:
                        current_phrase = phrase_id

                    self._metrics.record_recv(
                        seq,
                        server_send_wall=send_wall,
                        pipeline_latency=pipeline_lat,
                        phrase_id=phrase_id,
                    )
                    seq += 1
                    last_play_time = now

        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
