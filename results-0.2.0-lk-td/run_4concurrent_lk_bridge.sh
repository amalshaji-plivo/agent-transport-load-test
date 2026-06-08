#!/bin/bash
# AT+livekit 4-concurrent on a BRIDGE network (fixes the --network host fixed-port
# collision: each container gets its own net namespace, so the LiveKit agent's
# internal port can't clash). All 4 agents use the SAME internal WS_PORT=8083,
# mapped to distinct host ports 8111-8114. Mock-services reached by name over the
# bridge. Staggered startup (one warm at a time), then 4 clients fire together.
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test; cd "$WORK_DIR"
RES=$HOME/bench-out/4concurrent; mkdir -p "$RES"; PROG=$RES/progress_lkbr.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }
IMG_LK=agent-transport-load-test-agent-transport-livekit
IMG_MOCK=agent-transport-load-test/mock-services:latest
LK_ENV='-e ENABLE_TURN_DETECTOR=true -e ENABLE_VAD=false -e ENABLE_PY_VAD=false -e MIN_ENDPOINTING_DELAY=0.4 -e MAX_ENDPOINTING_DELAY=1.5'
NET=lkbridge
wait_port(){ for _ in $(seq 1 120); do (echo >/dev/tcp/localhost/$1) 2>/dev/null && return 0; sleep 1; done; return 1; }

docker rm -f mock4 a1 a2 a3 a4 2>/dev/null >/dev/null
docker network rm $NET 2>/dev/null >/dev/null || true
docker network create $NET >/dev/null

log "===== AT+livekit : 4 x c10 concurrent (BRIDGE net, staggered) ====="
# mock on bridge (reachable by name) + published to host for the readiness probe
docker run -d --name mock4 --network $NET --network-alias mock-services -p 9000:9000 $IMG_MOCK >>"$PROG" 2>&1
wait_port 9000 || { log "mock failed"; exit 1; }
log "mock-services ready"

PORTS=(8111 8112 8113 8114)
for k in 0 1 2 3; do
  n=$((k+1)); p=${PORTS[$k]}
  docker run -d --name a$n --network $NET --cpus=1.0 --memory=2g \
    -p $p:8083 -e MOCK_SERVICES_HOST=mock-services:9000 -e WS_PORT=8083 -e HTTP_PORT=8184 \
    $LK_ENV $IMG_LK >>"$PROG" 2>&1
  if wait_port $p; then log "a$n (host :$p -> :8083) ready"; else log "a$n (:$p) FAILED"; fi
  sleep 5
done
log "all 4 up; warmup pause"; sleep 10

for k in 0 1 2 3; do
  n=$((k+1)); p=${PORTS[$k]}
  docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 bench-client:latest \
    python -m load_test.cli --profile c10 --target agent-transport-livekit --no-docker \
      --at-livekit-url ws://localhost:$p --docker-container a$n \
      --output "/work/_4cbr_a${n}.json" >>"$RES/lkbr_a${n}.out" 2>&1 &
done
( for _ in $(seq 1 20); do docker stats --no-stream --format '{{.CPUPerc}}' mock4 2>/dev/null; sleep 3; done ) >"$RES/lkbr_mockcpu.log" 2>&1 &
wait
for n in 1 2 3 4; do [ -s "$WORK_DIR/_4cbr_a${n}.json" ] && mv "$WORK_DIR/_4cbr_a${n}.json" "$RES/lkbr_a${n}.json"; done
docker rm -f mock4 a1 a2 a3 a4 2>/dev/null >/dev/null
docker network rm $NET 2>/dev/null >/dev/null || true
log "###### 4CONCURRENT_LK_BRIDGE DONE ######"
