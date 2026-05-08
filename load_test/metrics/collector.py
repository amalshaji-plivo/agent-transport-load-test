"""Per-session frame-level metric recording.

Tracks end-to-end latency, component latencies, and critically:
WITHIN-PHRASE jitter (consecutive frames in the same TTS phrase).

This separates real transport jitter from inter-phrase gaps (which are
pipeline artifacts, not transport quality issues).
"""

import time
from dataclasses import dataclass, field
from typing import NamedTuple


class FrameTimestamp(NamedTuple):
    seq: int
    send_time: float
    recv_time: float


@dataclass
class SessionMetrics:
    session_id: str
    implementation: str

    start_time: float = 0.0
    first_frame_sent_time: float = 0.0
    first_frame_recv_time: float = 0.0
    # Post-warmup "first send/recv" — captures steady-state RTT rather than
    # cold-start latency. Useful in its own right.
    post_warmup_first_sent_time: float = 0.0
    post_warmup_first_recv_time: float = 0.0
    end_time: float = 0.0

    frames_sent: int = 0
    frames_received: int = 0

    timestamps: list[FrameTimestamp] = field(default_factory=list)

    # ALL inter-frame gaps (includes inter-phrase gaps)
    inter_frame_gaps: list[float] = field(default_factory=list)

    # WITHIN-PHRASE inter-frame gaps only (the real transport jitter metric)
    within_phrase_gaps: list[float] = field(default_factory=list)

    _last_recv_time: float = 0.0
    _last_phrase_id: int = -1

    # Decoupled from frames_sent/received so warmup-reset doesn't clobber
    # the cold-start first-frame measurement. These flags are intentionally
    # NOT reset in reset_for_measurement().
    _cold_first_send_recorded: bool = False
    _cold_first_recv_recorded: bool = False

    # Component latencies
    transport_delivery_times: list[float] = field(default_factory=list)
    pipeline_latencies: list[float] = field(default_factory=list)

    def record_session_start(self):
        self.start_time = time.perf_counter()

    def record_send(self, seq: int):
        now = time.perf_counter()
        # Cold-start first-frame (survives warmup reset)
        if not self._cold_first_send_recorded:
            self.first_frame_sent_time = now
            self._cold_first_send_recorded = True
        # Post-warmup steady-state first-frame (re-captured after each reset)
        if self.frames_sent == 0:
            self.post_warmup_first_sent_time = now
        self.timestamps.append(FrameTimestamp(seq=seq, send_time=now, recv_time=0.0))
        self.frames_sent += 1

    def record_recv(self, seq: int, server_send_wall: float = 0.0,
                    pipeline_latency: float = 0.0, phrase_id: int = -1):
        now = time.perf_counter()
        now_wall = time.time()

        # Cold-start first-frame (survives warmup reset)
        if not self._cold_first_recv_recorded:
            self.first_frame_recv_time = now
            self._cold_first_recv_recorded = True
        # Post-warmup steady-state first-frame (re-captured after each reset)
        if self.frames_received == 0:
            self.post_warmup_first_recv_time = now

        if seq < len(self.timestamps):
            old = self.timestamps[seq]
            self.timestamps[seq] = FrameTimestamp(
                seq=old.seq, send_time=old.send_time, recv_time=now
            )

        # All inter-frame gaps
        if self._last_recv_time > 0:
            gap = now - self._last_recv_time
            self.inter_frame_gaps.append(gap)

            # Within-phrase gaps: only if same phrase as previous frame
            if phrase_id >= 0 and phrase_id == self._last_phrase_id:
                self.within_phrase_gaps.append(gap)

        self._last_recv_time = now
        self._last_phrase_id = phrase_id

        # Transport delivery (wall clock)
        if server_send_wall > 0:
            transport_delay = now_wall - server_send_wall
            if transport_delay >= 0:
                self.transport_delivery_times.append(transport_delay)

        # Pipeline latency (first frame of first phrase only)
        if pipeline_latency > 0:
            self.pipeline_latencies.append(pipeline_latency)

        self.frames_received += 1

    def reset_for_measurement(self):
        """Discard warmup metrics. Cold-start first-frame is preserved; the
        post_warmup_first_* fields will be re-captured on the next send/recv.
        """
        self.frames_sent = 0
        self.frames_received = 0
        self.timestamps.clear()
        self.inter_frame_gaps.clear()
        self.within_phrase_gaps.clear()
        self.transport_delivery_times.clear()
        self.pipeline_latencies.clear()
        self._last_recv_time = 0.0
        self._last_phrase_id = -1
        # Clear post-warmup first-frame so it's captured fresh
        self.post_warmup_first_sent_time = 0.0
        self.post_warmup_first_recv_time = 0.0
        # Note: _cold_first_send_recorded / _cold_first_recv_recorded stay True
        # — cold-start first-frame is fixed at real session start.

    def record_session_end(self):
        self.end_time = time.perf_counter()

    @property
    def first_frame_latency(self) -> float:
        """Cold-start first-frame latency: time from session's very first send
        to its very first recv. NOT affected by warmup reset. This answers:
        'When a new call connects, how long until the agent speaks?'
        """
        if self.first_frame_recv_time > 0 and self.first_frame_sent_time > 0:
            return self.first_frame_recv_time - self.first_frame_sent_time
        return 0.0

    @property
    def post_warmup_rtt(self) -> float:
        """Steady-state send→recv interval after warmup reset. Much smaller
        than first_frame_latency under normal operation — it captures how
        long a frame round-trips through an already-running pipeline.
        """
        if self.post_warmup_first_recv_time > 0 and self.post_warmup_first_sent_time > 0:
            return self.post_warmup_first_recv_time - self.post_warmup_first_sent_time
        return 0.0

    @property
    def round_trip_latencies(self) -> list[float]:
        return [t.recv_time - t.send_time for t in self.timestamps if t.recv_time > 0]

    @property
    def output_to_input_frame_ratio(self) -> float:
        if self.frames_sent == 0:
            return 0.0
        return self.frames_received / self.frames_sent

    @property
    def duration(self) -> float:
        if self.end_time > 0 and self.start_time > 0:
            return self.end_time - self.start_time
        return 0.0


@dataclass
class TestRunMetrics:
    implementation: str
    concurrency: int
    sessions: dict[str, SessionMetrics] = field(default_factory=dict)
    wall_start: float = 0.0
    wall_end: float = 0.0

    def create_session(self, session_id: str) -> SessionMetrics:
        sm = SessionMetrics(session_id=session_id, implementation=self.implementation)
        self.sessions[session_id] = sm
        return sm

    def reset_all_sessions(self):
        """Reset metrics for all sessions (warmup discard)."""
        for sm in self.sessions.values():
            sm.reset_for_measurement()

    @property
    def wall_duration(self) -> float:
        if self.wall_end > 0 and self.wall_start > 0:
            return self.wall_end - self.wall_start
        return 0.0
