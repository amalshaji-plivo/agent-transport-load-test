# EC2 process-mode benchmark findings — livekit-gateway s2 (VAD + multilingual TD)

**Instance:** c6in.2xlarge (8 vCPU / 15 GB host), agent cgroup capped at **4 vCPU / 8 GB**
**Mode:** `JOB_EXECUTOR_TYPE=process`, VAD on, multilingual turn detector on
**Gateway:** `livekit-gateway==0.1.2` (PyPI wheel; no Rust source build on this box)
**Bench client:** containerized (AL2 glibc 2.26 can't install agent-transport host-side)

## Two ceiling-masking issues found & fixed before any valid number

The documented methodology could not produce a meaningful ceiling on EC2 until
two distinct artifacts were corrected:

1. **Turn-discard (barge-in).** The bench loops a ~1.3 s speech+silence audio
   cycle, faster than EC2's slow cores complete a turn (LLM TTFT + TTS). With
   default interruptions ON, each new "user speech" cancelled the in-flight
   response → only **~7 % of LLM turns reached TTS** (96 LLM → 7 TTS at c10),
   and whole sessions landed zero completed turns (`with_output` 6/10). Fixed
   with `allow_interruptions=false` (env `ALLOW_INTERRUPTIONS`, default false) →
   **100 % TTS:LLM**.

2. **`load_threshold` availability gate.** livekit-agents' Worker marks itself
   UNAVAILABLE once its load estimate exceeds `load_threshold` (production
   default **0.7**) and refuses new jobs. On 4 vCPU that tripped at **~6
   concurrent sessions**, well below real resource limits — capping working
   sessions at 6 regardless of `c`, at only ~40 % CPU. Fixed with
   `load_threshold=inf` (env `LOAD_THRESHOLD`) → sessions scale to the real
   resource ceiling.

Neither is in the upstream EC2_RUN_INSTRUCTIONS.md; both were required to get
valid capacity numbers on this hardware.

## Phase 1 — capacity ceiling (process, PROFILE=0, both fixes applied)

idle = concurrency + 2. Gates: 100 % sessions, within-phrase p99 ≤ 30 ms,
audible-silence p90 ≤ 5 ms, mean CPU ≤ 320 % (80 % of the 400 % cap).

| c | sessions | CPU mean | CPU peak | mem peak | wphase p99 | sil p90 | sil p99 | gates |
|---|---|---|---|---|---|---|---|---|
| c8  | 8/8   | 60 %  | 211 % | 4.0 GB | 20.8 ms | 0.39 | 0.8  | **PASS** |
| c15 | 15/15 | 192 % | 412 % | 5.9 GB | 21.8 ms | 0.38 | 1.8  | **PASS** |
| c16 | 16/16 | 214 % | 413 % | 6.0 GB | 23.7 ms | 0.42 | 1.x  | **PASS** |
| c17 | 17/17 | 214 % | 410 % | 6.0 GB | 25.1 ms | 0.45 | 1.x  | **PASS (ceiling)** |
| c18 | 18/18 | 224 % | 407 % | 6.4 GB | 42.0 ms | 0.54 | 22.0 | FAIL (wphase) |
| c20 | 20/20 | 252 % | 409 % | 6.9 GB | 60.8 ms | 0.52 | 40.8 | FAIL (wphase) |
| c25 | 25/25 | 128 % | 413 % | 8.2 GB (cap) | 65.7 ms | — | — | FAIL (wphase + mem cap) |
| c30 | 8/30  | 26 %  | 74 %  | 6.4 GB | — | — | — | COLLAPSE |
| c35 | 0/35  | 179 % | 426 % | — | — | — | — | COLLAPSE |

**Ceiling = c17** (highest cell passing all gates: 17/17, wphase p99 25.1 ms,
silence p90 0.45 ms, CPU mean 214 % < 320 % gate, peak pinned at the 410 % cap).
within-phrase p99 climbs gently 21.8 → 23.7 → 25.1 ms across c15-c17, then jumps
to 42 ms at c18 — the knee where peak-CPU saturation starts starving the audio
pipeline.

## Binding constraint: CPU, not memory

- **Peak CPU pegs at the ~410 % (4-vCPU) cap from c15 onward.** Beyond c15 the
  audio pipeline is starved during peak bursts → within-phrase gap p99 blows
  past the 30 ms gate (42 ms at c18, 61 ms at c20).
- Memory only reaches 6.9 GB at c20; it doesn't hit the 8 GB cap until c25.
- This is the **opposite of the Mac** (which was memory-bound at c30). EC2
  "4 vCPU" = ~2 physical hyperthreaded cores, so CPU saturates first — exactly
  as the instructions predicted (on-EC2 optimum ≈ c15).
- c30/c35 collapse: past the combined CPU+mem ceiling, the fork-storm during
  ramp can't sustain the workers and most/all sessions fail to establish.

## Phase 2 — minimum NUM_IDLE_PROCESSES at the ceiling (C=17), realistic 300s calls

2 s arrival (0.5 calls/s), 300 s calls → 17 calls ramp over ~34 s, then ~0 churn.
Sweep idle DOWN from C; find smallest pool holding 100 % sessions + gates.

| idle | sessions | CPU mean/peak | mem peak | wphase p99 | verdict |
|---|---|---|---|---|---|
| 17 (C)   | 17/17 | 204 % / 385 % | 5.7 GB | 21.5 ms | PASS |
| 13 (¾C)  | 17/17 | 262 % / 382 % | 5.2 GB | 23.2 ms | PASS |
| 9 (½C)   | 17/17 | 264 % / 376 % | 4.5 GB | 23.2 ms | PASS |
| 6 (⅓C)   | 17/17 | 253 % / 357 % | 4.0 GB | 23.2 ms | PASS |
| 4 (¼C)   | 17/17 | 248 % / 385 % | 3.6 GB | 21.6 ms | PASS |
| 2        | 17/17 | — / —         | 3.3 GB | 21.5 ms | PASS |
| **1**    | **17/17** | 258 % / 376 % | **3.2 GB** | 22.2 ms | **PASS** |

**The floor is idle=1 — every level from 17 down to 1 holds 17/17 with clean
latency.** This decisively confirms the hypothesis: the short-call
"idle = concurrency" requirement was a churn artifact. Under realistic long
(300 s) calls, workers spawn on demand and the slow arrival (0.5 calls/s) lets
the pool keep up, so the idle pool is almost irrelevant to CAPACITY. Memory
drops monotonically 5.7 GB (idle=17) → 3.2 GB (idle=1), a 2.5 GB saving.

**Caveat — idle is a LATENCY knob, not a capacity knob.** idle=1 holds 17/17
only because the benchmark's `warmup_sec=44` discards the ramp where on-demand
workers are still spawning (~12 s cold-start each). In production those first
calls would hit cold-start latency. Size idle to the *arrival burst*, per the
governing rule `idle ≥ ceil(peak_arrival_rate × prewarm_seconds)`: at 0.5
calls/s × ~12 s ≈ **6**. idle=6 costs only 4.0 GB and eliminates cold-starts at
this arrival rate.

### Burst arrival — does idle=6 survive 17 simultaneous calls?

The Phase 2 sweep used staggered arrival (0.5-2 s between call starts). To test
the opposite extreme, `c17_burst` lands all 17 calls within ~1 s
(`ramp_delay=0.05`), with idle=6 — so 11 sessions must wait for on-demand worker
spawns during the burst.

| c | idle | arrival | sessions | CPU mean/peak | mem | wphase p99 | verdict |
|---|---|---|---|---|---|---|---|
| 17 | 6 | burst (~1 s) | **17/17** | 225 % / 402 % | 4.0 GB | 23.0 ms | **PASS** |

**idle=6 holds 17/17 even under burst arrival.** With `load_threshold=inf` the
gateway no longer rejects the 11 excess calls (the 0.7 gate would have
`NoWorkers`'d them) — it queues them and spawns workers on demand, which catch
up within the burst. Peak CPU briefly hits the 402 % cap during the spawn storm,
then settles.

**Caveat:** the 11 cold-spawned sessions each waited ~10-12 s for a worker during
the burst; `warmup_sec=20` discards that window, so the clean steady-state
percentiles are post-recovery. idle=6 holds for CAPACITY and steady-state
QUALITY under burst, but those callers see a one-time cold-start before the bot
first responds. To eliminate burst cold-starts, size idle to the burst (idle=17).

## Final recommendation (4 vCPU / 8 GB, process mode, VAD + multilingual TD)

| setting | value | rationale |
|---|---|---|
| **max concurrency (c)** | **17** | highest passing all gates; CPU-bound at the 4-vCPU cap |
| **NUM_IDLE_PROCESSES** | **6** (prod) / 1 (min) | covers 0.5 calls/s arrival without cold-start; capacity floor is 1 |
| MEM_LIMIT | 8 GB | only ~6 GB used at c17; not the binding constraint |
| CPU_LIMIT | 4.0 | **this is the binding constraint** |
| ALLOW_INTERRUPTIONS | false | else ~93 % of turns discarded under the bench's fast cadence |
| LOAD_THRESHOLD | inf (or ≥ raise from 0.7) | default 0.7 caps working sessions at ~6 |

- **Binding constraint: CPU.** Peak pegs the 4-vCPU cap from c15; latency breaks
  at c18. Memory tops out ~6 GB at the ceiling — 2 GB of headroom unused.
- **To raise the ceiling: add vCPUs, not memory.** Or cut ML cost — the
  multilingual Qwen2 EOU turn detector is ~36 % of on-CPU work (Phase 1.5);
  English-only EOU (Phase 3) would directly lift c17.
- **Horizontal scaling** is the path beyond c17/container: each box is
  CPU-saturated, memory-light, so pack 1 agent container per ~4 vCPU and scale
  out behind a sticky (per-call) WebSocket load balancer.
- **Long-call vs short-call CPU:** Phase 2 long-call cells peak at ~360-385 %
  (below the 410 % cap) vs Phase 1 short-call cells pinned at the cap — because
  steady-state c17 has no constant fork-storm. This is why latency stays clean
  at the ceiling under realistic call durations.

## Phase 1.5 — profile at the ceiling (PROFILE=1, c17, single py-spy @ rate 20)

On-CPU self-time (speedscope leaf view). Trust Phase 1 for CPU magnitude; this
is SHAPE only.

| component | self-time | notes |
|---|---|---|
| **ONNX inference** (`onnxruntime_inference_collection.py:322`) | **24.3 %** | Silero VAD + Qwen2 EOU — dominant real CPU |
| **Qwen2 EOU tokenization** (`tokenization_qwen2` + utils) | **~12 %** | turn-detector text tokenization — surprisingly costly |
| **IPC + FFI marshaling** (`_read_exactly` 5.4 %, `_ffi_client` req/dispose ~4 %) | **~12 %** | audio frames across the worker process boundary |
| JSON decode (`raw_decode`) | 5.2 % | mock STT/LLM/TTS response parsing |
| threadpool `_worker` | 10.5 % | mostly parked recv threads, not burning a core |
| asyncio loop wakeups (selector write) | ~3 % | |

**The ML pipeline (inference + tokenization ≈ 36 %) dominates on-CPU work.** The
multilingual Qwen2 EOU turn detector is the single biggest lever — both its
inference and ~12 % tokenization overhead. Phase 3 (English-only EOU) would
directly cut this and raise the ceiling. Artifact:
`~/bench-out/profile-c17/lkg-c17.speedscope.json` (open at speedscope.app).
