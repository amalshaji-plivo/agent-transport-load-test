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

### Initial-response (cold-start) investigation — burst vs ramp arrival

Capacity gates (sessions + steady-state latency) pass everywhere above, but they
discard the ramp via `warmup_sec`. The user-facing question is **how long after
a call connects does the bot first speak** — i.e. time-to-first-turn. Measured
as the first EOU (turn-detected) event per session.

**Per-worker prewarm ≈ 2.5-3 s** (observed `elapsed_time` 2.4-3.0 s; Silero VAD +
Qwen2 EOU, EOU files pre-baked so no download). A call that arrives with no warm
worker waits for an on-demand spawn. The pain is **contention**: when many
spawn at once on 4 vCPUs they queue, and they also compete with active sessions.

**Burst (17 calls within ~1 s) — first-EOU spread per session:**

```
idle=6:   1.7 1.7 1.8 1.8 1.9 1.9 | 4.7 4.7 4.8 4.8 7.4 8.0 8.8 9.1 9.2 10.1 10.3
          └── 6 warm: ~1.7s ──┘     └─── 11 cold-spawned: 4.7-10.3 s ───┘   wphase p99 23ms (PASS)
idle=17:  1.7 .. 2.5 (15 sessions)  | 4.9 5.0 (2 stragglers)                  wphase p99 51ms (FAIL)
```

Neither idle setting is clean at the ceiling under burst:
- **idle=6** → 11 sessions cold-start (first response 4.7-10.3 s = dead air on a
  phone call). Steady-state audio is fine once up.
- **idle=17** → first response fixed (15/17 under 2.6 s), BUT draining all 17
  warm workers in ~1 s triggers a **simultaneous refill of 17 new workers** that
  storms the already-CPU-saturated box → within-phrase p99 jumps to **51 ms**
  (fails the ≤30 ms gate). The audio of the live calls stutters.

**Ramp (2 s between calls) — same c=17, same idle=17:**

| arrival | wphase p99 | CPU peak | first-response |
|---|---|---|---|
| **ramp** (2 s/call) | **21.5 ms** ✓ | 385 % | ~1.6 s after each call connects |
| **burst** (~1 s) | 51.2 ms ✗ | 408 % | 1.7 s (most), refill-storm jitter |

**The initial-response problem is burst-specific — ramp is clean.** Under ramp,
arrivals and pool refills are spread over time, so no spawn storm: each call gets
a warm worker (or a just-finished ~3 s spawn) and answers ~1.6 s after it
connects, with steady-state audio passing all gates. This holds even at small
idle (idle=1 ≈ idle=17 under ramp) because spawns happen one-at-a-time without
contention.

**Root cause:** at the c17 ceiling the 4 vCPUs are ~saturated by the live
sessions, leaving no headroom to absorb a *simultaneous* prewarm storm — whether
that storm is cold-start spawns (idle<burst) or pool refills (idle≥burst). Only
burst arrival creates a simultaneous storm; staggered/ramp arrival never does.

**Fixes for burst-arrival traffic (ranked):**
1. **Run below the ceiling.** At c≤~12 there's CPU headroom to absorb a prewarm
   storm; burst first-response and steady audio both stay clean. (Trade capacity
   for burst-robustness.)
2. **Cheaper prewarm** — English-only EOU instead of multilingual Qwen2 cuts the
   ~2.5-3 s spawn cost and the ~36 % ML CPU, shrinking the storm.
3. **Rate-limit pool refill** so it doesn't spawn all replacements at once
   (livekit-agents pool tuning).
4. **More vCPUs** — headroom for both sessions and prewarm.
5. **Smooth arrivals** (admission control / dialer pacing) so a true 17-in-1-s
   burst never reaches one container; route bursts across replicas.

**For staggered/steady telephony traffic (the realistic case): no action needed
— c17 with a modest idle (≈6) answers every call in ~1.6 s with clean audio.**

### Burst ceiling — highest c that survives a ~1 s burst cleanly

Sweep c at burst arrival with **idle = c** (pool covers the burst → no cold-start;
isolates the refill-storm effect on steady audio). Gate: within-phrase p99 ≤ 30 ms,
silence p90 ≤ 5 ms, 100 % sessions, and first-response tight (~1.7 s, no dead air).

| c | idle | sessions | wphase p95 | wphase p99 | sil p90 | first-response | mem | gate |
|---|---|---|---|---|---|---|---|---|
| 8  | 8  | 8/8   | 20.5 | 20.8 ms | 0.36 | 1.6-3.2 s | — | PASS |
| 10 | 10 | 10/10 | 20.5 | 20.8 ms | 0.32 | 1.6-2.2 s | — | PASS |
| 12 | 12 | 12/12 | 20.6 | 21.0 ms | 0.37 | 1.6-2.2 s | — | PASS |
| 14 | 14 | 14/14 | 20.7 | 21.3 ms | 0.38 | 1.7-2.4 s | — | PASS |
| **15** | **15** | **15/15** | **20.8** | **23.8 ms** | **0.46** | **1.7-3.4 s** | **5.3 GB** | **PASS (ceiling)** |
| 16 | 16 | 16/16 | 21.0 | 30.9 ms | 0.50 | 1.6-2.5 s | — | FAIL (marginal) |
| 17 | 17 | 17/17 | — | 51.2 ms | 0.51 | 1.7-2.5 s | 5.8 GB | FAIL |

**Burst ceiling = c15** (idle=15). With the pool sized to the burst, every call
gets a warm worker — first-response is the normal ~1.7 s pipeline, no cold-start
dead air — and the 15-worker refill storm still fits the CPU headroom (within-
phrase p99 23.8 ms). At c16 the storm pushes p99 to the 30 ms edge; at c17 it
blows to 51 ms.

**Two ceilings, by arrival pattern:**

| arrival | ceiling | idle | first-response | why |
|---|---|---|---|---|
| ramp / steady (2 s+ between calls) | **c17** | ~6 | ~1.6 s | no spawn storm; on-demand refill keeps up |
| burst (all within ~1 s) | **c15** | **= c (15)** | ~1.7 s | idle must cover the burst (no cold-start); refill storm caps audio at c15 |

The burst ceiling (c15) costs ~2 fewer concurrent calls than the ramp ceiling
(c17) AND a much larger idle pool (15 vs 6, ≈ +1.4 GB) — the price of burst
tolerance. Below c15 there's enough CPU headroom that the refill storm is
invisible; the 30 ms gate is crossed at c16.

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
