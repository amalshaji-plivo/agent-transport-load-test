#!/bin/bash
# AT+livekit 4-concurrent, CORRECTED: stagger agent startup (launch + fully warm
# ONE at a time) so 4 heavy EOU model loads don't starve each other. Then fire
# all 4 clients together (the steady-state concurrency test). Each agent capped
# 1 vCPU/2 GB, shared mock-services.
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test; cd "$WORK_DIR"
RES=$HOME/bench-out/4concurrent; mkdir -p "$RES"; PROG=$RES/progress_lk.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }
IMG_LK=agent-transport-load-test-agent-transport-livekit
IMG_MOCK=agent-transport-load-test/mock-services:latest
LK_ENV='-e ENABLE_TURN_DETECTOR=true -e ENABLE_VAD=false -e ENABLE_PY_VAD=false -e MIN_ENDPOINTING_DELAY=0.4 -e MAX_ENDPOINTING_DELAY=1.5'

docker rm -f mock4 a1 a2 a3 a4 2>/dev/null >/dev/null
wait_port(){ for _ in $(seq 1 120); do (echo >/dev/tcp/localhost/$1) 2>/dev/null && return 0; sleep 1; done; return 1; }

log "===== AT+livekit : 4 x c10 concurrent (staggered startup) ====="
docker run -d --name mock4 --network host $IMG_MOCK >>"$PROG" 2>&1
wait_port 9000 || { log "mock failed"; exit 1; }

PORTS=(8111 8112 8113 8114); HTTP=(8211 8212 8213 8214)
for k in 0 1 2 3; do
  n=$((k+1)); p=${PORTS[$k]}; h=${HTTP[$k]}
  docker run -d --name a$n --network host --cpus=1.0 --memory=2g \
    -e MOCK_SERVICES_HOST=localhost:9000 -e WS_PORT=$p -e HTTP_PORT=$h $LK_ENV $IMG_LK >>"$PROG" 2>&1
  if wait_port $p; then log "a$n (:$p) ready"; else log "a$n (:$p) FAILED to start"; fi
  sleep 5   # let model settle before starting the next (avoid load contention)
done
log "all 4 up; warmup pause"; sleep 10

# fire 4 clients in parallel = the real steady-state concurrency test
for k in 0 1 2 3; do
  n=$((k+1)); p=${PORTS[$k]}
  docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 bench-client:latest \
    python -m load_test.cli --profile c10 --target agent-transport-livekit --no-docker \
      --at-livekit-url ws://localhost:$p --docker-container a$n \
      --output "/work/_4c_at-livekit_a${n}.json" >>"$RES/at-livekit_a${n}.out" 2>&1 &
done
( for _ in $(seq 1 20); do docker stats --no-stream --format '{{.CPUPerc}}' mock4 2>/dev/null; sleep 3; done ) >"$RES/at-livekit_mockcpu.log" 2>&1 &
wait
for n in 1 2 3 4; do [ -s "$WORK_DIR/_4c_at-livekit_a${n}.json" ] && mv "$WORK_DIR/_4c_at-livekit_a${n}.json" "$RES/at-livekit_a${n}.json"; done
docker rm -f mock4 a1 a2 a3 a4 2>/dev/null >/dev/null
log "###### 4CONCURRENT_LK DONE ######"
