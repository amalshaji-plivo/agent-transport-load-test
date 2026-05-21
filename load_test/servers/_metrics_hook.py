"""Shared `metrics_collected` event sink used by both agent servers.

livekit-agents 1.x emits per-plugin timing via `AgentSession.on(
'metrics_collected', …)`. We append each event as one JSON line to
AGENT_METRICS_LOG so the bench harness can attribute CPU between
VAD / EOU (turn detector) / STT / LLM / TTS after the run completes.

Multiple workers in the same container may append concurrently. JSON
lines well under the PIPE_BUF / page-size boundary are atomic on
`O_APPEND` on Linux + macOS, which is what we rely on here — no locks.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from typing import Any


def attach(session) -> None:
    """Attach a metrics_collected sink to `session` if AGENT_METRICS_LOG is set.

    No-op if the env var is empty/unset so production / unit tests aren't
    burdened with extra I/O.
    """
    path = os.environ.get("AGENT_METRICS_LOG", "").strip()
    if not path:
        return

    def _on(ev: Any) -> None:
        m = getattr(ev, "metrics", None)
        if m is None:
            return
        rec: dict[str, Any] = {
            "ts": time.time(),
            "type": m.__class__.__name__,
        }
        # Most livekit-agents Metrics classes are pydantic dataclasses; fall
        # through to plain-dict if conversion fails for some plugin variant.
        try:
            if dataclasses.is_dataclass(m):
                rec["data"] = dataclasses.asdict(m)
            elif hasattr(m, "model_dump"):
                rec["data"] = m.model_dump()
            else:
                rec["data"] = {
                    k: getattr(m, k) for k in dir(m)
                    if not k.startswith("_") and not callable(getattr(m, k))
                }
        except Exception:
            rec["data"] = {}
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            # Don't let metrics I/O take down a session
            pass

    session.on("metrics_collected", _on)
