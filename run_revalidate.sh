#!/usr/bin/env bash
# Revalidation runs: 4 vCPU / 10 GB total budget, no VAD, no TD.
# Each setup runs 5 back-to-back at the headline-table concurrency. Container(s)
# recreated between runs for independence.
set -uo pipefail
cd "$(dirname "$0")"

PYTHON=.venv/bin/python
OUTDIR=results-revalidate
mkdir -p "$OUTDIR"

setup="${1:-}"

case "$setup" in
  direct)
    export COMPOSE_FILE=docker-compose.yml:docker-compose.novad.yml
    export CPU_LIMIT=4
    export MEM_LIMIT=10G
    echo "= direct-pipecat vertical 4 CPU / 10 GB | profile=direct_c50 | runs=5"
    docker compose up -d --wait direct-pipecat 2>&1 | tail -3
    for i in 1 2 3 4 5; do
      echo "--- direct run $i ---"
      $PYTHON -m load_test.cli --profile direct_c50 --target direct \
          --output "$OUTDIR/direct_c50_run${i}.json" 2>&1 \
          | tee "$OUTDIR/direct_c50_run${i}.log"
    done
    docker compose down 2>&1 | tail -3
    ;;

  at-pipecat)
    # 4 instances on 8091-8094, 1 CPU + 2.5 GB each (= 4 CPU / 10 GB total)
    export COMPOSE_FILE=docker-compose.yml:docker-compose.horizontal.yml
    export MEM_LIMIT=2500M
    URLS="ws://localhost:8091,ws://localhost:8092,ws://localhost:8093,ws://localhost:8094"
    HORIZ_SVCS=(agent-transport-python-vad-horiz-1 agent-transport-python-vad-horiz-2 \
                 agent-transport-python-vad-horiz-3 agent-transport-python-vad-horiz-4)
    echo "= AT + pipecat horiz 4 × (1 CPU / 2.5 GB) | profile=at_horiz_c110 | runs=5"
    for i in 1 2 3 4 5; do
      echo "--- at-pipecat run $i (recreating 4 horiz instances) ---"
      docker compose up -d --force-recreate --wait "${HORIZ_SVCS[@]}" 2>&1 | tail -4
      sleep 5  # prewarm
      $PYTHON -m load_test.cli --profile at_horiz_c110 --target agent-transport-rust-vad \
          --at-rust-url "$URLS" --no-docker \
          --output "$OUTDIR/at_pipecat_c110_run${i}.json" 2>&1 \
          | tee "$OUTDIR/at_pipecat_c110_run${i}.log"
    done
    docker compose down 2>&1 | tail -3
    ;;

  at-livekit)
    export COMPOSE_FILE=docker-compose.yml:docker-compose.livekit-horizontal.yml
    export MEM_LIMIT=2500M
    URLS="ws://localhost:8091,ws://localhost:8092,ws://localhost:8093,ws://localhost:8094"
    HORIZ_SVCS=(agent-transport-livekit-horiz-1 agent-transport-livekit-horiz-2 \
                 agent-transport-livekit-horiz-3 agent-transport-livekit-horiz-4)
    echo "= AT + LiveKit horiz 4 × (1 CPU / 2.5 GB) | profile=lk_horiz_c140 | runs=5"
    for i in 1 2 3 4 5; do
      echo "--- at-livekit run $i (recreating 4 horiz instances) ---"
      docker compose up -d --force-recreate --wait "${HORIZ_SVCS[@]}" 2>&1 | tail -4
      sleep 5
      $PYTHON -m load_test.cli --profile lk_horiz_c140 --target agent-transport-livekit \
          --lk-url "$URLS" --no-docker \
          --output "$OUTDIR/at_livekit_c140_run${i}.json" 2>&1 \
          | tee "$OUTDIR/at_livekit_c140_run${i}.log"
    done
    docker compose down 2>&1 | tail -3
    ;;

  *)
    echo "usage: $0 {direct|at-pipecat|at-livekit}" >&2
    exit 2
    ;;
esac

echo "=== DONE $setup ==="
