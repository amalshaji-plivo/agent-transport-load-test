#!/bin/bash
# Single-cell run: livekit-gateway only, c=15, idle=22, 4 vCPU, 8G RAM,
# with py-spy sampling the agent container's worker tree across the load.
# Mirrors RUN_ON_EC2.md but trimmed to one cell and one target, plus profile.
set -uo pipefail

WORK_DIR=${WORK_DIR:-$(cd "$(dirname "$0")" && pwd)}
COMPOSE_FILE=$WORK_DIR/docker-compose.lkp-vs-lkg.yml
PROJECT_DIR=$WORK_DIR
OUT_DIR=${OUT_DIR:-$HOME/bench-out}
METRICS_DIR=$OUT_DIR/agent-metrics
LOG=$OUT_DIR/c15-lkg-profile.log
RESULTS=${RESULTS_DIR:-$OUT_DIR/results-c15-lkg-profile}
mkdir -p "$METRICS_DIR" "$RESULTS"
: > "$LOG"

log() { echo "$(date '+%H:%M:%S') $*" | tee -a "$LOG"; }

if [ -z "${HOST_IP:-}" ]; then
  if command -v ipconfig >/dev/null 2>&1; then
    HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "")
  fi
  [ -z "$HOST_IP" ] && HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
export HOST_IP
export CPU_LIMIT=4.0
export MEM_LIMIT=8G
export NUM_IDLE_PROCESSES=${NUM_IDLE_PROCESSES:-22}
export ENABLE_VAD=${ENABLE_VAD:-true}
export ENABLE_TURN_DETECTOR=${ENABLE_TURN_DETECTOR:-true}
export AGENT_METRICS_DIR=$METRICS_DIR

STEP=${STEP:-c15}
TARGET=lkg
COMPOSE_PROJECT=$(basename "$WORK_DIR")
CONTAINER=${COMPOSE_PROJECT}-livekit-gateway-agent-1
URL=ws://localhost:8084
IMPL=livekit-gateway

# py-spy sampling window: steady-state only (skip 12s warmup, cover 30s test + slack).
PROFILE_DURATION=35
PROFILE_RATE=100       # samples / sec
WARMUP_SKIP_SEC=${WARMUP_SKIP_SEC:-15}  # c20 warmup=15, c15=12 etc.
PROFILE_OUT_CONTAINER=/agent-metrics/lkg-${STEP}.svg
PROFILE_RAW_CONTAINER=/agent-metrics/lkg-${STEP}.speedscope.json

log "single-cell EC2 run: target=$IMPL step=$STEP idle=$NUM_IDLE_PROCESSES cpu=$CPU_LIMIT mem=$MEM_LIMIT HOST_IP=$HOST_IP"

cd "$PROJECT_DIR"

log "down -v"
# HOST_IP fallback: compose interpolates the whole file (incl. livekit-server's
# required ${HOST_IP:?...}) even for `down`, so an unset HOST_IP makes teardown
# error out and leak the container. Always provide a value here.
HOST_IP="${HOST_IP:-0.0.0.0}" docker compose -f "$COMPOSE_FILE" down -v >> "$LOG" 2>&1 || true

log "force-recreate stack"
docker compose -f "$COMPOSE_FILE" up -d --force-recreate --wait livekit-gateway-agent mock-services >> "$LOG" 2>&1

# CRITICAL readiness gate (both modes): the gateway closes any Plivo session
# that arrives before an agent worker has registered, with NoWorkers — it does
# NOT queue or retry. The agent takes a VARIABLE ~10-15s to register (plugin +
# model + job-runner init), so a fixed sleep races the connection ramp and
# yields anywhere from 0/N to N/N. Wait for the gateway to actually log the
# worker registration before starting the bench.
reg_deadline=180 reg_elapsed=0
until docker exec "$CONTAINER" sh -c 'grep -q "worker registered" /tmp/lkg_gateway.log 2>/dev/null'; do
  if [ "$reg_elapsed" -ge "$reg_deadline" ]; then
    log "WARN: no 'worker registered' in gateway log after ${reg_deadline}s"
    break
  fi
  sleep 1; reg_elapsed=$((reg_elapsed + 1))
done
[ "$reg_elapsed" -lt "$reg_deadline" ] && log "  agent worker registered with gateway (after ${reg_elapsed}s)"

if [ "${JOB_EXECUTOR_TYPE:-process}" = "thread" ]; then
  # Thread-executor mode: no forkserver pool; worker registration (above) is
  # the readiness signal. Small settle margin.
  log "  thread-executor mode: worker registered, settling 5s"
  sleep 5
else
  # Wait for the prewarm pool. Works at any log level because we count
  # forkserver-spawned worker processes inside the container, then wait a
  # grace period sized to (workers * 12s) / vCPU for prewarm() to finish.
  expected=$((NUM_IDLE_PROCESSES + 1))   # +1 for the forkserver itself
  deadline=240 count=0 elapsed=0
  while [ "$elapsed" -lt "$deadline" ]; do
    count=$(docker exec "$CONTAINER" sh -c \
      "grep -l forkserver /proc/[0-9]*/cmdline 2>/dev/null | wc -l" 2>/dev/null || echo 0)
    count=${count:-0}
    if [ "$count" -ge "$expected" ]; then
      log "  $count/$expected forkserver+worker processes spawned (after ${elapsed}s)"
      break
    fi
    sleep 2; elapsed=$((elapsed + 2))
  done
  [ "$count" -lt "$expected" ] && log "WARN: only $count/$expected processes spawned"
  GRACE=${PREWARM_GRACE_SEC:-$(( NUM_IDLE_PROCESSES * 12 / 4 + 10 ))}
  log "  waiting ${GRACE}s for prewarm() to finish across all workers"
  sleep "$GRACE"
fi

# Clear previous step metrics.
rm -f "$METRICS_DIR/lkg.jsonl"

log "===== bench begin ($IMPL $STEP) ====="
cd "$WORK_DIR"
.venv/bin/python -m load_test.cli \
    --profile "$STEP" --target "$IMPL" --no-docker \
    --docker-container "$CONTAINER" \
    --lkg-url "$URL" \
    --output "$RESULTS/$TARGET-$STEP.json" \
    >> "$RESULTS/$TARGET-$STEP.out" 2>&1 &
BENCH_PID=$!
cd "$PROJECT_DIR"

# Let the bench's warmup phase complete before sampling, so py-spy only sees
# steady-state load (no ramp / prewarm noise).
log "warmup skip: sleeping ${WARMUP_SKIP_SEC}s before starting py-spy"
sleep "$WARMUP_SKIP_SEC"

log "starting py-spy (${PROFILE_DURATION}s @ ${PROFILE_RATE} Hz, flamegraph + speedscope)"
docker exec -d "$CONTAINER" bash -lc \
  "py-spy record --pid 1 --subprocesses --rate $PROFILE_RATE --duration $PROFILE_DURATION \
     --format flamegraph --output $PROFILE_OUT_CONTAINER" \
  >> "$LOG" 2>&1
docker exec -d "$CONTAINER" bash -lc \
  "py-spy record --pid 1 --subprocesses --rate $PROFILE_RATE --duration $PROFILE_DURATION \
     --format speedscope --output $PROFILE_RAW_CONTAINER" \
  >> "$LOG" 2>&1

wait "$BENCH_PID"
rc=$?
[ -s "$METRICS_DIR/lkg.jsonl" ] && cp "$METRICS_DIR/lkg.jsonl" "$RESULTS/$TARGET-$STEP.metrics.jsonl"
log "bench exit=$rc"

# Wait for py-spy to finish writing its files (duration + small margin).
log "waiting for py-spy to flush..."
sleep 10
for f in "$METRICS_DIR/lkg-${STEP}.svg" "$METRICS_DIR/lkg-${STEP}.speedscope.json"; do
  for _ in $(seq 1 30); do
    [ -s "$f" ] && break
    sleep 2
  done
  if [ -s "$f" ]; then
    cp "$f" "$RESULTS/"
    log "  saved $(basename "$f") ($(stat -c%s "$f" 2>/dev/null || stat -f%z "$f") bytes)"
  else
    log "  MISSING $f"
  fi
done

log "down -v"
# HOST_IP fallback: compose interpolates the whole file (incl. livekit-server's
# required ${HOST_IP:?...}) even for `down`, so an unset HOST_IP makes teardown
# error out and leak the container. Always provide a value here.
HOST_IP="${HOST_IP:-0.0.0.0}" docker compose -f "$COMPOSE_FILE" down -v >> "$LOG" 2>&1 || true
log "C15 LKG PROFILE RUN DONE"
log "artifacts: $RESULTS/"
