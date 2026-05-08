"""Benchmark metadata helpers shared by both server implementations."""

import json
import time
from collections import deque
from dataclasses import dataclass


@dataclass(slots=True)
class PendingFrameMeta:
    phrase_id: int
    server_send_wall: float
    pipeline_latency: float


def build_frame_meta(
    *,
    phrase_id: int,
    pipeline_latency: float = 0.0,
    server_send_wall: float | None = None,
) -> str:
    """Build a side-band metadata message for the next outbound audio frame."""
    return json.dumps({
        "event": "_lt_meta",
        "phrase_id": phrase_id,
        "server_send_wall": server_send_wall if server_send_wall is not None else 0.0,
        "pipeline_latency": pipeline_latency,
    })


class PendingMetaQueue:
    """FIFO store for metadata that should apply to subsequent playAudio frames."""

    def __init__(self):
        self._queue: deque[PendingFrameMeta] = deque()

    def append_from_message(self, data: dict) -> None:
        self._queue.append(
            PendingFrameMeta(
                phrase_id=int(data.get("phrase_id", -1)),
                server_send_wall=float(data.get("server_send_wall", 0.0)),
                pipeline_latency=float(data.get("pipeline_latency", 0.0)),
            )
        )

    def pop_next(self) -> PendingFrameMeta | None:
        if self._queue:
            return self._queue.popleft()
        return None
