#!/bin/bash
# EC2-adapted single-cell harness — same logic as run_c15_lkg_profile.sh, but
# runs the bench CLIENT inside the bench-client:latest container (this box is
# Amazon Linux 2, glibc 2.26, which can't install agent-transport's
# manylinux_2_28 wheels host-side). Everything else — readiness gate, PROFILE
# switch, HOST_IP-proof teardown — is preserved from the upstream harness.
#
# Env in:  STEP, NUM_IDLE_PROCESSES, RESULTS_DIR, PROFILE, WARMUP_SKIP_SEC,
#          ENABLE_VAD, ENABLE_TURN_DETECTOR, JOB_EXECUTOR_TYPE, CPU_LIMIT, MEM_LIMIT
set -uo pipefail

WORK_DIR=${WORK_DIR:-$(cd "$(dirname "$0")" && pwd)}
COMPOSE_FILE=$WORK_DIR/docker-compose.lkp-vs-lkg.yml
OUT_DIR=${OUT_DIR:-$HOME/bench-out}
METRICS_DIR=$OUT_DIR/agent-metrics
LOG=$OUT_DIR/lkg-ec2.log
RESULTS=${RESULTS_DIR:-$OUT_DIR/results-lkg-ec2}
mkdir -p "$METRICS_DIR" "$RESULTS"
: > "$LOG"

log() { echo "$(date '+%H:%M:%S') $*" | tee -a "$LOG"; }

export HOST_IP=${HOST_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}
export CPU_LIMIT=${CPU_LIMIT:-4.0}
export MEM_LIMIT=${MEM_LIMIT:-8G}
export NUM_IDLE_PROCESSES=${NUM_IDLE_PROCESSES:-22}
export ENABLE_VAD=${ENABLE_VAD:-true}
export ENABLE_TURN_DETECTOR=${ENABLE_TURN_DETECTOR:-true}
export JOB_EXECUTOR_TYPE=${JOB_EXECUTOR_TYPE:-process}
export AGENT_METRICS_DIR=$METRICS_DIR

STEP=${STEP:-c15}
TARGET=lkg
COMPOSE_PROJECT=$(basename "$WORK_DIR")
CONTAINER=${COMPOSE_PROJECT}-livekit-gateway-agent-1
URL=ws://localhost:8084
IMPL=livekit-gateway

PROFILE=${PROFILE:-0}
PROFILE_DURATION=${PROFILE_DURATION:-35}
PROFILE_RATE=${PROFILE_RATE:-20}
WARMUP_SKIP_SEC=${WARMUP_SKIP_SEC:-15}
PROFILE_RAW_CONTAINER=/agent-metrics/lkg-${STEP}.speedscope.json

log "EC2 cell: target=$IMPL step=$STEP idle=$NUM_IDLE_PROCESSES exec=$JOB_EXECUTOR_TYPE cpu=$CPU_LIMIT mem=$MEM_LIMIT PROFILE=$PROFILE HOST_IP=$HOST_IP"

cd "$WORK_DIR"

log "down -v"
HOST_IP="${HOST_IP:-0.0.0.0}" docker compose -f "$COMPOSE_FILE" down -v >> "$LOG" 2>&1 || true

log "force-recreate stack"
docker compose -f "$COMPOSE_FILE" up -d --force-recreate --wait livekit-gateway-agent mock-services >> "$LOG" 2>&1

# Worker-registration readiness gate (gateway drops calls that arrive before the
# agent registers — NoWorkers, no retry). Wait for the gateway's log line.
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
  log "  thread-executor mode: worker registered, settling 5s"
  sleep 5
else
  # Wait for the prewarm pool: count forkserver-spawned worker procs, then a
  # grace period sized to (workers * 12s) / vCPU for prewarm() to finish.
  expected=$((NUM_IDLE_PROCESSES + 1))
  deadline=300 count=0 elapsed=0
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

rm -f "$METRICS_DIR/lkg.jsonl"

log "===== bench begin ($IMPL $STEP) ====="
# Bench client containerized (--network host so localhost:8084 resolves to the
# agent's published port; docker.sock mounted so `docker stats <name>` works).
docker run --rm \
    --network host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work \
    -w /work \
    --name lkg-bench-ec2 \
    bench-client:latest \
    python -m load_test.cli \
      --profile "$STEP" --target "$IMPL" --no-docker \
      --docker-container "$CONTAINER" \
      --lkg-url "$URL" \
      --output "/work/_bench_${STEP}.json" \
    >> "$RESULTS/$TARGET-$STEP.out" 2>&1 &
BENCH_PID=$!

if [ "$PROFILE" = "1" ]; then
  log "warmup skip: sleeping ${WARMUP_SKIP_SEC}s before starting py-spy"
  sleep "$WARMUP_SKIP_SEC"
  log "starting py-spy (single instance, ${PROFILE_DURATION}s @ ${PROFILE_RATE} Hz, speedscope)"
  docker exec -d "$CONTAINER" bash -lc \
    "py-spy record --pid 1 --subprocesses --rate $PROFILE_RATE --duration $PROFILE_DURATION \
       --format speedscope --output $PROFILE_RAW_CONTAINER" \
    >> "$LOG" 2>&1
else
  log "PROFILE=0: no py-spy — clean capacity measurement"
fi

wait "$BENCH_PID"
rc=$?
[ -s "$WORK_DIR/_bench_${STEP}.json" ] && mv "$WORK_DIR/_bench_${STEP}.json" "$RESULTS/$TARGET-$STEP.json"
[ -s "$METRICS_DIR/lkg.jsonl" ] && cp "$METRICS_DIR/lkg.jsonl" "$RESULTS/$TARGET-$STEP.metrics.jsonl"
log "bench exit=$rc"

if [ "$PROFILE" = "1" ]; then
  log "waiting for py-spy to flush..."
  sleep 10
  f="$METRICS_DIR/lkg-${STEP}.speedscope.json"
  for _ in $(seq 1 30); do [ -s "$f" ] && break; sleep 2; done
  if [ -s "$f" ]; then
    cp "$f" "$RESULTS/"
    log "  saved $(basename "$f") ($(stat -c%s "$f" 2>/dev/null || echo '?') bytes)"
  else
    log "  MISSING $f"
  fi
fi

log "down -v"
HOST_IP="${HOST_IP:-0.0.0.0}" docker compose -f "$COMPOSE_FILE" down -v >> "$LOG" 2>&1 || true
log "DONE — artifacts: $RESULTS/"
