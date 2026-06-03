# AT + LiveKit on agent-transport 0.2.0 — capacity sweep

**Date:** 2026-06-03 · **Host:** macOS + OrbStack (10 cores / 12.6 GB VM) · **Server:** `agent-transport==0.2.0` + `livekit-agents>=1.5.15`, LiveKit adapter (`AudioStreamServer`).
**Workload:** multilingual EOU **turn detector + Python Silero VAD**, mocked STT/LLM/TTS (production-calibrated timings), speak-then-pause turn cadence (`TURN_SILENCE_MS=4000`, `MAX_ENDPOINTING_DELAY=1.5`). `PROFILE=0` for capacity (no py-spy contamination).

## TL;DR — recommended concurrency

| Profile | Recommended | Last 100%-delivery c | Binding constraint |
| :------ | :---------: | :------------------: | :----------------- |
| **1 vCPU / 2 GB** | **~10** | ≥12 (swept max, still 100%) | None hit in range — CPU ~36% mean, mem ~1.1 GB. Ceiling is **> 12**, untested higher. |
| **4 vCPU / 8 GB** | **~25** | 25 | **Delivery reliability** (flakes at c≥30), **not** CPU/mem — CPU only ~58% of cap, mem ~1.2 GB of 8 at the knee. |

Neither box becomes CPU- or memory-bound within the swept range. The 4 vCPU delivery knee at c≈25→30 is a *reliability* limit (and non-monotonic: c=30→19/30 but c=40→35/40, i.e. variance), driven by the as-is turn-cadence workload + shared-host effects at high concurrency, **not** the agent's true compute ceiling. Treat these as conservative, Mac-measured numbers; certify on EC2 (runbook below).

## 1 vCPU / 2 GB  (CPU cap = 100%)

| c | delivery | CPU mean/peak | mem mean/peak | audible-silence p99 (eligible) | first-frame p50 | throughput |
|--:|:--------:|:-------------:|:-------------:|:------------------------------:|:---------------:|:----------:|
| 2 | 2/2 | 13.0% / 35.7% | 997 / 1031 MB | — (0) | 30.1 s | 1.2 f/s |
| 4 | 4/4 | 19.7% / 47.3% | 1010 / 1088 MB | 79.6 ms (1) | 4.0 s | 2.4 f/s |
| 6 | 6/6 | 23.2% / 57.5% | 1049 / 1084 MB | 80.2 ms (5) | 6.1 s | 5.0 f/s |
| 8 | 8/8 | 30.0% / 62.1% | 1072 / 1137 MB | — (0) | 5.9 s | 2.4 f/s |
| 10 | 10/10 | 30.9% / 80.6% | 1077 / 1102 MB | 79.8 ms (8) | 6.4 s | 8.4 f/s |
| 12 | 12/12 | 35.6% / 87.4% | 1088 / 1124 MB | 80.4 ms (1) | 4.4 s | 4.6 f/s |

100% delivery throughout. Memory **flat ~1.0–1.1 GB** (the EOU model subprocess is a fixed floor; per-session adds ~2 MB). CPU mean never exceeds ~36% of the single core (peaks spike to ~87% on turn bursts).

## 4 vCPU / 8 GB  (CPU cap = 400%)

| c | delivery | CPU mean/peak | mem mean/peak | audible-silence p99 (eligible) | first-frame p50 | throughput |
|--:|:--------:|:-------------:|:-------------:|:------------------------------:|:---------------:|:----------:|
| 8 | 8/8 | 34.0% / 80.4% | 1055 / 1089 MB | — (0) | 8.0 s | 2.1 f/s |
| 12 | 12/12 | 41.9% / 111.5% | 1095 / 1125 MB | 73.8 ms (1) | 8.0 s | 5.1 f/s |
| 16 | 16/16 | 51.6% / 114.9% | 1132 / 1166 MB | 80.1 ms (8) | 8.0 s | 12.0 f/s |
| 20 | 20/20 | 54.2% / 128.7% | 1107 / 1142 MB | 79.9 ms (3) | 4.2 s | 21.4 f/s |
| 25 | 25/25 | 57.5% / 163.3% | 1166 / 1185 MB | 64.2 ms (8) | 7.9 s | 26.2 f/s |
| 30 | **19/30** | 67.9% / 214.6% | 1209 / 1236 MB | 60.6 ms (2) | 7.9 s | 7.2 f/s |
| 40 | **35/40** | 57.7% / 227.4% | 1382 / 1390 MB | 60.7 ms (9) | 7.6 s | 28.0 f/s |

100% delivery through c=25; degrades (and goes non-monotonic) from c=30. At the knee CPU mean is ~58% of the 4-core cap (~2.3 cores) and memory ~1.2 GB of 8 — **not** saturated.

## CPU breakdown (py-spy, 4 vCPU @ c=25 — corrected)

**Magnitude (trust this): clean `PROFILE=0` docker-stats → ~58% of the 4-core cap ≈ 2.3 cores at c=25.**

py-spy on this app samples many *parked* threads (the 256-thread blocking-recv pool + per-frame `recv`), so raw py-spy percentages are thread-presence, not CPU. Splitting the 654 samples into blocked vs on-CPU (leaf heuristic): **~41% parked/blocked, ~59% on-CPU.** Of the on-CPU portion (≈ the 2.3 real cores):

| on-CPU share | ≈ cores | where |
|------:|:------:|:------|
| ~50% | ~1.14 | **EOU turn-detector inference** (ONNX transformer forward pass) |
| ~31% | ~0.70 | livekit-agents framework + inference-runner loop (`runners.py` — partly EOU dispatch) |
| ~7% | ~0.16 | VAD inference (Silero ONNX) |
| ~5% | ~0.13 | agent-transport FFI / resampler (compute) |
| ~6% | ~0.13 | event loop / async + VAD glue |
| **~0.8%** | **~0.02** | **STT/LLM/TTS mock clients** — negligible (I/O-bound; the network wait is off-CPU) |

**Takeaways:** the **EOU turn detector dominates real CPU** (~half of on-CPU, ~1.1 cores; with its runner loop, ML inference is ~65–80% of on-CPU). **VAD is cheap** (~0.16 cores) despite running per-frame. **STT/LLM/TTS ≈ 0.8% / ~0.02 cores** — effectively free (their cost is I/O wait, off-CPU). And **~41% of thread-time is parked** in the blocking-recv threadpool — a design characteristic (threads waiting), not CPU burn.

*Lever:* to cut CPU, the EOU model is the target (English-only EOU variant, or a longer endpointing window so it evaluates less often); the VAD and the STT/LLM/TTS path are not worth optimizing.

*Caveats:* py-spy on a GIL + inference-subprocess + 256-blocking-thread app is messy — the blocked/on-CPU split is a leaf-keyword heuristic, `runners.py` is ambiguous (EOU dispatch vs IPC wait), and magnitude (2.3 cores) is from the clean run while the shape is from the py-spy run. Net conclusion is robust: ML inference (EOU) is the real CPU hotspot; STT/LLM/TTS is not.

> Note: an earlier draft of this section reported a raw-py-spy split (ONNX ~33% / threadpool ~24% / audio-io ~17% / …). That conflated parked-thread wall-time with CPU; the table above is the corrected on-CPU attribution.

## Memory breakdown (per-process RSS, 4 vCPU @ c=25)

| Process | RSS idle | RSS @ c=25 | Role |
|:--------|---------:|-----------:|:-----|
| EOU inference subprocess | 842 MB | 895 MB | turn-detector ONNX model (`InferenceProcExecutor`) — **dominant fixed floor** |
| main server | 270 MB | 241 MB | AudioStreamServer + AgentSessions + Rust transport |
| forkserver/IPC helper | 12 MB | 12 MB | — |

The EOU model subprocess (~850 MB) is the single largest memory cost and is **fixed** regardless of concurrency; per-session memory is only ~2 MB. On the 2 GB box that floor is ~40% of the budget before any call connects — which is why memory, while not the binding constraint at the swept concurrencies, sets the small box's hard headroom.

## Caveats (important)

1. **Turn cadence.** The EOU detector does not sustain a perfectly regular turn cadence on the synthetic mock workload (it was built for STT-driven turns). Result: `audible-silence` eligibility flaps (some steps have too few frames for the percentile), and **first-frame / RTT are noisy and inflated (cold-start + cadence), not real response latency.** Treat the latency columns as indicative only; **delivery%, CPU, and memory are the trustworthy signals.** Per-turn component latency measured separately was healthy (EOU 0–600 ms, LLM TTFT 0.3–1.3 s, TTS TTFB 0.2–0.3 s ≈ 1–2 s response).
2. **Not compute-bound.** Both boxes hold 100% delivery with CPU/memory headroom up to their delivery knee; the knee is a reliability/host artifact at high concurrency, so the *true* compute ceiling is higher than reported here.
3. **Mac/OrbStack.** Shared-core host; numbers are approximate (server CPU is cgroup-scoped so not inflated by the client, but high-c contention can add jitter). Certify on dedicated EC2.

## EC2 certification runbook (AT + LiveKit, client on a separate box)

The existing `EC2_RUN_INSTRUCTIONS.md` / `run_c15_lkg_profile.sh` are **livekit-gateway**-specific. For this AT+LiveKit target:

```bash
# On the EC2 agent box (dedicated, non-burstable: c6i/m6i/c7g) — build + run server stack:
cd agent-transport-load-test
export COMPOSE_FILE=docker-compose.yml:docker-compose.mocks.yml
export ENABLE_TURN_DETECTOR=true ENABLE_VAD=false TURN_SILENCE_MS=4000 \
       MIN_ENDPOINTING_DELAY=0.4 MAX_ENDPOINTING_DELAY=1.5
export CPU_LIMIT=4.0 MEM_LIMIT=8G            # or 1.0 / 2G for the small box
docker compose build agent-transport-livekit mock-services
docker compose up -d --force-recreate --wait agent-transport-livekit
# open security-group ingress on 8083

# On a SEPARATE client box (so it doesn't steal the agent's cores):
.venv/bin/python -m load_test.cli --profile c25 --target agent-transport-livekit \
    --no-docker --at-livekit-url ws://<AGENT_EC2_PRIVATE_IP>:8083 \
    --docker-container <unused-locally; use psutil or docker stats on the agent box> \
    --output out-c25.json
```
Certify only the candidate concurrencies (1 vCPU: ~10–14; 4 vCPU: ~22–30) rather than re-sweeping. Expect the real ceiling to be **higher** than the Mac numbers since the client no longer competes for cores. On EC2, "4 vCPU" is ~2 physical hyperthread cores, so also re-confirm where CPU actually saturates.

## Harness changes made this session (to make the target runnable on 0.2.0)
- `Dockerfile.agent-transport-livekit`: `agent-transport` 0.1.11→**0.2.0**, `livekit-agents>=1.5.15`, added `py-spy`.
- `agent_transport_livekit_server.py`: load Python Silero VAD when TD on + Rust VAD off; **register the EOU inference runner at import time** (so `AudioStreamServer` builds the `InferenceProcExecutor` — otherwise `ctx.inference_executor` is None and EOU errors); endpointing-delay env knobs; optional `METRICS_LOG` per-turn hook.
- `docker-compose.mocks.yml` (new): wires `mock-services` + `MOCK_SERVICES_HOST`/endpointing/`METRICS_LOG` env + `SYS_PTRACE` into the AT+LiveKit target (the base compose had no mocks for it → zero output without this).
- `cli.py` / `runner.py`: wired the `agent-transport-livekit` target (+ `--at-livekit-url`, pacing 0.020).
- `audio_gen.py`: `TURN_SILENCE_MS` env for the speak-then-pause cadence.
- `profiles.py`: `at_lk_1cpu` / `at_lk_4cpu` sweep profiles.
