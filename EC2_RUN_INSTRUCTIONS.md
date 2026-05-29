# EC2 benchmark instructions (hand this to Claude on the EC2 box)

You are running the livekit-gateway load-test harness on a real EC2 instance.
All harness code, fixes, and prior findings are already in this repo. Read
`PROCESS_MODE_FINDINGS.md` and `RUN_ON_EC2_THREAD.md` first — they contain the
methodology and the bugs already fixed. This file is your task list.

## Mission

We benchmark **process mode only** (`JOB_EXECUTOR_TYPE=process` — thread mode was
abandoned: no fault isolation + GIL). Two questions the dev Mac could NOT answer
and you must:

1. **Real capacity ceiling on EC2 hardware.** The Mac (4 full Apple-Silicon
   cores) held c=30. EC2 "4 vCPU" = ~2 physical cores (hyperthreads), so expect
   LOWER — the original on-EC2 optimum was c≈15. Find the max concurrency that
   holds 100% sessions AND passes the latency gates.

2. **The right `NUM_IDLE_PROCESSES`.** This is the key open question. On the Mac,
   only `idle = concurrency` held 100% — but that pins memory at the 8 GB cap,
   and it's an artifact of the benchmark's SHORT calls (high churn outruns the
   ~12 s prewarm refill). With REALISTIC long calls (`c30_long`-style, 300 s)
   churn is low, so a much smaller idle pool should suffice. The Mac OOM'd on the
   long-call test; you can run it. Governing rule:
   `idle ≥ ceil(peak_arrival_rate × prewarm_seconds)`, NOT `idle = concurrency`.

Workload is fixed: **VAD on + multilingual turn detector on**, 4 vCPU / 8 GB.

## Setup

```bash
cd ~/agent-stack-workspace/agent-transport-load-test   # sibling to livekit-gateway/
git pull
export HOST_IP=$(hostname -I | awk '{print $1}')       # EC2 private IPv4
docker compose -f docker-compose.lkp-vs-lkg.yml build livekit-gateway-agent mock-services
python -m venv .venv && .venv/bin/pip install -e .     # gateway path needs NO livekit/livekit-api
mkdir -p ~/bench-out
```

Record the instance type (`ec2-metadata -t` or `curl .../instance-type`) in your
final report — use a DEDICATED type (c6i/c7g/m6i…), NOT burstable t2/t3/t4g
(CPU credits throttle sustained load and invalidate the numbers).

## NON-NEGOTIABLE invariants (each was a bug that cost hours — do not violate)

1. **`PROFILE=0` for all capacity/CPU numbers** (it's the default). py-spy runs
   IN the container and on the 400%-capped cgroup costs ~180% CPU — it both
   inflates `docker stats` AND starves the workload, fabricating fake CPU
   saturation. Only use `PROFILE=1` (single instance, rate=20) when you
   specifically want a flamegraph SHAPE, and NEVER trust CPU from that run.

2. **`HOST_IP` must be set for EVERY `docker compose` call, including `down`.**
   The compose file interpolates livekit-server's `${HOST_IP:?...}` even on
   teardown; unset → teardown errors → leaked container eats RAM → later runs
   fail mysteriously. The harness `run_c15_lkg_profile.sh` already guards this
   internally; you must guard your own manual compose calls.

3. **The harness already gates on worker registration** (waits for the gateway
   to log `worker registered`). Do not replace it with a fixed sleep — the agent
   takes a VARIABLE ~10-15 s to register and the gateway drops any call that
   arrives first (`NoWorkers`, no retry). This caused 0/N flakes.

4. **Watch for host (client-side) OOM**, exit code 137 or `sessions=N
   with_output=0 frames: 0 sent`. That means the BENCH CLIENT died, not the
   agent. The client buffers all received audio in RAM; long calls × high
   concurrency can exhaust it. If you see this, either run the client on a
   SEPARATE box (strongly preferred — see below) or reduce duration/concurrency.

### Strongly recommended: drive the client from a second box

The client competes with the 8 GB agent container for host RAM. To measure the
AGENT's true limits, run the agent stack on the EC2 box and the bench client on
a separate machine pointed at it:

```bash
# on the client box (any machine with network access to the agent):
.venv/bin/python -m load_test.cli --profile cN --target livekit-gateway \
  --no-docker --lkg-url ws://<AGENT_EC2_PRIVATE_IP>:8084 --output out.json
```
Open security-group ingress on 8084 (and 7884 if needed). If you only have one
box, run single-box but treat any 0/N or exit-137 as a CLIENT artifact, retest.

## Phase 1 — capacity ceiling (process, PROFILE=0)

Find where the AGENT chokes. Start low (EC2 is weaker than the Mac). Use idle =
concurrency + 2 so the idle pool isn't the limiter in this phase.

```bash
export LKG_LOG_LEVEL=warn ENABLE_VAD=true ENABLE_TURN_DETECTOR=true
export JOB_EXECUTOR_TYPE=process CPU_LIMIT=4.0 MEM_LIMIT=8G PROFILE=0
# step:warmup_skip:idle  — extend/trim around the knee
for spec in c8:12:10 c10:12:12 c12:12:14 c15:12:17 c18:14:20 c20:15:22 c25:25:27; do
  STEP=${spec%%:*}; r=${spec#*:}; SKIP=${r%%:*}; IDLE=${r##*:}
  NUM_IDLE_PROCESSES=$IDLE STEP=$STEP WARMUP_SKIP_SEC=$SKIP \
    RESULTS_DIR=~/bench-out/cap-$STEP ./run_c15_lkg_profile.sh
  HOST_IP=$HOST_IP docker compose -f docker-compose.lkp-vs-lkg.yml down -v >/dev/null 2>&1
done
```

The ceiling = highest STEP where: `with_output == total_sessions` (100%), AND
within-phrase p99 ≤ 30 ms, AND audible-silence p90 ≤ 5 ms, AND mean CPU ≤ ~320%
(80% of the 400% cap). Read each cell's `lkg-<step>.out`. Note whether the limit
is CPU (mean approaching cap) or memory (peak near 8 GB) — on the Mac it was
memory; on weaker EC2 cores it may become CPU.

## Phase 2 — minimum NUM_IDLE_PROCESSES with REALISTIC calls

Take the Phase-1 ceiling concurrency (call it C). Add a long-call profile for it
(300 s calls, 2 s arrival = realistic low churn). Edit `load_test/profiles.py` —
copy the `c30_long` line, rename to `cC_long`, set `concurrency=C`, keep
`duration_sec=300, ramp_delay=2.0`, set `warmup_sec≈C*2+10`:

```python
"cC_long": LoadProfile("cC_long", [LoadStep(concurrency=C, duration_sec=300, ramp_delay=2.0, warmup_sec=<C*2+10>)]),
```

Then sweep idle DOWN from C to find the smallest pool that still holds 100%:

```bash
for idle in C $((C*3/4)) $((C/2)) $((C/3)) $((C/4)); do
  NUM_IDLE_PROCESSES=$idle STEP=cC_long WARMUP_SKIP_SEC=<C*2+10> \
    RESULTS_DIR=~/bench-out/idle-$idle ./run_c15_lkg_profile.sh
  HOST_IP=$HOST_IP docker compose -f docker-compose.lkp-vs-lkg.yml down -v >/dev/null 2>&1
done
```

The answer = the lowest idle that still delivers 100% sessions under long calls,
and the memory it costs. EXPECT this to be well below C (unlike the Mac's
short-call result) — that's the whole point. If even idle=C/4 holds 100%, push
lower. Note: EC2's slower cores mean SLOWER prewarm → lower refill rate → the
minimum idle may be higher than the pure-arrival-rate math predicts; measure,
don't assume. If a run shows 0/N at low idle but the agent isn't CPU/mem-bound,
that's pool starvation (calls arriving faster than refill) — the real signal.

## Phase 3 (optional lever) — English-only EOU turn detector

Multilingual Qwen2 EOU is the biggest per-worker memory chunk (~104 MB) and the
slowest part of prewarm. If the workload is English, swapping to the English EOU
model shrinks per-worker memory AND speeds prewarm (→ higher refill rate → even
smaller viable idle pool). The model files are already pre-downloaded for both
variants. To test, in `load_test/servers/livekit_gateway_server.py` the turn
detector uses `MultilingualModel()`; try the English `EOUModelType="en"` variant
and re-run Phase 1+2. Report the memory/prewarm/ceiling delta. (Confirm with the
product owner whether English-only is acceptable before recommending it.)

## What to measure per cell (all in `lkg-<step>.out`)
- `sessions=N | with_output=M` — M/N is delivery; must be 100%.
- Mean / peak CPU (% of 400% cap), Mean / peak memory (vs 8192 MB cap).
- Within-phrase gap p99 (gate ≤30 ms), Audible silence gap p90/p99 (gate ≤5 ms),
  Post-warmup RTT, First-frame.
- Distinguish a real agent limit (CPU near cap OR mem near 8 GB) from a client
  artifact (0/N + 0 frames sent + low agent CPU) — retest artifacts.

## Deliverables
1. Instance type + a capacity table (concurrency × sessions/CPU/mem/gates) →
   the EC2 ceiling.
2. The minimum `NUM_IDLE_PROCESSES` for that ceiling under realistic (300 s)
   calls, with the memory it uses — this is the production recommendation.
3. Whether the binding constraint on EC2 is CPU or memory.
4. (If Phase 3 run) English-EOU deltas.
5. A short markdown report next to the results; do NOT trust any CPU number from
   a PROFILE=1 run.
```
