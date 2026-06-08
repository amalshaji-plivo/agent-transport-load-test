#!/bin/bash
# py-spy CPU profile for AT+LiveKit at the sweet spot. PROFILE=1 (single py-spy,
# speedscope) — SHAPE only; CPU magnitude per docker-stats (PROFILE=0 runs).
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test
cd "$WORK_DIR"
export COMPOSE_PROJECT_NAME=agent-transport-load-test
export COMPOSE_FILE=$WORK_DIR/docker-compose.yml:$WORK_DIR/docker-compose.mocks.yml
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false ENABLE_PY_VAD=false
export MIN_ENDPOINTING_DELAY=0.4 MAX_ENDPOINTING_DELAY=1.5
C=agent-transport-load-test-agent-transport-livekit-1
RESULTS=$HOME/bench-out/at-lk-profile; mkdir -p "$RESULTS"; PROG=$RESULTS/progress.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }

prof(){ # profile cpu mem tag
  export CPU_LIMIT=$2 MEM_LIMIT=$3
  log "===== profile $4 ($1 cpu=$2) ====="
  docker compose down -v >/dev/null 2>&1 || true
  docker compose up -d --force-recreate --wait agent-transport-livekit mock-services >>"$PROG" 2>&1
  sleep 8
  # per-process memory at idle
  docker exec $C ps -eo pid,rss,comm --sort=-rss 2>/dev/null | head -8 > "$RESULTS/$4.mem-idle.txt"
  # start load in background
  docker run -d --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 --name lk-prof-cli-$4 \
    bench-client:latest \
    python -m load_test.cli --profile "$1" --target agent-transport-livekit \
      --no-docker --at-livekit-url ws://localhost:8083 --docker-container "$C" \
      --output /work/_p_$4.json >/dev/null 2>&1
  sleep 12
  docker exec $C ps -eo pid,rss,comm --sort=-rss 2>/dev/null | head -12 > "$RESULTS/$4.mem-load.txt"
  log "  py-spy record 25s..."
  docker exec $C py-spy record --pid 1 --subprocesses --rate 20 --duration 25 \
    --format speedscope --output /tmp/$4.speedscope.json >>"$PROG" 2>&1 || log "  py-spy rc=$?"
  docker cp $C:/tmp/$4.speedscope.json "$RESULTS/$4.speedscope.json" 2>/dev/null && log "  saved $4.speedscope.json"
  docker rm -f lk-prof-cli-$4 >/dev/null 2>&1 || true
  docker compose down -v >/dev/null 2>&1 || true
}

prof c25 4.0 8G 4cpu-c25
prof c10 1.0 2G 1cpu-c10
log "PROFILE DONE"
