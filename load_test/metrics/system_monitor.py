"""System resource monitoring — Docker stats API and psutil fallback.

Collects CPU% and memory usage for server containers/processes at
regular intervals in a background thread.
"""

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import NamedTuple


class ResourceSnapshot(NamedTuple):
    """Single point-in-time resource measurement."""
    timestamp: float  # time.monotonic()
    cpu_percent: float
    memory_mb: float
    active_sessions: int  # 0 if unknown


@dataclass
class SystemMonitor:
    """Base class — subclassed by Docker and psutil monitors."""

    interval: float = 1.0
    snapshots: list[ResourceSnapshot] = field(default_factory=list)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self) -> list[ResourceSnapshot]:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self.snapshots

    def _collect_loop(self):
        raise NotImplementedError


@dataclass
class DockerStatsMonitor(SystemMonitor):
    """Collects CPU/memory from a Docker container via `docker stats`."""

    container_name: str = ""

    def _collect_loop(self):
        while not self._stop_event.is_set():
            try:
                result = subprocess.run(
                    [
                        "docker", "stats", self.container_name,
                        "--no-stream", "--format",
                        '{"cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}"}'
                    ],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout.strip())
                    cpu = float(data["cpu"].rstrip("%"))
                    # Parse memory: "123.4MiB / 512MiB"
                    mem_str = data["mem"].split("/")[0].strip()
                    mem_mb = _parse_mem(mem_str)
                    self.snapshots.append(ResourceSnapshot(
                        timestamp=time.monotonic(),
                        cpu_percent=cpu,
                        memory_mb=mem_mb,
                        active_sessions=0,
                    ))
            except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, KeyError):
                pass

            self._stop_event.wait(self.interval)


@dataclass
class PsutilMonitor(SystemMonitor):
    """Collects CPU/memory from a local process tree via psutil.

    Tracks the root PID plus all descendants and reports their summed
    CPU% and RSS. livekit-agents (and the AT framework) fork worker
    children per job; if we only sampled the parent, we'd undercount
    actual server load. The Rust livekit-gateway binary has no
    children, so tree-aggregation is a no-op for it — keeping the
    behaviour symmetric across targets.
    """

    pid: int = 0
    include_children: bool = True

    def _collect_loop(self):
        import psutil
        try:
            root = psutil.Process(self.pid)
        except psutil.NoSuchProcess:
            return

        # Prime cpu_percent for the root + initial children
        root.cpu_percent()
        primed = {root.pid: root}
        if self.include_children:
            try:
                for ch in root.children(recursive=True):
                    primed[ch.pid] = ch
                    ch.cpu_percent()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self._stop_event.wait(self.interval)

        while not self._stop_event.is_set():
            cpu_total = 0.0
            mem_total = 0.0

            # Refresh the descendant set every tick — workers come and go.
            current: dict[int, "psutil.Process"] = {}
            try:
                current[root.pid] = root
                if self.include_children:
                    for ch in root.children(recursive=True):
                        current[ch.pid] = ch
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break

            for pid, proc in current.items():
                try:
                    if pid not in primed:
                        # First sample for this new child — prime, skip cpu
                        proc.cpu_percent()
                        primed[pid] = proc
                        mem_total += proc.memory_info().rss / (1024 * 1024)
                        continue
                    cpu_total += proc.cpu_percent()
                    mem_total += proc.memory_info().rss / (1024 * 1024)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    primed.pop(pid, None)
                    continue

            self.snapshots.append(ResourceSnapshot(
                timestamp=time.monotonic(),
                cpu_percent=cpu_total,
                memory_mb=mem_total,
                active_sessions=0,
            ))

            self._stop_event.wait(self.interval)


def find_pid_listening_on_port(port: int) -> int | None:
    """Best-effort lookup for the PID listening on a local TCP port."""
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if (
                conn.laddr
                and conn.laddr.port == port
                and conn.status == psutil.CONN_LISTEN
                and conn.pid
            ):
                return conn.pid
    except Exception:
        pass

    try:
        result = subprocess.run(
            [
                "lsof",
                "-nP",
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fp",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in result.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                return int(line[1:])
    except Exception:
        pass
    return None


def _parse_mem(s: str) -> float:
    """Parse Docker memory string like '123.4MiB' or '1.2GiB' to MB."""
    s = s.strip()
    if s.endswith("GiB"):
        return float(s[:-3]) * 1024
    elif s.endswith("MiB"):
        return float(s[:-3])
    elif s.endswith("KiB"):
        return float(s[:-3]) / 1024
    elif s.endswith("B"):
        return float(s[:-1]) / (1024 * 1024)
    return 0.0
