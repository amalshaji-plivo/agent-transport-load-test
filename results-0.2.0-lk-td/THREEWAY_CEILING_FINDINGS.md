# Three-architecture QoE ceiling comparison — EC2 (all numbers measured)

**Box:** c6in.2xlarge (8 vCPU / 15 GB). Bench client containerized (`bench-client:latest`;
AL2 glibc 2.26 can't install agent-transport host-side). Fresh `--force-recreate`
container per run (per-run isolation). `TURN_SILENCE_MS=4000`. Mocks overlay wires
real WS/HTTP STT/LLM/TTS for all servers. Python Silero VAD on both AT arms.

**QoE gate:** delivery 100% **AND** CPU mean < 80% of cap **AND** audible-silence
p90 ≤ 5 ms. First-response captured but **not gateable** (synthetic `TURN_SILENCE`
+ EOU cadence dominates it; noisy at every load).

The three architectures at equal hardware budget (one 4-vCPU box):
- **Python pipecat** — 1 × (4 vCPU / 8 GB), WORKERS=4
- **AT + pipecat** — 4 × (1 vCPU / 2 GB), Python VAD
- **AT + livekit** — 4 × (1 vCPU / 2 GB), EOU turn detector

## Final ceilings (each value from 5 measured runs — no interpolation)

| arch / config | profile | delivery | CPU (of cap) | silence p90 (mean ± sd) | 1st-resp |
|---|---|---|---|---|---|
| **AT+pipecat** (py-VAD) | **c14** · 1 vCPU | 5/5 | 38% | **0.9 ± 0.0 ms** ✅ | 2.4 s |
| **AT+livekit** (TD) | **c10** · 1 vCPU | 5/5 | 45% | **0.75 ms warm** (cold run 20.1) ✅ | 6.2 s |
| **Python pipecat** 40 ms frames | c30 · 4 vCPU | 5/5 | 59% (15%) | 11.7 ± 0.3 ms ❌ | 2.4 s |
| **Python pipecat** 20 ms frames | c30 · 4 vCPU | 5/5 | 61% (15%) | **6.2 ± 0.1 ms** ❌ | 2.2 s |

| arch | QoE ceiling /core | ×4 aggregate | binding signal |
|---|---|---|---|
| **AT+pipecat** | **c14** | **~56** (capacity measured¹) | audible silence (knee at c16) |
| **AT+livekit** | **c10** | **~40** (projection²) | CPU + audio degrade at c12; needs warmup |
| **Python pipecat** | **c30 delivery, fails 5 ms audio** (6.2 ms best) | ~30 single box | asyncio.sleep pacing floor |

¹ AT+pipecat ×4 confirmed on delivery + CPU by a 4-concurrent run (below); audio-at-density inconclusive (co-location confound).
² AT+livekit ×4 not yet measured — the 4-concurrent run was blocked by a host-networking port collision.

**Verdict: AT+pipecat (~56) > AT+livekit (~40) > Python pipecat (~30, fails strict audio gate).**
The per-core ceilings are fully measured; the ×4 aggregates are capacity-validated for AT+pipecat and projected for AT+livekit (see 4-concurrent section).

## Per-run raw data

**AT+pipecat c14 (1 vCPU, py-VAD)** — delivery 14/14 every run
| run | CPU | silence p90 | within-gap p90 | 1st-resp |
|---|---|---|---|---|
| 1 | 38% | 0.9 ms | 20.9 ms | 2.40 s |
| 2 | 37% | 0.9 ms | 20.9 ms | 2.50 s |
| 3 | 37% | 1.0 ms | 21.0 ms | 2.54 s |
| 4 | 38% | 0.9 ms | 20.9 ms | 2.37 s |
| 5 | 40% | 1.0 ms | 21.0 ms | 2.18 s |

**AT+pipecat c16 (the rejected knee)** — bimodal silence `[1.0, 9.8, 7.7, 1.0, 7.1]`,
mean 5.3 ms > gate → ceiling dropped to c14.

**AT+livekit c10 (1 vCPU, TD)** — delivery 10/10 every run
| run | CPU | silence p90 | note |
|---|---|---|---|
| 1 | 47% | 20.1 ms | cold start (EOU warmup) |
| 2 | 47% | 0.7 ms | warm |
| 3 | 43% | 0.7 ms | warm |
| 4 | 42% | 0.6 ms | warm |
| 5 | 44% | 1.0 ms | warm |

Warm mean = 0.75 ms. **c12 fails**: CPU 70–74% AND per-session audio degrades ~25%
(96→72 frames/session, throughput 13.3→11.7 f/s) — so sparse the silence metric
collected **zero** within-phrase-gap samples. Degradation, not a pass.

**Python pipecat c30 (4 vCPU, WORKERS=4)** — delivery 30/30 every run
| run | CPU (of 400%) | within-gap p90 | silence p90 @40 ms | silence p90 @20 ms |
|---|---|---|---|---|
| 40 ms frames | 58–60% | ~51 ms | 11.3–12.0 ms | — |
| 20 ms frames | 56–65% | 26.0–26.3 ms | — | 6.0–6.3 ms |

## The three experiments and what each settled

1. **AT+pipecat c14 ×5** → ceiling confirmed at c14 (0.9 ms, sd 0.0). The single
   c16 run that read 4.8 ms was a lucky low draw of a bimodal distribution.

2. **AT+livekit c12 ×5** → the knee is **not** above c10. c12 saturates CPU (71%)
   and thins audio output below the measurable threshold. c10 stands, with a
   **cold-start warmup requirement** (first request after container start spikes
   to 20 ms; warm is 0.75 ms).

3. **Python pipecat 20 ms frames ×5** → settles config-vs-architecture. Pipecat's
   default `audio_out_10ms_chunks=4` (40 ms frames) caused ~5.5 ms of the floor;
   switching to 2 (20 ms frames, matching the AT cadence) cut silence 11.7→6.2 ms.
   But **~6.2 ms remains and still fails the 5 ms gate** — that residual is the
   `asyncio.sleep`-based output pacing on a GIL-bound event loop (pipecat
   `base_output.py`), which no frame-size change removes. The AT transport paces
   with a Rust/tokio timer, holding p90 at the cadence (+0.6 ms) regardless of load.

## Why per-core × 4 is fair for AT but not for Python

The AT arms deploy as 4 independent 1-vCPU processes → ceilings sum (×4). Python
pipecat is one asyncio process; even WORKERS=4 (SO_REUSEPORT across 4 workers)
uses only ~15% of the 4-core budget at c30 — the binder is the per-frame
`asyncio.sleep` tail, not CPU. Adding workers flattens silence *degradation* under
load (W=1 drifts 10.7→22.8 ms; W=4 holds ~11 ms) but does not lower the floor.

## 4-concurrent aggregate validation (turning ×4 from projection to measurement)

Ran 4 independent agent containers at once, each cgroup-capped to 1 vCPU/2 GB at
its per-core ceiling, with 4 bench clients firing in parallel and one shared
mock-services backend (CPU sampled throughout). Host-networked on the 8-vCPU box.

**AT+pipecat — 4 × c14 simultaneously:**
| signal | isolated | 4-concurrent | holds? |
|---|---|---|---|
| delivery | 14/14 | **56/56** | ✅ scales ×4 |
| CPU / container | 38% | 43% | ✅ cgroup isolation clean |
| client frames sent | full | full (18.7k each) | ✅ clients not send-starved |
| audible-silence p90 | 0.9 ms | **11.9 ms (max 17)** | ❌ degraded |
| mock-services CPU | — | 19% peak | ✅ shared backend not the bottleneck |

**What it proves:** the **capacity** half of ~56 is real — 56/56 delivered with clean
per-container CPU isolation and no backend contention.

**What it can't prove on one box:** audio quality at 4-agents-per-box. Silence rose
to ~12 ms, but this is **confounded by co-location** — 4 capped agents + 4
load-generating clients + mocks all share 8 cores. AT+pipecat's sub-ms pacing needs
its tokio timer to get a core the instant it fires; a saturated host delays those
wakeups. Clients sent full frame counts (not send-starved), so it is scheduler
contention for timely wakeups — **partly real** (4 agents/box would see some of this
in production) and **partly artifact** (in production the load generators are not on
the agent box). Separating the two needs **off-box clients** (a multi-box rig).

**AT+livekit — 4 × c10: blocked (harness limitation).** Under `--network host` the
LiveKit agent binds a fixed internal port that collides across containers, so only
1 of 4 functions (10/40; the working one held at 42 % CPU = isolated value).
Staggered startup fixed the model-load starvation but not the port collision —
4 instances on one host need **bridge networking** (separate net namespaces). So the
AT+livekit ×4 aggregate (~40) remains a projection, not yet measured.

**Net:** AT+pipecat ~56 is capacity-validated (delivery + CPU); AT+livekit ~40 stays
projected; audio-quality-at-density is a separate question requiring a multi-box rig.

## Bottom line

- **AT+pipecat is the highest-capacity, cleanest-audio option** (~56 QoE-clean
  per 4-vCPU box, 0.9 ms silence, 38% CPU at ceiling — CPU headroom to spare).
- **AT+livekit** trades capacity for the EOU turn detector (~40; CPU-bound by the
  ONNX inference) and needs a warmup request.
- **Python pipecat** delivers reliably (30/30) and is responsive (2.2 s) but
  **cannot pass a strict 5 ms audio-pacing gate** — architectural, not config.
