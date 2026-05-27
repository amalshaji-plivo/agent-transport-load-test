# EC2 runbook — thread-executor concurrency sweep (4 vCPU / 8 GB, VAD + TD on)

Goal: reproduce the thread-executor finding on real hardware and find the TRUE
concurrency ceiling. On the dev Mac the agent stops being the bottleneck above
c=40 — the host load-generator runs out of RAM first. A dedicated EC2 box (with
the bench client driven from a *separate* machine, or a bigger box) lets us push
past that.

## TL;DR finding to reproduce

Switching `JOB_EXECUTOR_TYPE` from `process` → `thread` took sustainable
concurrency from c=15 → c=40+ on 4 vCPU / 8 GB, with VAD + multilingual turn
detector ON, while passing all latency gates (process mode failed them at every
concurrency, including c=15).

| config | sessions | CPU mean | mem mean | wphase p99 | silence p99 |
|---|---|---|---|---|---|
| c=15 process (old optimum) | 15/15 | 234 % | 5.6 GB | 61 ms ❌ | 41 ms ❌ |
| c=20 thread | 20/20 | 88 % | 1.4 GB | 22 ms ✓ | 1.8 ms ✓ |
| c=40 thread | 40/40 | 125 % | 1.95 GB | 22 ms ✓ | 1.8 ms ✓ |

## ⚠️ Expect LOWER absolute concurrency on EC2 than these Mac numbers

These numbers were measured on an Apple-Silicon Mac with OrbStack `cpus=4.0`
(= 4 FULL M-series cores). **"4 vCPU" on EC2 is not the same amount of compute:**

- An EC2 vCPU is usually a **hyperthread** — half a physical x86/Graviton core on
  shared silicon. 4 vCPUs ≈ 2 physical cores of real throughput.
- Apple-Silicon cores have much higher per-core IPC, larger caches, and far more
  memory bandwidth than a shared EC2 vCPU.
- The choke here is **CPU-burst-saturation, not memory**, so sustainable
  concurrency scales ~linearly with real per-core compute. Weaker cores → the
  saturation cliff lands at a lower c.

Implication: the absolute c values above (process c=30, thread c=40+) are
OPTIMISTIC for EC2. The original on-EC2 optimum was c=15 — roughly consistent
with EC2 cores being ~2× weaker for this onnx/FFI/IPC-heavy workload. Treat the
Mac numbers as an upper bound; the EC2 run produces the authoritative figure.

What DOES transfer: the **relative** result — thread mode beats process mode by
~2-2.7× concurrency with far lower memory and latency — because it removes work
(per-frame IPC, per-fork model duplication), independent of raw core speed.

ALSO: record the instance type. A **burstable** type (t2/t3/t4g) throttles
sustained load via CPU credits and will cap concurrency far below a dedicated
type (c6i/c7g/m6i). Use a dedicated/compute instance and pin it; note the exact
type in the results so the numbers are interpretable.

## Files to push (sibling layout required)

```
agent-stack-workspace/
  agent-transport-load-test/   # this repo
  livekit-gateway/             # Rust gateway source (sibling, for the image build)
```

The code changes that make thread mode work are already committed in
`load_test/servers/livekit_gateway_server.py`:
1. silero + turn_detector imports hoisted to module top (plugin registration
   must run on the main thread; in thread mode prewarm() runs on a worker thread).
2. Both EOU model variants (English + Multilingual) pre-downloaded at import time
   (the inference subprocess initializes BOTH registered runners at boot with
   `local_files_only=True`, before prewarm()).
3. `JOB_EXECUTOR_TYPE` env knob (process|thread), wired through docker-compose.

## Config

| env var | value | meaning |
|---|---|---|
| `HOST_IP` | EC2 private IPv4 (`172.31.x.x`) | WebRTC ICE; loopback won't work. REQUIRED even for `docker compose down`. |
| `JOB_EXECUTOR_TYPE` | `thread` | the whole point — run sessions as threads in one process |
| `ENABLE_VAD` | `true` | always on |
| `ENABLE_TURN_DETECTOR` | `true` | always on (multilingual EOU) |
| `NUM_IDLE_PROCESSES` | `1` | irrelevant in thread mode (no fork pool); keep at 1 |
| `LKG_LOG_LEVEL` | `warn` | info adds ~9 % CPU + doubles RTT via log IPC |
| `CPU_LIMIT` | `4.0` | cgroup vCPU cap |
| `MEM_LIMIT` | `8G` | cgroup memory cap |

## Build

From inside `agent-transport-load-test/`:

```bash
export HOST_IP=$(hostname -I | awk '{print $1}')
docker compose -f docker-compose.lkp-vs-lkg.yml build livekit-gateway-agent mock-services
python -m venv .venv && .venv/bin/pip install -e .
```

The gateway target (`--target livekit-gateway`, `ws://…:8084`) uses
`PlivoWsClient` over plain `websockets` — it needs NEITHER `livekit` nor
`livekit-api`. The `LivekitRtcClient` import is now lazy, so a gateway-only run
imports cleanly without them. Only install `livekit livekit-api` if you also
want to run the stock-LiveKit comparison target (`--target livekit-python`,
`livekit://…`), which mints JWT access tokens via `livekit-api`.

## Sweep script

Save as `~/sweep_thread.sh`:

```bash
#!/bin/bash
set -uo pipefail
WORK_DIR=$HOME/agent-stack-workspace/agent-transport-load-test
cd "$WORK_DIR"
export HOST_IP=$(hostname -I | awk '{print $1}')
export LKG_LOG_LEVEL=warn ENABLE_VAD=true ENABLE_TURN_DETECTOR=true
export JOB_EXECUTOR_TYPE=thread NUM_IDLE_PROCESSES=1
export CPU_LIMIT=4.0 MEM_LIMIT=8G

# step:warmup_skip pairs — extend upward to find the ceiling
for spec in c20:15 c30:27 c40:45 c50:55 c60:60 c80:70; do
  STEP=${spec%%:*}; SKIP=${spec##*:}
  echo ">>> $STEP"
  STEP=$STEP WARMUP_SKIP_SEC=$SKIP \
    RESULTS_DIR=$HOME/bench-out/thread-$STEP \
    ./run_c15_lkg_profile.sh
  # teardown is HOST_IP-proof inside the script, but force it here too
  HOST_IP=$HOST_IP docker compose -f docker-compose.lkp-vs-lkg.yml down -v >/dev/null 2>&1 || true
done
echo ">>> SWEEP DONE"
```

```bash
chmod +x ~/sweep_thread.sh
nohup ~/sweep_thread.sh > ~/bench-out/sweep-thread.out 2>&1 &
disown
tail -f ~/bench-out/sweep-thread.out
```

Profiles c20..c50 already exist in the repo. c60/c80 need profile entries in
`load_test/profiles.py` (copy the c50 line, bump concurrency + warmup_sec).

## CRITICAL measurement caveats (read before trusting numbers)

1. **py-spy inflates CPU.** `run_c15_lkg_profile.sh` runs `py-spy record` INSIDE
   the container during the measurement window, and CPU is read from
   `docker stats` (whole-cgroup), so the profiler's own CPU (ptrace sampling of
   50-90 threads at 100 Hz) is counted. This is a near-constant overhead, so it
   inflates LOW-concurrency CPU most. For clean capacity numbers, run a variant
   WITHOUT py-spy (skip the two `docker exec ... py-spy` lines) and compare. Use
   the py-spy run only for the flamegraph, the no-py-spy run for CPU/mem.

2. **`docker compose down` REQUIRES HOST_IP.** The compose file interpolates
   `livekit-server`'s `${HOST_IP:?...}` even for `down`. Without it, teardown
   errors out and leaks the container (we lost ~2.3 GB to a stale container on
   the Mac, which then starved the load-generator and caused phantom session
   losses). The script now forces `HOST_IP=${HOST_IP:-0.0.0.0}` on teardown —
   keep that.

3. **Drive the bench client from a SEPARATE box if possible.** The host client
   buffers all received audio frames in RAM (~150k frames at c=40). On the same
   box as an 8 GB container it competes for memory. To find the true ceiling,
   run `python -m load_test.cli ... --lkg-url ws://<ec2-ip>:8084` from a second
   machine pointed at the EC2 agent.

## Quality gates (per the original runbook)

100 % sessions, within-phrase p99 ≤ 30 ms, audible-silence p90 ≤ 5 ms,
mean CPU ≤ 80 % of the 400 % cap (i.e. ≤ 320 %).

NOTE: process mode fails wphase/silence at EVERY concurrency incl. c=15 — those
gates are only achievable in thread mode. Thread mode passes all four through
at least c=40.

## What to look for

- The step where sessions drop below 100 % OR mean CPU (no-py-spy) crosses 320 %.
  That's the new optimum.
- Watch the GIL ceiling: thread mode pins pure-Python work to ~1 core. The
  turn-detector tokenization (~13-17 % of CPU, pure Python, holds the GIL) is the
  likely scaling wall. If latency tails blow up together across all sessions at
  some c, that's GIL head-of-line blocking, not memory.
- Per-cell artifacts land in `~/bench-out/thread-<step>/`: `lkg-<step>.json`
  (metrics), `.svg` (flamegraph), `.speedscope.json` (load at speedscope.app).

## Downsides of thread mode to weigh

- No fault isolation: one session crash / FFI segfault / OOM kills ALL sessions.
- GIL: pure-Python hot-path work serializes across sessions (see tokenization).
- Any accidental synchronous/blocking call on the event loop freezes every
  session, not one. Audit STT/LLM/TTS adapters + gateway glue for sync I/O.
- Memory leaks accumulate in one long-lived process instead of being reclaimed
  on per-job respawn.

---

# Process mode (the recommended path) + the registration-race bug

Thread mode was abandoned (no fault isolation + GIL serialization, and the
single worker is most exposed to the race below). For production sizing, run
PROCESS mode: `JOB_EXECUTOR_TYPE=process` (the default), `NUM_IDLE_PROCESSES`
≈ target concurrency + 2.

## The registration race (now fixed in run_c15_lkg_profile.sh)

The Rust gateway rejects any Plivo session that arrives before an agent worker
has registered (`err=NoWorkers`) and CLOSES it — no queue, no retry. The agent
takes a variable ~10-15s to register. A fixed warmup sleep races the connection
ramp, giving anywhere from 0/N to N/N for the SAME config. The harness now gates
bench start on the gateway logging `worker registered` (polls
`/tmp/lkg_gateway.log` inside the container). Keep that gate — without it your
EC2 numbers will be non-deterministic.

## Local (Mac) process-mode result, for reference — EXPECT LOWER ON EC2

| concurrency | sessions | CPU mean/peak | mem peak | wphase p99 | silence p99 |
|---|---|---|---|---|---|
| c30 | 30/30 ✓ | 286 % / 404 % | 5.8 GB | 64 ms | 44 ms |
| c33 | 26/33 ❌ | 280 % / 404 % | 6.8 GB | 62 ms | 42 ms |

Mac ceiling = c=30, bounded by 4-vCPU BURST saturation (peak pinned at the
400 % cap; mean ~285 %). Memory never the limit (~6 GB of 8 GB). Latency gates
(wphase ≤30, silence ≤5) FAIL at every concurrency including c=20 — that's the
constant per-frame cross-process IPC floor, load-independent.

## Why EC2 gives lower concurrency than these Mac numbers

1. **"4 vCPU" on EC2 = hyperthreads** (~2 physical cores) vs 4 full Apple-Silicon
   cores on the Mac. The choke is CPU-burst-saturation, so the ceiling scales ~
   linearly with real per-core compute — weaker cores, lower c. The original
   on-EC2 optimum was c=15 (~2× weaker cores for this onnx/FFI workload).
2. **Gateway + agent share the cgroup.** The Rust gateway (transport) and the
   Python agent (VAD + turn-detector onnx inference) draw from the same
   4 vCPU / 8 GB. `docker stats` CPU is the sum; py-spy only sees the Python
   side. On weaker EC2 cores both cost more wall-time, so saturation hits sooner.

Pin a DEDICATED instance type (c6i/c7g/m6i — NOT burstable t2/t3/t4g, whose CPU
credits throttle sustained load) and record it with the results.

## Process sweep on EC2

```bash
export HOST_IP=$(hostname -I | awk '{print $1}')
export LKG_LOG_LEVEL=warn ENABLE_VAD=true ENABLE_TURN_DETECTOR=true
export JOB_EXECUTOR_TYPE=process CPU_LIMIT=4.0 MEM_LIMIT=8G
for spec in c10:12:12 c12:12:14 c15:12:17 c18:14:20 c20:15:22 c25:25:27; do
  STEP=${spec%%:*}; rest=${spec#*:}; SKIP=${rest%%:*}; IDLE=${rest##*:}
  NUM_IDLE_PROCESSES=$IDLE STEP=$STEP WARMUP_SKIP_SEC=$SKIP \
    RESULTS_DIR=$HOME/bench-out/proc-$STEP ./run_c15_lkg_profile.sh
  HOST_IP=$HOST_IP docker compose -f docker-compose.lkp-vs-lkg.yml down -v >/dev/null 2>&1 || true
done
```

Start lower (c10-c15) since EC2 will choke earlier than the Mac. The ceiling is
the last step with 100 % sessions AND mean CPU (read it from a no-py-spy run)
under ~320 % (80 % of the 400 % cap).
