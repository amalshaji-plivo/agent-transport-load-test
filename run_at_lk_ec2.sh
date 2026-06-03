#!/bin/bash
# AT + LiveKit (agent-transport 0.2.0) capacity sweep, EC2-adapted.
# Server builds from PyPI (agent-transport==0.2.0) in Docker. Bench CLIENT runs
# in the bench-client container (AL2 glibc 2.26 can't install agent-transport
# host-side). Single-c steps with a fresh server per step = per-step isolation
# (equivalent to the upstream profile's fresh_container_per_step, which we can't
# use with --no-docker). Validated AT env: turn detector on, Python VAD off,
# TURN_SILENCE_MS=4000, endpointing 0.4/1.5, PROFILE=0.
set -uo pipefail

WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test
cd "$WORK_DIR"
export COMPOSE_PROJECT_NAME=agent-transport-load-test
export COMPOSE_FILE=$WORK_DIR/docker-compose.yml:$WORK_DIR/docker-compose.mocks.yml
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false ENABLE_PY_VAD=false
export MIN_ENDPOINTING_DELAY=0.4 MAX_ENDPOINTING_DELAY=1.5
export CPU_LIMIT=4.0 MEM_LIMIT=8G
C=agent-transport-load-test-agent-transport-livekit-1
RESULTS=$HOME/bench-out/at-lk-4cpu
PROG=$RESULTS/progress.log
mkdir -p "$RESULTS"; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }

STEPS="${STEPS:-c8 c14 c20 c25 c30 c35 c40}"
log "###### AT+LiveKit 0.2.0 4cpu/8G sweep — steps: $STEPS ######"

for STEP in $STEPS; do
  log "===== $STEP begin ====="
  docker compose down -v >/dev/null 2>&1 || true
  docker compose up -d --force-recreate --wait agent-transport-livekit mock-services >>"$PROG" 2>&1
  sleep 8   # EOU inference subprocess init past the TCP healthcheck
  docker run --rm --network host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work \
    -e TURN_SILENCE_MS=4000 \
    bench-client:latest \
    python -m load_test.cli --profile "$STEP" --target agent-transport-livekit \
      --no-docker --at-livekit-url ws://localhost:8083 \
      --docker-container "$C" --output "/work/_at_${STEP}.json" \
    >>"$RESULTS/$STEP.out" 2>&1
  rc=$?
  [ -s "$WORK_DIR/_at_${STEP}.json" ] && mv "$WORK_DIR/_at_${STEP}.json" "$RESULTS/$STEP.json"
  grep -E "sessions=|with_output|Mean / peak CPU|Mean / peak memory|Audible silence" "$RESULTS/$STEP.out" 2>/dev/null | tee -a "$PROG"
  log "===== $STEP exit=$rc ====="
done
docker compose down -v >/dev/null 2>&1 || true
log "###### SWEEP COMPLETE ######"
