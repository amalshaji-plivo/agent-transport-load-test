# Run the s2 sweep on EC2

Reproduces the `idle=20 + Silero@8kHz` benchmark. The Silero 8 kHz change is
already in `agent-transport-load-test/load_test/servers/livekit_{python,gateway}_server.py`.

## Files to push

- `agent-transport-load-test/` — bench client, mock services, agent server entrypoints
- `livekit-gateway/` — Rust source for the gateway image build
- `docker-compose.lkp-vs-lkg.yml` — the bench compose stack

## Config

| env var | value | meaning |
|---|---|---|
| `HOST_IP` | EC2 private IPv4 (e.g. `172.31.x.x`) | advertised in WebRTC ICE candidates; loopback won't work |
| `CPU_LIMIT` | `4.0` | per-agent-container vCPU cgroup cap |
| `MEM_LIMIT` | `12G` | per-agent-container memory cap |
| `NUM_IDLE_PROCESSES` | `20` | prewarmed worker pool (must be ≥ max tested c so all sessions hit a warm fork) |
| `ENABLE_VAD` | `true` | |
| `ENABLE_TURN_DETECTOR` | `true` | |

## Build

From the workspace root:

```bash
HOST_IP=$(hostname -I | awk '{print $1}') \
  docker compose -f docker-compose.lkp-vs-lkg.yml \
  build livekit-python-agent livekit-gateway-agent
```

## Sweep script

Save as `~/sweep_s2_idle20.sh` (path-portable; uses `$HOME` instead of macOS paths):

```bash
#!/bin/bash
set -uo pipefail

COMPOSE_FILE=$HOME/agent-stack-workspace/docker-compose.lkp-vs-lkg.yml
PROJECT_DIR=$HOME/agent-stack-workspace
WORK_DIR=$HOME/agent-stack-workspace/agent-transport-load-test
OUT_DIR=$HOME/bench-out
METRICS_DIR=$OUT_DIR/agent-metrics
LOG=$OUT_DIR/sweep.log
RESULTS=$OUT_DIR/results-s2-idle20-ec2
mkdir -p "$METRICS_DIR" "$RESULTS"
: > "$LOG"

log() { echo "$(date '+%H:%M:%S') $*" | tee -a "$LOG"; }

export HOST_IP=${HOST_IP:-$(hostname -I | awk '{print $1}')}
export CPU_LIMIT=4.0
export AGENT_METRICS_DIR=$METRICS_DIR
export ENABLE_VAD=true
export ENABLE_TURN_DETECTOR=true
export NUM_IDLE_PROCESSES=20
export MEM_LIMIT=12G

log "EC2 s2 sweep: idle=$NUM_IDLE_PROCESSES, Silero@8kHz, HOST_IP=$HOST_IP"

cd "$PROJECT_DIR"

clear_step_metrics() { rm -f "$METRICS_DIR/lkp.jsonl" "$METRICS_DIR/lkg.jsonl"; }

recreate_all() {
  log "force-recreate full stack"
  docker compose -f "$COMPOSE_FILE" up -d --force-recreate --wait >> "$LOG" 2>&1
  local expected=$NUM_IDLE_PROCESSES deadline=120
  for svc in livekit-python-agent livekit-gateway-agent; do
    local count=0 elapsed=0
    while [ "$elapsed" -lt "$deadline" ]; do
      count=$(docker compose -f "$COMPOSE_FILE" logs --no-color --tail=4000 "$svc" 2>/dev/null \
          | grep -c "process initialized")
      if [ "$count" -ge "$expected" ]; then
        log "  $svc: ${count}/${expected} prewarmed (after ${elapsed}s)"
        break
      fi
      sleep 2; elapsed=$((elapsed + 2))
    done
    [ "$count" -lt "$expected" ] && log "  WARN: $svc only ${count}/${expected} prewarmed"
  done
  sleep 3
}

run_step() {
  local target=$1 step=$2 container url impl
  case "$target" in
    lkp) container="agent-stack-workspace-livekit-python-agent-1";  url="livekit://localhost:7880"; impl="livekit-python" ;;
    lkg) container="agent-stack-workspace-livekit-gateway-agent-1"; url="ws://localhost:8084";       impl="livekit-gateway" ;;
  esac

  log "===== s2 $target $step begin ====="
  clear_step_metrics
  recreate_all

  cd "$WORK_DIR"
  .venv/bin/python -m load_test.cli \
      --profile "$step" --target "$impl" --no-docker \
      --docker-container "$container" \
      $( [ "$target" = "lkp" ] && echo "--lkp-url $url" || echo "--lkg-url $url" ) \
      --output "$RESULTS/$target-$step.json" \
      >> "$RESULTS/$target-$step.out" 2>&1
  local rc=$?
  cd "$PROJECT_DIR"
  [ -s "$METRICS_DIR/$target.jsonl" ] && cp "$METRICS_DIR/$target.jsonl" "$RESULTS/$target-$step.metrics.jsonl"
  log "s2 $target $step exit=$rc"
}

docker compose -f "$COMPOSE_FILE" down -v >> "$LOG" 2>&1 || true

for STEP in c2 c5 c8 c11 c14 c17 c20; do run_step lkp "$STEP"; done
for STEP in c2 c5 c8 c11 c14 c17 c20; do run_step lkg "$STEP"; done

docker compose -f "$COMPOSE_FILE" down -v >> "$LOG" 2>&1 || true
log "S2 IDLE=20 EC2 RUN DONE"
```

## Run

```bash
chmod +x ~/sweep_s2_idle20.sh
nohup ~/sweep_s2_idle20.sh > ~/bench-out/sweep.out 2>&1 &
disown
tail -f ~/bench-out/sweep.log     # done when you see "S2 IDLE=20 EC2 RUN DONE"
```

~35 min wall clock. Results land under `~/bench-out/results-s2-idle20-ec2/`,
one JSON per cell (`lkp-c{N}.json` / `lkg-c{N}.json`).

## Compare

Run the same comparison script that produced the macOS report, pointed at
the EC2 output dir:

```python
NEW      = '/path/to/results-s2-idle20-ec2'       # EC2 run
BASELINE = '/tmp/lkg-bench/results-s2-silero8k'   # macOS Silero@8kHz reference
```

The interesting fields per cell are
`summaries[0].sessions_with_output / .total_sessions`,
`within_phrase_gap.p99`, `jitter.p99`, `audible_silence_gap.p90`,
`resources.mean_cpu`, `resources.peak_memory_mb`.

Quality gates: 100% sessions, wphase p99 ≤ 30 ms, silence p90 ≤ 5 ms, CPU mean ≤ 80%.
