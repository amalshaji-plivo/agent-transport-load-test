"""Multi-session orchestrator.

Manages N concurrent client instances with staggered startup so the
server doesn't see a thundering herd on session zero. The client type
is chosen by URL scheme:

  ws://…                  → PlivoWsClient   (Plivo audio-stream WS)
  livekit://…             → LivekitRtcClient (LiveKit RTC over WebRTC)
  livekit+wss://…         → LivekitRtcClient with WSS signaling

The two clients drive an equivalent workload (20 ms frames at real-time
pace + drain) and write into the same SessionMetrics shape — so the
downstream metrics aggregator doesn't need to know which kind ran.
"""

import asyncio
import time

from load_test.client.plivo_ws_client import PlivoWsClient
from load_test.metrics.collector import TestRunMetrics


def _client_for_url(url: str):
    """Pick the bench client for one URL by scheme.

    LivekitRtcClient is imported lazily so the gateway-only path (PlivoWsClient
    over plain websockets) doesn't require the `livekit` / `livekit-api`
    packages — they're only needed for the stock-LiveKit comparison target.
    """
    if url.startswith("livekit://") or url.startswith("livekit+"):
        from load_test.client.livekit_rtc_client import LivekitRtcClient
        return LivekitRtcClient
    return PlivoWsClient


class SessionManager:
    """Spawns and manages concurrent load test sessions.

    ``url`` may be a single string or a list of strings; when a list is given,
    sessions are routed round-robin across the URLs so a horizontal topology
    (N × 1-CPU servers) can be load-tested without an external TCP LB.
    """

    def __init__(
        self,
        url: str | list[str],
        implementation: str,
        concurrency: int,
        session_duration: float,
        ramp_delay: float = 0.1,
        warmup_sec: float = 0,
    ):
        self._urls: list[str] = [url] if isinstance(url, str) else list(url)
        if not self._urls:
            raise ValueError("SessionManager requires at least one URL")
        self._implementation = implementation
        self._concurrency = concurrency
        self._session_duration = session_duration
        self._ramp_delay = ramp_delay
        self._warmup_sec = warmup_sec

    async def run(self) -> TestRunMetrics:
        """Execute all sessions and return collected metrics."""
        run_metrics = TestRunMetrics(
            implementation=self._implementation,
            concurrency=self._concurrency,
        )

        # Extend client duration to include warmup
        total_duration = self._session_duration + self._warmup_sec

        tasks: list[asyncio.Task] = []
        for i in range(self._concurrency):
            session_id = f"{self._implementation}-session-{i}"
            sm = run_metrics.create_session(session_id)
            url = self._urls[i % len(self._urls)]
            client_cls = _client_for_url(url)
            client = client_cls(
                url=url,
                session_id=session_id,
                metrics=sm,
                duration_sec=total_duration,
            )
            task = asyncio.create_task(client.run())
            tasks.append(task)

            # Stagger session starts
            if self._ramp_delay > 0 and i < self._concurrency - 1:
                await asyncio.sleep(self._ramp_delay)

        # Warmup: let sessions reach steady state, then discard metrics
        if self._warmup_sec > 0:
            await asyncio.sleep(self._warmup_sec)
            run_metrics.reset_all_sessions()

        run_metrics.wall_start = time.monotonic()

        # Wait for all sessions to complete
        await asyncio.gather(*tasks, return_exceptions=True)

        run_metrics.wall_end = time.monotonic()
        return run_metrics
