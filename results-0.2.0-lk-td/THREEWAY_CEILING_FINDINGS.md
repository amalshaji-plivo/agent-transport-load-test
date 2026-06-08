# Three-architecture QoE ceiling comparison — EC2 (all numbers measured)

**Box:** c6in.2xlarge (8 vCPU / 15 GB). Bench client containerized (`bench-client:latest`;
AL2 glibc 2.26 can't install agent-transport host-side). `TURN_SILENCE_MS=4000`. Mocks
overlay wires real WS/HTTP STT/LLM/TTS for all servers. Python Silero VAD on both AT arms.

**Methodology (the ceiling):** one container per test — spin up a fresh agent
container, run the bench at a single concurrency, tear it down (`--force-recreate`
+ `down -v`), then the next concurrency gets a brand-new container. Each data point
is independent; no carryover. Every ceiling below is the mean of **5 such runs**.
The ceiling is bracketed by running adjacent concurrencies as separate tests and
finding where the joint gate flips.

**QoE gate:** delivery 100% **AND** CPU mean < 80% of cap **AND** audible-silence
p90 ≤ 5 ms. First-response captured but **not gateable** (synthetic `TURN_SILENCE`
+ EOU cadence dominates it; noisy at every load).

---

# THE CEILING (one container per test, 5-run average)

| arch | config | **ceiling** | delivery | CPU (of cap) | silence p90 | gate |
|---|---|---|---|---|---|---|
| **AT+pipecat** (py-VAD) | 1 vCPU / 2 GB | **c14** | 5/5 | 38% | **0.9 ± 0.0 ms** | ✅ |
| **AT+livekit** (EOU TD) | 1 vCPU / 2 GB | **c10** | 5/5 | 45% | **0.75 ms warm** (cold run 20.1) | ✅ |
| **Python pipecat** (W=4) | 4 vCPU / 8 GB | **fails gate** (best c30) | 5/5 | 15% of 4 cores | **6.2 ± 0.1 ms** (20 ms frames) | ❌ |

**Verdict: AT+pipecat (c14) and AT+livekit (c10) both hold the full QoE gate per
container; Python pipecat cannot hold the 5 ms audio gate at any concurrency.**

- **AT+pipecat — c14**, bound by audible silence. Cleanest audio (0.9 ms), lots of CPU
  headroom (38%). c16 rejected (bimodal silence, mean 5.3 ms > gate).
- **AT+livekit — c10**, bound by CPU + audio. Heavier (EOU ONNX). Needs a warmup
  request (cold first call spikes to 20 ms; warm 0.75 ms). c12 rejected (CPU 71% +
  per-session audio thins ~25%, silence unmeasurable).
- **Python pipecat — fails**, bound by a structural ~6.2 ms audio-pacing floor
  (`asyncio.sleep` output pacing on a GIL-bound loop). Delivers reliably (30/30) and
  is responsive (2.2 s), but never ≤5 ms silence.

## Per-run raw data

**AT+pipecat c14 (1 vCPU, py-VAD)** — delivery 14/14 every run
| run | CPU | silence p90 | within-gap p90 | 1st-resp |
|---|---|---|---|---|
| 1 | 38% | 0.9 ms | 20.9 ms | 2.40 s |
| 2 | 37% | 0.9 ms | 20.9 ms | 2.50 s |
| 3 | 37% | 1.0 ms | 21.0 ms | 2.54 s |
| 4 | 38% | 0.9 ms | 20.9 ms | 2.37 s |
| 5 | 40% | 1.0 ms | 21.0 ms | 2.18 s |

c16 (rejected knee): bimodal silence `[1.0, 9.8, 7.7, 1.0, 7.1]`, mean 5.3 ms > gate.

**AT+livekit c10 (1 vCPU, TD)** — delivery 10/10 every run
| run | CPU | silence p90 | note |
|---|---|---|---|
| 1 | 47% | 20.1 ms | cold start (EOU warmup) |
| 2 | 47% | 0.7 ms | warm |
| 3 | 43% | 0.7 ms | warm |
| 4 | 42% | 0.6 ms | warm |
| 5 | 44% | 1.0 ms | warm |

Warm mean 0.75 ms. c12 (rejected): CPU 70–74% + audio thins ~25% (96→72 frames/session),
silence metric collected zero gap samples — degradation, not a pass.

**Python pipecat c30 (4 vCPU, WORKERS=4)** — delivery 30/30 every run
| frames | CPU (of 400%) | within-gap p90 | silence p90 |
|---|---|---|---|
| 40 ms (default) | 58–60% | ~51 ms | 11.3–12.0 ms |
| 20 ms (`audio_out_10ms_chunks=2`) | 56–65% | 26.0–26.3 ms | 6.0–6.3 ms |

## Why Python fails the audio gate (config vs architecture)

Pipecat's default `audio_out_10ms_chunks=4` (40 ms frames) caused ~5.5 ms of the
11.7 ms floor; switching to 2 (20 ms frames, matching the AT cadence) cut silence to
6.2 ms. But **~6.2 ms remains and still fails** — that residual is the `asyncio.sleep`
output pacing on a GIL-bound event loop (`base_output.py`), which no frame-size change
removes. The AT transport paces with a Rust/tokio timer, holding p90 at the cadence
(+0.6 ms) regardless of load. Adding workers (W=4, SO_REUSEPORT) flattens silence
*degradation* under load (W=1 drifts 10.7→22.8 ms; W=4 holds ~11 ms) but never lowers
the floor — and uses only ~15% of the 4-core budget, so Python is pacing-bound, not
CPU-bound.

---

# APPENDIX — density exploration (4 containers/box) — CONFOUNDED, not the ceiling

This section is a **separate** question from the ceiling: *if you pack 4 agent
containers on one box, does capacity multiply?* These numbers are **confounded** and
should not be read as ceilings — on this single 8-vCPU box the 4 load-generating
bench clients run **on the same box** as the 4 agents (+ mocks), so the audio metrics
include co-location contention that would not exist with off-box clients. Reported for
completeness; a clean answer needs a multi-box rig (agents and load clients separated).

**Naive ×4 projection:** 4 independent 1-vCPU containers → AT+pipecat ~56, AT+livekit
~40. Delivery and CPU scale (cgroup caps isolate cleanly); **audio does not**.

**QoE-gated 4-up sweep** (4 agents + 4 clients simultaneously; joint gate applied):

AT+pipecat (host net):
| per-cont | total | max CPU/cont | silence p90 | gate |
|---|---|---|---|---|
| c14 | 56 | 43% | 11.9 ms | ❌ silence |
| **c12** | **48** | 44% | 1.2 ms | ✅ |
| c10 | 40 | 37% | 1.0 ms | ✅ |
| c8 | 32 | 31% | 0.6 ms | ✅ |

AT+livekit (bridge net — needed; `--network host` collides on a fixed internal port):
| per-cont | total | max CPU/cont | silence p90 | gate |
|---|---|---|---|---|
| c10 | 40 | 75% | 41.2 ms (1/4 measurable) | ❌ CPU+sil |
| c8 | 32 | 47% | 19.6 ms (3/4) | ❌ sil |
| c6 | 24 | 34% | 7.8 ms (4/4) | ❌ sil |
| c4 | 16 | 24% | n/a (0/4) + 14/16 | ❌ delivery |

**Density observations (confounded):**
- AT+pipecat degrades gracefully — full gate holds to **48/box** (4×c12); tips only at
  56 when the 8 cores saturate.
- AT+livekit degrades sharply — 4 heavy EOU workers + 4 clients thrash; silence fails
  even at 24 total. Isolated it is clean (c10, 0.75 ms), so the penalty is **agent-density
  co-location**, amplified here by on-box clients.
- Both confirm delivery + CPU scale ×4 (AT+pipecat 56/56 @ 43%; AT+livekit 40/40 @ 75%);
  only the audio metric breaks under co-location.

**Bottom line for density:** capacity (delivery+CPU) scales with added containers;
audio-quality-at-density is unresolved on one box and needs off-box load generation to
measure cleanly. Do not treat 48 / 40 as ceilings — the ceilings are the one-container
numbers above (c14 / c10 / fails).
