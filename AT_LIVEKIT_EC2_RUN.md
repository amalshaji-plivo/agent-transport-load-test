# Re-run the AT + LiveKit (agent-transport 0.2.0) capacity sweep on EC2

> **Copy-paste prompt for an EC2 Claude (or follow by hand):**
> Read `AT_LIVEKIT_EC2_RUN.md` and execute it. It re-runs the agent-transport **0.2.0** AT+LiveKit capacity sweep (multilingual EOU **turn detector + Python Silero VAD**) on this **dedicated** EC2 box to certify the Mac-measured ceilings and find the TRUE compute ceiling. The Mac run was *reliability-bound* (delivery flaked before CPU/mem saturated), partly from client/server core contention on a shared 10-core host — EC2 with spare cores (or a separate client box) removes that. Run the sweep inside **tmux**. Report the per-profile ceiling table, the binding constraint (CPU vs mem vs delivery), and a py-spy + per-process memory breakdown at the sweet spot. Do NOT change bench behavior — run as committed. Compare against `results-0.2.0-lk-td/REPORT.md` (the Mac baseline).

## Mac baseline to certify (from `results-0.2.0-lk-td/REPORT.md`)
- **1 vCPU / 2 GB:** 100% delivery through c=12 (top of sweep), CPU ~36% of 1 core, mem ~1.1 GB → **ceiling not reached (>12)**.
- **4 vCPU / 8 GB:** 100% delivery through c=25; flaky at c=30 (19/30, non-monotonic). CPU ~58% of 4 cores, mem ~1.2 GB → **reliability-bound, not CPU/mem-bound**.
- **Memory:** EOU model subprocess ~850 MB fixed floor + ~2 MB/session.
- **CPU:** dominated by EOU inference; VAD cheap; STT/LLM/TTS negligible (I/O-bound). Use **docker-stats** for CPU magnitude — py-spy reflects thread-presence on this app, not CPU.
- **EC2 reality:** "4 vCPU" ≈ ~2 physical cores (hyperthreads), so per-core throughput is *lower* than the Mac's full cores but contention-free. Expect the ceiling to land at a possibly different `c` — measure, don't assume.

## Instance & topology
- **Dedicated, non-burstable** instance only — c6i / c7i / m6i / c7g. **NOT** t2/t3/t4g (CPU credits throttle sustained load and invalidate the numbers).
- **Simplest + clean (recommended):** ONE box with cores to spare so the client never starves the capped agent — e.g. **c6i.4xlarge (16 vCPU / 32 GB)**. Cap the agent at 1 or 4 cores; client + OS use the rest. Run both profiles here.
- **Gold standard (optional):** two boxes — agent on A, bench client on B (`--no-docker --at-livekit-url ws://<A-private-ip>:8083`); sample `docker stats` on A. Zero contention.
- Unlike the livekit-gateway runbook, AT+LiveKit needs **no livekit-server, no HOST_IP, no WebRTC ports, no NUM_IDLE_PROCESSES** — it's a single-process WebSocket server on `:8083` with one inference subprocess. Simpler.

## Setup (agent box)
```bash
git clone https://github.com/amalshaji-plivo/agent-transport-load-test.git
cd agent-transport-load-test && git pull          # needs commit 7610ff2 or later
python3 -m venv .venv && .venv/bin/pip install -e .   # bench-client harness deps
export COMPOSE_FILE=docker-compose.yml:docker-compose.mocks.yml   # REQUIRED (wires mock-services)
docker compose build agent-transport-livekit mock-services
curl -s http://169.254.169.254/latest/meta-data/instance-type; echo   # record for the report
```

## Run the capacity sweep (PROFILE=0 — clean CPU/mem)
```bash
tmux new -s sweep            # so it survives SSH disconnects
bash run_at_lk_sweep.sh      # 1 vCPU/2G then 4 vCPU/8G, fresh container per step, 60s steady each
#   PROFILES=4cpu bash run_at_lk_sweep.sh   # just one profile
# watch:  tail -f results-0.2.0-lk-td/4cpu-8gb.out
```
`run_at_lk_sweep.sh` sets the validated env itself (turn detector + Python VAD, `TURN_SILENCE_MS=4000`, `MAX_ENDPOINTING_DELAY=1.5`, `PROFILE=0`) and writes per-step summaries to `results-0.2.0-lk-td/<profile>.out` + `progress.log`.

**Push past the Mac range** (recommended — the Mac never hit the 1 vCPU ceiling, and 4 vCPU may hold higher without contention). Edit `load_test/profiles.py` (`at_lk_1cpu` → add 14,16,18,20; `at_lk_4cpu` → add 50,60), or run extra single-c steps:
```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.mocks.yml
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false TURN_SILENCE_MS=4000 \
       MIN_ENDPOINTING_DELAY=0.4 MAX_ENDPOINTING_DELAY=1.5 PROFILE=0 CPU_LIMIT=4.0 MEM_LIMIT=8G
C=agent-transport-load-test-agent-transport-livekit-1
docker compose up -d --force-recreate --wait agent-transport-livekit; sleep 6
.venv/bin/python -m load_test.cli --profile c50 --target agent-transport-livekit --no-docker \
   --at-livekit-url ws://localhost:8083 --docker-container $C --output results-0.2.0-lk-td/4cpu-c50.json
docker compose down
```
(Single-c profiles `c8 c10 … c50` already exist; they're 30 s steady vs the sweep's 60 s — fine for the CPU-saturation question, slightly noisier on the silence metric.)

### Two-box mode (optional)
Agent box: bring the container up (env above), open security-group ingress on 8083.
Client box (separate): `... --no-docker --at-livekit-url ws://<AGENT_PRIVATE_IP>:8083 --output out.json`.
Capture CPU/mem on the agent box during the run: `while :; do docker stats $C --no-stream --format '{{.CPUPerc}} {{.MemUsage}}'; sleep 1; done | tee cpu-mem.log`.

## Profile the sweet spot (py-spy CPU + per-process memory)
Pick the highest `c` with 100% delivery where CPU isn't pegged, then (PROFILE=1 — py-spy contaminates CPU, so it's shape-only; trust docker-stats for magnitude):
```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.mocks.yml
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false TURN_SILENCE_MS=4000 MAX_ENDPOINTING_DELAY=1.5 CPU_LIMIT=4.0 MEM_LIMIT=8G
C=agent-transport-load-test-agent-transport-livekit-1
docker compose up -d --force-recreate --wait agent-transport-livekit; sleep 6
echo "MEM @ idle:";  docker exec $C ps -eo pid,rss,comm --sort=-rss | head -12
.venv/bin/python -m load_test.cli --profile c25 --target agent-transport-livekit --no-docker \
   --at-livekit-url ws://localhost:8083 --docker-container $C --output results-0.2.0-lk-td/prof.json &
sleep 12
echo "MEM @ load:";  docker exec $C ps -eo pid,rss,comm --sort=-rss | head -16
docker exec $C py-spy record --pid 1 --subprocesses --rate 20 --duration 25 --format speedscope --output /tmp/prof.json
docker cp $C:/tmp/prof.json results-0.2.0-lk-td/prof.speedscope.json
wait; docker compose down
```
For a CPU-by-module breakdown that isn't fooled by the ~256 parked recv-threads, split the speedscope samples into **blocked vs on-CPU** first — see the corrected method/script notes in `results-0.2.0-lk-td/REPORT.md` (CPU breakdown section).

## What to read per step (`results-0.2.0-lk-td/<profile>.out`)
- `sessions=N | with_output=M` → delivery; **ceiling = highest c where M == N**.
- Mean/peak CPU (docker-stats; cap = 100% × vCPU). Mean ≳ 80% of cap ⇒ CPU-bound.
- Mean/peak memory vs cap (EOU floor ~850 MB).
- Audible-silence gap p50/p99 (eligible sessions only) → output smoothness.
- First-frame / RTT → **indicative only** (see caveat #4).
- Throughput (f/s).

## Non-negotiables & caveats
1. **PROFILE=0 for every capacity number.** py-spy inside the capped cgroup costs ~180% CPU and fabricates saturation. PROFILE=1 only for a flamegraph shape; never trust its CPU/mem.
2. **Dedicated instance** (no t-series). Record the instance type in the report.
3. **The two hard-won fixes are already committed** — mock-services wiring (the `docker-compose.mocks.yml` overlay; you MUST keep `COMPOSE_FILE` set to include it or the server produces **zero output**) and the EOU inference-runner registration. Don't re-debug them.
4. **Turn-cadence caveat:** the EOU detector doesn't sustain a perfectly regular cadence on the synthetic mock workload, so audible-silence eligibility flaps and first-frame/RTT are noisy/inflated. **Delivery %, CPU, and memory are the trustworthy signals; latency is indicative.**
5. **CPU magnitude = docker-stats, not py-spy.** On this app (GIL + inference subprocess + 256 blocking recv threads) ~40% of py-spy samples are parked threads; it's good for "what's the on-CPU hotspot" (EOU inference), not for absolute CPU.
6. **Run in tmux/nohup.** A detached background shell was flaky on the dev box; foreground-in-tmux is reliable.

## Deliverables
1. Instance type + per-profile capacity table (c × delivery × CPU × mem × silence × first-frame × throughput).
2. Certified ceiling per profile + the binding constraint (CPU vs mem vs delivery), and whether EC2 holds **higher** than the Mac once client contention is gone.
3. py-spy flamegraph + per-process memory breakdown at the sweet spot.
4. A short markdown report next to the results, diffed against the Mac baseline in `results-0.2.0-lk-td/REPORT.md`.
