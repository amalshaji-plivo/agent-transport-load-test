#!/bin/bash
# AT + LiveKit capacity sweep on agent-transport 0.2.0.
# Config: multilingual turn detector + Python Silero VAD (NO Rust VAD), PROFILE=0
# (no py-spy) so docker-stats CPU/mem are uncontaminated capacity numbers.
#
# Sweeps two single-container hardware profiles to find the per-profile
# concurrency ceiling. Per-step fresh container (handled by the CLI's
# fresh_container_per_step) so every level starts from a cold process.
#
# Progress: per-step summaries stream into results-0.2.0-lk-td/<name>.out;
# profile-level markers go to results-0.2.0-lk-td/progress.log.
set -uo pipefail

D=/Users/amal.shaji/Workspace/plivo-labs/agent-transport-load-test
cd "$D"
RESULTS="$D/results-0.2.0-lk-td"
PROG="$RESULTS/progress.log"
mkdir -p "$RESULTS"
CONTAINER=agent-transport-load-test-agent-transport-livekit-1
PYBIN="$D/.venv/bin/python"

# Config under test: turn detector ON, Rust VAD OFF (Python VAD auto-loads),
# clean capacity measurement (no py-spy).
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false ENABLE_PY_VAD=false PROFILE=0

log() { echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }

run_profile() {
  local name="$1" cpu="$2" mem="$3" profile="$4"
  export CPU_LIMIT="$cpu" MEM_LIMIT="$mem"
  log "=== START $name : cpu=$cpu mem=$mem profile=$profile (turn-detector + python-VAD) ==="
  docker compose down >/dev/null 2>&1 || true
  "$PYBIN" -m load_test.cli --profile "$profile" --target agent-transport-livekit \
      --docker-container "$CONTAINER" \
      --output "$RESULTS/$name.json" >> "$RESULTS/$name.out" 2>&1
  local rc=$?
  docker compose down >/dev/null 2>&1 || true
  log "=== DONE  $name (cli exit=$rc) -> $name.json ==="
}

log "###### AT+LiveKit 0.2.0 capacity sweep BEGIN ######"
run_profile "1cpu-2gb" "1.0" "2G" "at_lk_1cpu"
run_profile "4cpu-8gb" "4.0" "8G" "at_lk_4cpu"
log "###### SWEEP COMPLETE ######"
