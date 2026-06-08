#!/bin/bash
# py-spy CPU profile at each of the 3 ceiling configs. SHAPE only (PROFILE run; do
# not trust CPU magnitude from these — that's the PROFILE=0 5x numbers). For each
# config: start the bench load, let it warm, py-spy record 25s --subprocesses, save
# speedscope, then parse a top-self-time breakdown.
#   AT+pipecat c14 (1 vCPU, py-VAD), AT+livekit c10 (1 vCPU, TD), Python c30 (4 vCPU, 20ms)
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test; cd "$WORK_DIR"
export COMPOSE_PROJECT_NAME=agent-transport-load-test
export COMPOSE_FILE=$WORK_DIR/docker-compose.yml:$WORK_DIR/docker-compose.mocks.yml
RES=$HOME/bench-out/prof-3config; mkdir -p "$RES"; PROG=$RES/progress.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }

prof(){ # tag svc target urlflag url profile cpu mem  [extra compose env already exported]
  local TAG=$1 SVC=$2 TGT=$3 FLAG=$4 URL=$5 PROF=$6 CPU=$7 MEM=$8
  local C=agent-transport-load-test-${SVC}-1
  export CPU_LIMIT=$CPU MEM_LIMIT=$MEM
  log "===== profile $TAG ($PROF, $CPU vCPU) ====="
  docker compose down -v >/dev/null 2>&1 || true
  docker compose up -d --force-recreate --wait $SVC mock-services >>"$PROG" 2>&1
  sleep 8
  docker run -d --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 --name prof-cli-$TAG \
    bench-client:latest \
    python -m load_test.cli --profile "$PROF" --target "$TGT" --no-docker \
      $FLAG "$URL" --docker-container "$C" --output /work/_pf_$TAG.json >/dev/null 2>&1
  sleep 12   # let load reach steady state
  log "  py-spy record 25s on $TAG ..."
  docker exec $C py-spy record --pid 1 --subprocesses --rate 50 --duration 25 \
    --format speedscope --output /tmp/$TAG.speedscope.json >>"$PROG" 2>&1 || log "  py-spy rc=$?"
  docker cp $C:/tmp/$TAG.speedscope.json "$RES/$TAG.speedscope.json" 2>/dev/null && log "  saved $TAG.speedscope.json"
  docker rm -f prof-cli-$TAG >/dev/null 2>&1 || true
  docker compose down -v >/dev/null 2>&1 || true
  log "  --- $TAG breakdown ---"
  python3 /root/parse_speedscope.py "$RES/$TAG.speedscope.json" 12 2>>"$PROG" | tee -a "$PROG" || log "  parse failed"
}

# AT+livekit env (only consumed by that service)
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false ENABLE_PY_VAD=false
export MIN_ENDPOINTING_DELAY=0.4 MAX_ENDPOINTING_DELAY=1.5
# Python frame size + workers
export WORKERS=4 AUDIO_OUT_10MS_CHUNKS=2 DIRECT_ENABLE_VAD=true

prof at-pipecat-c14 agent-transport-python-vad agent-transport-python-vad --at-python-url ws://localhost:8081 c14 1.0 2G
prof at-livekit-c10 agent-transport-livekit    agent-transport-livekit    --at-livekit-url ws://localhost:8083 c10 1.0 2G
prof py-pipecat-c30 direct-pipecat             direct                     --direct-url     ws://localhost:8080 c30 4.0 8G

log "###### PROFILE_3CONFIG DONE ######"
