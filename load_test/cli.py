import argparse
import asyncio
import os
import sys

from loguru import logger

from load_test.profiles import PROFILES
from load_test.report import export_json
from load_test.runner import run_comparison, run_single_target


# Map CLI target names → implementation names used by the runner
TARGET_MAP = {
    "direct": "direct-pipecat",
    "agent-transport": "agent-transport-rust-vad",
    "agent-transport-python-vad": "agent-transport-python-vad",
    "agent-transport-rust-vad": "agent-transport-rust-vad",
    "agent-transport-livekit": "agent-transport-livekit",
}

# Shorthand multi-target groups
TARGET_GROUPS = {
    "both": ["direct-pipecat", "agent-transport-rust-vad"],
    "all": [
        "direct-pipecat",
        "agent-transport-python-vad",
        "agent-transport-rust-vad",
    ],
}


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark Direct Pipecat vs Agent-Transport with Python and Rust VAD backends",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Compare all three implementations in Docker
  python -m load_test.cli --profile medium --target all

  # Compare direct-pipecat and the Rust-VAD transport variant
  python -m load_test.cli --profile medium --target both

  # Capacity run with explicit resource limits
  CPU_LIMIT=4 MEM_LIMIT=10G python -m load_test.cli --profile capacity --target all

  # Run only the LiveKit variant
  python -m load_test.cli --profile smoke --target agent-transport-livekit

  # Connect to already-running servers (preferred for capacity)
  python -m load_test.cli --profile capacity --target all --no-docker \\
      --direct-url ws://localhost:8080 \\
      --at-python-url ws://localhost:8081 \\
      --at-rust-url ws://localhost:8082
        """,
    )
    parser.add_argument(
        "--profile",
        choices=list(PROFILES.keys()),
        default="smoke",
        help="Load profile to run (default: smoke)",
    )
    parser.add_argument(
        "--target",
        choices=[
            "all",
            "both",
            "direct",
            "agent-transport",
            "agent-transport-python-vad",
            "agent-transport-rust-vad",
            "agent-transport-livekit",
        ],
        default="both",
        help="Which server(s) to test (default: both)",
    )
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Skip Docker compose — connect to already-running servers",
    )
    parser.add_argument(
        "--direct-url",
        default=None,
        help="WebSocket URL for direct pipecat server (default: ws://localhost:8080)",
    )
    parser.add_argument(
        "--at-url",
        default=None,
        help="Legacy alias for --at-rust-url (default: ws://localhost:8082)",
    )
    parser.add_argument(
        "--at-python-url",
        default=None,
        help="WebSocket URL for agent-transport Python-VAD server (default: ws://localhost:8081)",
    )
    parser.add_argument(
        "--at-rust-url",
        default=None,
        help="WebSocket URL for agent-transport Rust-VAD server (default: ws://localhost:8082)",
    )
    parser.add_argument(
        "--lk-url",
        default=None,
        help="WebSocket URL(s) for agent-transport-livekit server (comma-separated for "
             "round-robin routing across a horizontal topology; default: ws://localhost:8083)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path for JSON results export",
    )

    args = parser.parse_args()

    # Allow comma-separated URLs for round-robin across a horizontal deploy.
    for attr in ("lk_url", "at_url", "at_python_url", "at_rust_url", "direct_url"):
        val = getattr(args, attr, None)
        if isinstance(val, str) and "," in val:
            setattr(args, attr, [u.strip() for u in val.split(",") if u.strip()])

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

    profile = PROFILES[args.profile]
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    use_docker = not args.no_docker

    logger.info(f"Benchmark profile: {profile.name}")
    logger.info(f"Steps: {[(s.concurrency, f'{s.duration_sec}s') for s in profile.steps]}")

    if profile.name == "capacity" and use_docker and (
        "CPU_LIMIT" not in os.environ or "MEM_LIMIT" not in os.environ
    ):
        logger.warning(
            "Capacity runs in Docker use compose defaults unless CPU_LIMIT and MEM_LIMIT are set. "
            "Use --no-docker on the target host, or set explicit limits to match the real server."
        )

    # Resolve target(s) to run
    if args.target in TARGET_GROUPS:
        targets = TARGET_GROUPS[args.target]
    else:
        targets = [TARGET_MAP.get(args.target, args.target)]

    if len(targets) > 1:
        # Multi-target comparison
        all_summaries, comparisons = asyncio.run(
            run_comparison(
                profile=profile,
                project_dir=project_dir,
                direct_url=args.direct_url,
                at_python_url=args.at_python_url,
                at_rust_url=args.at_rust_url or args.at_url,
                lk_url=args.lk_url,
                targets=targets,
                use_docker=use_docker,
            )
        )
        if args.output:
            flat_summaries = [s for slist in all_summaries.values() for s in slist]
            export_json(flat_summaries, comparisons, args.output)
    else:
        # Single-target run
        impl_name = targets[0]
        url_map = {
            "direct-pipecat": (
                args.direct_url or "ws://localhost:8080",
                "direct-pipecat",
                8080,
            ),
            "agent-transport-python-vad": (
                args.at_python_url or "ws://localhost:8081",
                "agent-transport-python-vad",
                8081,
            ),
            "agent-transport-rust-vad": (
                args.at_rust_url or args.at_url or "ws://localhost:8082",
                "agent-transport-rust-vad",
                8082,
            ),
            "agent-transport-livekit": (
                args.lk_url or "ws://localhost:8083",
                "agent-transport-livekit",
                8083,
            ),
        }
        url, service_name, service_port = url_map[impl_name]
        # Horizontal mode (list of URLs) doesn't map to a single compose
        # service, so skip the fresh-container-per-step recreate logic —
        # the caller is responsible for bringing up the 4+ instances.
        is_horizontal = isinstance(url, list)
        single_project_dir = project_dir if (use_docker and not is_horizontal) else None
        summaries = asyncio.run(
            run_single_target(
                url=url,
                implementation=impl_name,
                profile=profile,
                service_name=service_name if (use_docker and not is_horizontal) else None,
                service_port=service_port if (use_docker and not is_horizontal) else None,
                project_dir=single_project_dir,
            )
        )
        if args.output:
            export_json(summaries, [], args.output)


if __name__ == "__main__":
    main()
