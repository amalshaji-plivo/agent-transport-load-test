#!/bin/bash
# 4-CONCURRENT validation: run 4 independent 1-vCPU agent containers at once,
# each at its per-core ceiling, 4 bench clients in parallel. Turns the ~56/~40
# ×4 PROJECTION into a MEASURED aggregate. Watches mock-services CPU to catch
# shared-backend contention. All host-networked; agents cgroup-capped 1 vCPU/2 GB.
#   AT+pipecat : 4 x c14  (py-VAD)   ports 8101-8104
#   AT+livekit : 4 x c10  (TD)       ports 8111-8114 (http 8211-8214)
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test; cd "$WORK_DIR"
RES=$HOME/bench-out/4concurrent; mkdir -p "$RES"; PROG=$RES/progress.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }
IMG_PV=agent-transport-load-test-agent-transport-python-vad
IMG_LK=agent-transport-load-test-agent-transport-livekit
IMG_MOCK=agent-transport-load-test/mock-services:latest

cleanup(){ docker rm -f mock4 a1 a2 a3 a4 2>/dev/null >/dev/null; }
wait_port(){ for _ in $(seq 1 60); do (echo >/dev/tcp/localhost/$1) 2>/dev/null && return 0; sleep 1; done; return 1; }

# args: armname profile target urlflag img  then 4 "WS_PORT|EXTRA_ENV" specs
run_arm(){
  local ARM=$1 PROF=$2 TGT=$3 FLAG=$4 IMG=$5; shift 5
  local specs=("$@")
  cleanup
  log "===== $ARM : 4 x $PROF concurrent ====="
  docker run -d --name mock4 --network host $IMG_MOCK >>"$PROG" 2>&1
  wait_port 9000 || { log "mock-services failed"; return 1; }
  # launch 4 agents
  local k=1
  for spec in "${specs[@]}"; do
    local PORT=${spec%%|*}; local ENVS=${spec#*|}
    docker run -d --name a$k --network host --cpus=1.0 --memory=2g \
      -e MOCK_SERVICES_HOST=localhost:9000 $ENVS $IMG >>"$PROG" 2>&1
    k=$((k+1))
  done
  log "waiting for 4 agents to be ready..."
  for spec in "${specs[@]}"; do wait_port "${spec%%|*}" || log "WARN port ${spec%%|*} not ready"; done
  sleep 10   # model warmup
  # launch 4 bench clients in parallel
  k=1
  for spec in "${specs[@]}"; do
    local PORT=${spec%%|*}
    docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 bench-client:latest \
      python -m load_test.cli --profile "$PROF" --target "$TGT" --no-docker \
        --${FLAG} ws://localhost:$PORT --docker-container a$k \
        --output "/work/_4c_${ARM}_a${k}.json" >>"$RES/${ARM}_a${k}.out" 2>&1 &
    k=$((k+1))
  done
  # sample mock-services CPU while clients run
  ( for _ in $(seq 1 20); do docker stats --no-stream --format '{{.CPUPerc}}' mock4 2>/dev/null; sleep 3; done ) >"$RES/${ARM}_mockcpu.log" 2>&1 &
  wait  # all 4 clients
  for k in 1 2 3 4; do [ -s "$WORK_DIR/_4c_${ARM}_a${k}.json" ] && mv "$WORK_DIR/_4c_${ARM}_a${k}.json" "$RES/${ARM}_a${k}.json"; done
  log "===== $ARM done ====="
  cleanup
}

PV_VAD='-e ENABLE_VAD=true -e VAD_BACKEND=python -e VAD_POOL_SIZE=8'
LK_ENV='-e ENABLE_TURN_DETECTOR=true -e ENABLE_VAD=false -e ENABLE_PY_VAD=false -e MIN_ENDPOINTING_DELAY=0.4 -e MAX_ENDPOINTING_DELAY=1.5'

run_arm at-pipecat c14 agent-transport-python-vad at-python-url "$IMG_PV" \
  "8101|-e WS_PORT=8101 $PV_VAD" "8102|-e WS_PORT=8102 $PV_VAD" \
  "8103|-e WS_PORT=8103 $PV_VAD" "8104|-e WS_PORT=8104 $PV_VAD"

run_arm at-livekit c10 agent-transport-livekit at-livekit-url "$IMG_LK" \
  "8111|-e WS_PORT=8111 -e HTTP_PORT=8211 $LK_ENV" "8112|-e WS_PORT=8112 -e HTTP_PORT=8212 $LK_ENV" \
  "8113|-e WS_PORT=8113 -e HTTP_PORT=8213 $LK_ENV" "8114|-e WS_PORT=8114 -e HTTP_PORT=8214 $LK_ENV"

log "###### 4CONCURRENT DONE ######"
