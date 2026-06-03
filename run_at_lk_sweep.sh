#!/bin/bash
# AT + LiveKit capacity sweep on agent-transport 0.2.0 (turn detector + Python VAD).
# Portable (Mac or EC2). Run inside tmux/nohup on EC2 so it survives disconnects.
#
#   bash run_at_lk_sweep.sh                  # both profiles: 1 vCPU/2G then 4 vCPU/8G
#   PROFILES=4cpu bash run_at_lk_sweep.sh    # only the 4 vCPU profile
#   PROFILES=1cpu bash run_at_lk_sweep.sh    # only the 1 vCPU profile
#
# Env knobs (defaults match the validated config): TURN_SILENCE_MS, MIN/MAX_ENDPOINTING_DELAY,
# PYBIN, RESULTS_DIR. PROFILE stays 0 (clean CPU/mem; never run py-spy in a capacity sweep).
set +e   # never let one failed step abort the whole sweep

D="$(cd "$(dirname "$0")" && pwd)"
export COMPOSE_PROJECT_NAME="$(basename "$D")"
RESULTS="${RESULTS_DIR:-$D/results-0.2.0-lk-td}"; PROG="$RESULTS/progress.log"; mkdir -p "$RESULTS"
C="${COMPOSE_PROJECT_NAME}-agent-transport-livekit-1"
PY="${PYBIN:-$D/.venv/bin/python}"

export PYTHONPATH="$D"
export COMPOSE_FILE="$D/docker-compose.yml:$D/docker-compose.mocks.yml"
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false ENABLE_PY_VAD=false PROFILE=0 METRICS_LOG=false
export TURN_SILENCE_MS="${TURN_SILENCE_MS:-4000}"
export MIN_ENDPOINTING_DELAY="${MIN_ENDPOINTING_DELAY:-0.4}" MAX_ENDPOINTING_DELAY="${MAX_ENDPOINTING_DELAY:-1.5}"

lg(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }
runp(){  # name cpu_limit mem_limit profile
  export CPU_LIMIT="$2" MEM_LIMIT="$3"
  lg "=== START $1 (cpu=$2 mem=$3 profile=$4) ==="
  docker compose down >/dev/null 2>&1
  "$PY" -m load_test.cli --profile "$4" --target agent-transport-livekit \
     --docker-container "$C" --output "$RESULTS/$1.json" >> "$RESULTS/$1.out" 2>&1
  lg "=== DONE $1 (cli rc=$?) ==="
  docker compose down >/dev/null 2>&1
}

P="${PROFILES:-1cpu 4cpu}"
lg "###### AT+LiveKit 0.2.0 capacity sweep BEGIN (PROFILES=$P, container=$C) ######"
[[ "$P" == *1cpu* ]] && runp 1cpu-2gb 1.0 2G at_lk_1cpu
[[ "$P" == *4cpu* ]] && runp 4cpu-8gb 4.0 8G at_lk_4cpu
lg "###### SWEEP COMPLETE ######"
