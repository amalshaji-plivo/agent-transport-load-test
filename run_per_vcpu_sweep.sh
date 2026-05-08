#!/usr/bin/env bash
# Per-vCPU sweep: 1 CPU / 4 GB / VAD on / smart-turn ML on.
# Runs the three configurations sequentially. Each step recreates the container
# so results are independent.
set -uo pipefail

cd "$(dirname "$0")"

export COMPOSE_FILE=docker-compose.yml:docker-compose.vad-1cpu.yml
export CPU_LIMIT=1.0
export MEM_LIMIT=4G
export ENABLE_TURN_DETECTOR=true
PYTHON=.venv/bin/python
PROFILE=per_vcpu_2_20
OUTDIR=results-per-vcpu

run_one() {
  local target="$1" url_flag="$2" url="$3" tag="$4"
  echo "=========================================================="
  echo "= START $tag (target=$target url=$url)"
  echo "=========================================================="
  docker compose up -d --wait "$target" 2>&1 | tail -3
  $PYTHON -m load_test.cli --profile "$PROFILE" --target "$target" "$url_flag" "$url" \
      --output "$OUTDIR/$tag.json" 2>&1 | tee "$OUTDIR/$tag.log"
  local ec=$?
  docker compose down 2>&1 | tail -2
  echo "=== DONE $tag exit=$ec ==="
  return $ec
}

run_one direct                      --direct-url   ws://localhost:8080 direct-pipecat-1cpu-vad-td
run_one agent-transport-rust-vad    --at-rust-url  ws://localhost:8082 at-pipecat-1cpu-vad-td
run_one agent-transport-livekit     --lk-url       ws://localhost:8083 at-livekit-1cpu-vad-td

echo "ALL DONE"
