#!/bin/bash
# AT+LiveKit 0.2.0 — QoE-gated re-run. Capture ALL signals per step:
# delivery, CPU (docker-stats, PROFILE=0), mem, silence p90, and first-response
# (first_frame_latency = connect->first bot audio, client-side, zero server cost).
# Gate: delivery 100% AND CPU mean <80% cap AND silence p90 <=5ms AND
#       first-response near its low-load floor (no load-induced inflation).
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test
cd "$WORK_DIR"
export COMPOSE_PROJECT_NAME=agent-transport-load-test
export COMPOSE_FILE=$WORK_DIR/docker-compose.yml:$WORK_DIR/docker-compose.mocks.yml
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false ENABLE_PY_VAD=false
export MIN_ENDPOINTING_DELAY=0.4 MAX_ENDPOINTING_DELAY=1.5
C=agent-transport-load-test-agent-transport-livekit-1
RESULTS=$HOME/bench-out/at-lk-qoe; mkdir -p "$RESULTS"; PROG=$RESULTS/progress.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }

run(){ # profile cpu mem tag
  export CPU_LIMIT=$2 MEM_LIMIT=$3
  log "===== $4 ($1 cpu=$2 mem=$3) begin ====="
  docker compose down -v >/dev/null 2>&1 || true
  docker compose up -d --force-recreate --wait agent-transport-livekit mock-services >>"$PROG" 2>&1
  sleep 8
  docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 \
    bench-client:latest \
    python -m load_test.cli --profile "$1" --target agent-transport-livekit \
      --no-docker --at-livekit-url ws://localhost:8083 --docker-container "$C" \
      --output "/work/_q_${4}.json" >>"$RESULTS/$4.out" 2>&1
  [ -s "$WORK_DIR/_q_${4}.json" ] && mv "$WORK_DIR/_q_${4}.json" "$RESULTS/$4.json"
  log "===== $4 end ====="
}

log "###### 1 vCPU / 2 GB ######"
for c in 2 4 6 8 10 14 20; do run c$c 1.0 2G 1cpu-c$c; done
log "###### 4 vCPU / 8 GB ######"
run c8 4.0 8G 4cpu-c8
run c16 4.0 8G 4cpu-c16
run c25 4.0 8G 4cpu-c25
run c40 4.0 8G 4cpu-c40
run c50_atlk 4.0 8G 4cpu-c50
run c60_atlk 4.0 8G 4cpu-c60
docker compose down -v >/dev/null 2>&1 || true
log "###### QOE SWEEP DONE ######"
