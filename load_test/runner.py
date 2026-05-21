"""Main orchestrator: manages Docker lifecycle and runs load tests."""

import asyncio
import os
import subprocess
import sys
import time
from urllib.parse import urlparse

from loguru import logger

from load_test.client.session_manager import SessionManager
from load_test.metrics.aggregator import (
    ComparisonResult,
    RunSummary,
    compare_runs,
    summarize_run,
)
from load_test.metrics.collector import TestRunMetrics
from load_test.metrics.system_monitor import (
    DockerStatsMonitor,
    PsutilMonitor,
    ResourceSnapshot,
    find_pid_listening_on_port,
)
from load_test.profiles import LoadProfile, LoadStep
from load_test.report import print_comparison, print_single_summary


DIRECT_CONTAINER = "agent-transport-load-test-direct-pipecat-1"
AT_PY_CONTAINER = "agent-transport-load-test-agent-transport-python-vad-1"
AT_RUST_CONTAINER = "agent-transport-load-test-agent-transport-rust-vad-1"
LKG_CONTAINER = "agent-transport-load-test-livekit-gateway-1"
LKP_CONTAINER = "agent-transport-load-test-livekit-python-1"

# Compose service names (distinct from container names, which have -1 suffix)
DIRECT_SERVICE = "direct-pipecat"
AT_PY_SERVICE = "agent-transport-python-vad"
AT_RUST_SERVICE = "agent-transport-rust-vad"
LKG_SERVICE = "livekit-gateway"
LKP_SERVICE = "livekit-python"

DIRECT_PORT = 8080
AT_PY_PORT = 8081
AT_RUST_PORT = 8082
LKG_PORT = 8084   # Plivo-facing port for the livekit-gateway server
LKP_PORT = 7880   # LiveKit Server (SFU) signaling port — psutil monitor binds here

# Per-implementation output pacing interval (seconds between adjacent frames on
# the wire). Used to derive `audible_silence_gap` and to pick the rate-based
# survivorship threshold in summarize_run.
#   direct-pipecat:  pipecat batches at 40 ms by default
#   agent-transport (both VAD variants): Rust tokio::time::interval at 20 ms
#   livekit-gateway: Rust tokio pacer at 20 ms
#   livekit-python:  WebRTC Opus at 20 ms per packet
PACING_INTERVAL_BY_IMPL: dict[str, float] = {
    "direct-pipecat": 0.040,
    "agent-transport-python-vad": 0.020,
    "agent-transport-rust-vad": 0.020,
    "livekit-gateway": 0.020,
    "livekit-python": 0.020,
}


def docker_compose_up(project_dir: str) -> None:
    """Build and start Docker containers."""
    logger.info("Building and starting Docker containers...")
    subprocess.run(
        ["docker", "compose", "up", "-d", "--build", "--wait"],
        cwd=project_dir,
        check=True,
    )
    logger.info("Docker containers are up and healthy")
    _verify_cpu_limits(project_dir)


def _verify_cpu_limits(project_dir: str) -> None:
    """Log the enforced CPU/memory limits per container.

    CPU limits via Compose `deploy.resources` only apply under Swarm. On
    OrbStack/Docker Desktop you need the root-level `cpus:` + `mem_limit:`
    fields. This function logs what was actually set on the containers so
    the user can verify enforcement before interpreting benchmark results.
    """
    import json as _json

    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return

    requested_cpus = os.getenv("CPU_LIMIT", "1.0")
    requested_mem = os.getenv("MEM_LIMIT", "512M")
    logger.info(f"Requested limits: cpus={requested_cpus} mem={requested_mem}")

    for line in result.stdout.splitlines():
        try:
            info = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        name = info.get("Name", "?")
        try:
            inspect = subprocess.run(
                ["docker", "inspect", name, "--format",
                 "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}"],
                capture_output=True, text=True, check=True,
            )
            nano_cpus_str, mem_str = inspect.stdout.strip().split()
            nano_cpus = int(nano_cpus_str)
            mem = int(mem_str)
            cpus = nano_cpus / 1e9 if nano_cpus > 0 else 0.0
            mem_mb = mem / (1024 * 1024) if mem > 0 else 0.0
            cpu_enforced = "ENFORCED" if nano_cpus > 0 else "UNLIMITED!"
            mem_enforced = "ENFORCED" if mem > 0 else "UNLIMITED!"
            logger.info(
                f"  {name}: cpus={cpus:.2f} ({cpu_enforced}) "
                f"mem={mem_mb:.0f}MB ({mem_enforced})"
            )
        except Exception:
            pass


def docker_compose_down(project_dir: str) -> None:
    """Stop and remove Docker containers."""
    logger.info("Stopping Docker containers...")
    subprocess.run(
        ["docker", "compose", "down"],
        cwd=project_dir,
        check=True,
    )


def docker_compose_restart_service(
    project_dir: str, service: str, port: int, settle_sec: float = 5.0
) -> None:
    """Restart one service to give it a fresh process between load steps.

    Uses `docker compose up -d --force-recreate` (instead of `restart`) so the
    container is recreated from the image. This guarantees pristine state — no
    leftover file descriptors, ONNX caches, memory fragmentation, or zombie
    tasks from the prior step. After recreation we wait for the healthcheck
    and give an extra `settle_sec` for prewarm (VAD model load, etc.).
    """
    logger.info(f"Recreating container '{service}' for fresh state...")
    subprocess.run(
        ["docker", "compose", "up", "-d", "--force-recreate", "--wait", service],
        cwd=project_dir,
        check=True,
    )
    # Wait for the WebSocket port to accept connections on top of compose's
    # healthcheck (which only checks port connect — the Python prewarm may
    # still be running after that succeeds).
    if not wait_for_port("localhost", port, timeout=30):
        logger.error(f"{service} not reachable on port {port} after restart")
        raise RuntimeError(f"{service} not reachable on port {port} after restart")
    if settle_sec > 0:
        logger.info(f"Settling {settle_sec:.1f}s after restart (prewarm)...")
        time.sleep(settle_sec)


def wait_for_port(host: str, port: int, timeout: float = 30) -> bool:
    """Wait until a TCP port is accepting connections."""
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    return False


async def run_load_step(
    url: str,
    implementation: str,
    step: LoadStep,
    container_name: str | None = None,
    monitor_port: int | None = None,
) -> tuple[TestRunMetrics, list[ResourceSnapshot]]:
    """Run a single load step and collect metrics."""
    warmup_info = f" | warmup={step.warmup_sec}s" if step.warmup_sec > 0 else ""
    logger.info(
        f"Running {implementation} | concurrency={step.concurrency} | duration={step.duration_sec}s{warmup_info}"
    )

    # Start resource monitor
    monitor = None
    if container_name:
        monitor = DockerStatsMonitor(container_name=container_name)
        monitor.start()
    elif monitor_port:
        pid = find_pid_listening_on_port(monitor_port)
        if pid:
            monitor = PsutilMonitor(pid=pid)
            monitor.start()
        else:
            logger.warning(
                f"Could not find a local process listening on port {monitor_port}; "
                "CPU/memory stats will be unavailable for this run"
            )

    # Run sessions
    manager = SessionManager(
        url=url,
        implementation=implementation,
        concurrency=step.concurrency,
        session_duration=step.duration_sec,
        ramp_delay=step.ramp_delay,
        warmup_sec=step.warmup_sec,
    )
    run_metrics = await manager.run()

    # Stop resource monitor
    snapshots: list[ResourceSnapshot] = []
    if monitor:
        snapshots = monitor.stop()

    return run_metrics, snapshots


async def run_profile_against_target(
    url: str,
    implementation: str,
    profile: LoadProfile,
    container_name: str | None = None,
    monitor_port: int | None = None,
    service_name: str | None = None,
    service_port: int | None = None,
    project_dir: str | None = None,
) -> list[RunSummary]:
    """Run all steps in a profile against one target. Returns per-step summaries.

    If `profile.fresh_container_per_step` is set and we have the service
    metadata (service_name, service_port, project_dir), the container is
    recreated between steps so each step sees a cold server process.
    """
    summaries: list[RunSummary] = []
    can_restart = (
        profile.fresh_container_per_step
        and service_name is not None
        and service_port is not None
        and project_dir is not None
    )

    if profile.fresh_container_per_step and not can_restart:
        logger.warning(
            f"Profile '{profile.name}' requests fresh_container_per_step "
            f"but service_name/service_port/project_dir are not available. "
            f"Proceeding with shared-container steps (warm state will bleed)."
        )

    for i, step in enumerate(profile.steps):
        # Recreate the container before EVERY step so each measurement starts
        # from a cold server process. Includes the first step so the starting
        # state is the same regardless of how the caller brought containers up.
        if can_restart:
            docker_compose_restart_service(
                project_dir, service_name, service_port, settle_sec=5.0
            )

        run_metrics, snapshots = await run_load_step(
            url=url,
            implementation=implementation,
            step=step,
            container_name=container_name,
            monitor_port=monitor_port,
        )
        pacing = PACING_INTERVAL_BY_IMPL.get(implementation, 0.020)
        summary = summarize_run(
            run_metrics,
            snapshots,
            pacing_interval_sec=pacing,
        )
        print_single_summary(summary)
        summaries.append(summary)

        # Brief pause between steps (only meaningful when we're NOT about to
        # spend ~10-15s restarting the container anyway).
        if step != profile.steps[-1] and not can_restart:
            logger.info("Pausing 3s between load steps...")
            await asyncio.sleep(3)

    return summaries


async def run_comparison(
    profile: LoadProfile,
    project_dir: str,
    direct_url: str | None = None,
    at_python_url: str | None = None,
    at_rust_url: str | None = None,
    lkg_url: str | None = None,
    lkp_url: str | None = None,
    targets: list[str] | None = None,
    use_docker: bool = True,
) -> tuple[dict[str, list[RunSummary]], list[ComparisonResult]]:
    """Run the full comparison: selected implementations, all profile steps.

    Args:
        targets: list of implementation names to run. Defaults to
                 ["direct-pipecat", "agent-transport-rust-vad"].
    """
    if targets is None:
        targets = ["direct-pipecat", "agent-transport-rust-vad"]

    if use_docker:
        docker_compose_up(project_dir)

    # Map implementation name → (url, container, port, service, display)
    target_config = {
        "direct-pipecat": (
            direct_url or f"ws://localhost:{DIRECT_PORT}",
            DIRECT_CONTAINER,
            DIRECT_PORT,
            DIRECT_SERVICE,
            "Direct Pipecat",
        ),
        "agent-transport-python-vad": (
            at_python_url or f"ws://localhost:{AT_PY_PORT}",
            AT_PY_CONTAINER,
            AT_PY_PORT,
            AT_PY_SERVICE,
            "Agent-Transport + Python VAD",
        ),
        "agent-transport-rust-vad": (
            at_rust_url or f"ws://localhost:{AT_RUST_PORT}",
            AT_RUST_CONTAINER,
            AT_RUST_PORT,
            AT_RUST_SERVICE,
            "Agent-Transport + Rust VAD",
        ),
        "livekit-gateway": (
            lkg_url or f"ws://localhost:{LKG_PORT}",
            LKG_CONTAINER,
            LKG_PORT,
            LKG_SERVICE,
            "livekit-gateway",
        ),
        "livekit-python": (
            lkp_url or f"livekit://localhost:{LKP_PORT}",
            LKP_CONTAINER,
            LKP_PORT,
            LKP_SERVICE,
            "livekit-python (stock LiveKit SFU)",
        ),
    }

    try:
        # Verify ports are ready
        if use_docker:
            for impl_name in targets:
                _, _, port, _, display_name = target_config[impl_name]
                if not wait_for_port("localhost", port, timeout=60):
                    logger.error(f"{display_name} not ready on port {port}")
                    sys.exit(1)

        all_summaries: dict[str, list[RunSummary]] = {}

        for i, impl_name in enumerate(targets):
            url, container, port, service, display_name = target_config[impl_name]

            if i > 0:
                logger.info(f"Pausing 5s before {display_name} test...")
                await asyncio.sleep(5)

            logger.info("=" * 60)
            logger.info(f"Testing: {display_name}")
            logger.info("=" * 60)

            summaries = await run_profile_against_target(
                url=url,
                implementation=impl_name,
                profile=profile,
                container_name=container if use_docker else None,
                monitor_port=None if use_docker else _port_for_local_monitor(url, impl_name),
                service_name=service if use_docker else None,
                service_port=port if use_docker else None,
                project_dir=project_dir if use_docker else None,
            )
            all_summaries[impl_name] = summaries

        # Build pairwise comparisons (first target is baseline)
        comparisons: list[ComparisonResult] = []
        baseline_name = targets[0]
        for candidate_name in targets[1:]:
            baseline_summaries = all_summaries[baseline_name]
            candidate_summaries = all_summaries[candidate_name]
            for bs, cs in zip(baseline_summaries, candidate_summaries):
                comp = compare_runs(baseline=bs, candidate=cs)
                comparisons.append(comp)
                print_comparison(comp)

        return all_summaries, comparisons

    finally:
        if use_docker:
            docker_compose_down(project_dir)


async def run_single_target(
    url: str,
    implementation: str,
    profile: LoadProfile,
    container_name: str | None = None,
    service_name: str | None = None,
    service_port: int | None = None,
    project_dir: str | None = None,
) -> list[RunSummary]:
    """Run load test against a single target."""
    return await run_profile_against_target(
        url=url,
        implementation=implementation,
        profile=profile,
        container_name=container_name,
        monitor_port=None if container_name else _port_for_local_monitor(url, implementation),
        service_name=service_name,
        service_port=service_port,
        project_dir=project_dir,
    )


# For impls whose bench URL doesn't map 1:1 to the process under test, point
# the host-side psutil monitor at the *agent process* (not the transport
# layer). This isolates "agent cost" — the livekit-agents Python pipeline
# we're load-testing — from the surrounding transport's cost (LiveKit SFU
# for lkp, livekit-gateway Rust binary for lkg), both of which exist for
# both targets in some form. PsutilMonitor walks the process tree, so the
# AgentServer's per-job forked children are included.
_AGENT_MONITOR_PORT_BY_IMPL: dict[str, int] = {
    "livekit-python": 8281,   # AGENT_SERVER_HTTP_PORT in livekit_python_server.py
    "livekit-gateway": 8181,  # AGENT_SERVER_HTTP_PORT in livekit_gateway_server.py
}


def _port_for_local_monitor(
    url: str | list[str], implementation: str | None = None
) -> int | None:
    """Return the port for localhost targets so we can attach a psutil monitor.

    For multi-URL round-robin (horizontal topology), the harness has no single
    process to monitor on the host side — resource stats come from
    ``docker stats`` on each container instead. Returns None in that case.
    """
    if isinstance(url, list):
        return None
    if implementation in _AGENT_MONITOR_PORT_BY_IMPL:
        return _AGENT_MONITOR_PORT_BY_IMPL[implementation]
    parsed = urlparse(url)
    # livekit:// URLs point at a LiveKit SFU and not the agent process, so
    # the URL port is useless for monitoring; fall back to None.
    if parsed.scheme in ("livekit", "livekit+ws", "livekit+wss", "livekit-tls"):
        return None
    if parsed.hostname not in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return None
    return parsed.port
