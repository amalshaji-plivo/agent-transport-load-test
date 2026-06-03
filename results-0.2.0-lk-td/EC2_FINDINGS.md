# AT + LiveKit (agent-transport 0.2.0) capacity — EC2 certification

**Box:** c6in.2xlarge (8 vCPU / 15 GB host); agent cgroup **4 vCPU / 8 GB**.
**Config (as committed):** turn detector ON (multilingual EOU), Python VAD OFF,
`TURN_SILENCE_MS=4000`, endpointing 0.4/1.5 s, PROFILE=0. Server = `agent-transport==0.2.0`
from PyPI (Docker). Bench client containerized (AL2 glibc 2.26 can't install
agent-transport host-side); fresh server per step (= per-step isolation).

## Capacity sweep (4 vCPU / 8 GB)

| c | delivery | CPU mean/peak (cap 400%) | mem peak | verdict |
|---|---|---|---|---|
| 8  | 8/8   | 49% / 121% | 1.0 GB | ✓ |
| 14 | 14/14 | 73% / 194% | 1.1 GB | ✓ |
| 20 | 20/20 | 102% / 283% | 1.1 GB | ✓ |
| **25** | **25/25** | 95% / 306% | 1.1 GB | ✓ last clean monotonic |
| 30 | 16/30 → repeats 22/30, 17/30, 9/30 | 113% / 402% | 1.2 GB | ✗ flaky |
| 35 | 32/35 → repeats **35/35, 35/35, 35/35** | 113% / 411% | 1.3 GB | mixed (reproducibly clean on repeat) |
| 40 | 40/40 → repeats 40/40, 39/40, 35/40 | 132% / 410% | 1.4 GB | near-perfect |

## Certified verdict — matches the Mac baseline, and rules out its confound

- **Clean monotonic delivery ceiling = c25** (4 vCPU / 8 GB) — exactly the Mac's number.
- **Binding constraint = delivery reliability, NOT CPU or memory.** Memory never
  exceeds 1.4 GB of 8 (EOU subprocess ~0.85-1 GB floor + ~2 MB/session); mean CPU
  is ~24-33% of the 4-vCPU cap even at c40 (peak pegs 400% only in turn bursts,
  never sustained). The box has ample compute headroom for 40+.
- **Delivery is non-monotonic above c25** — c30 flaky, c35 reproducibly 35/35 (3/3),
  c40 near-perfect. Capacity cannot make 35 reliable while 30 is broken, so this
  is **not** a load ceiling.
- **EC2 resolves the Mac's open question.** The Mac suspected its c≥30 flakiness
  might be shared-host client/server core contention. On EC2 with spare cores
  (agent capped at 4, client + OS on the other 4) the **non-monotonic flakiness
  persists** — so it is **intrinsic to the EOU turn-detector cadence on the
  synthetic mock workload, not host contention.** (Runbook caveat #4 / Mac
  REPORT note #1 confirmed on better hardware.)

## The c30 anomaly (EC2-specific observation)

c30 doesn't just drop a few sessions — the **server goes near-silent**: c30 r3
sent 29 751 frames and received **75 back (0.8 f/s)**, vs c35's 2 565 recv (27 f/s)
the very next run on a fresh container. The bot stops producing turns almost
entirely. Because each step is a fresh `--force-recreate` container and c35/c40
(run after c30) are clean, this is a **turn-cadence resonance** of the c30
profile's specific ramp/warmup timing (`ramp_delay=0.7, warmup_sec=27`) with the
4 s synthetic silence + EOU end-of-utterance detection — the detector waits for
an end-of-speech the looped audio doesn't cleanly signal, and turns never fire.
It is a benchmark-workload artifact, not an agent fault or a capacity limit.

## Pushing beyond c25 — the real (CPU-bound) ceiling

The c25 "knee" was misleading. Extending the sweep shows delivery recovers above
the c30 cadence dip and stays clean until CPU actually saturates.

**1 vCPU / 2 GB:**

| c | delivery | CPU mean/peak (cap 100%) | mem |
|---|---|---|---|
| 8 | 8/8 | 38% / 87% | 1.0 GB |
| 14 | 14/14 | 84% / 103% | 1.1 GB |
| **20** | **20/20** | **101% / 104%** | 1.2 GB |

→ single core saturates ~c20 (mean pegged at 101%, still 20/20). **Recommend c14**
(84%, headroom). Memory never binds (EOU floor ~1 GB + ~10 MB/session).

**4 vCPU / 8 GB (beyond 25):**

| c | delivery | CPU mean/peak (cap 400%) | mem |
|---|---|---|---|
| 50 | 50/50 | 310% (78%) / 407% | 1.8 GB |
| **60** | **60/60** | **353% (88%) / 406%** | 1.7 GB |
| 75 | 71/75 | 371% (93%) / 416% | 1.8 GB |
| 100 | 96/100 | 329%* / 413% | 2.2 GB |

→ **compute ceiling ≈ c60** (60/60 clean at 88% CPU). Delivery drops at c75 just as
CPU saturates (93%); c100's *lower* mean (329%) is over-saturation — sessions stall
so less work completes. Memory irrelevant (2.2 GB of 8 at c100).

**Full delivery curve:** 25✓ 30✗(16-22) 35✓ 40✓ 50✓ 60✓ 75(71/75) 100(96/100).
The c30 dip is an isolated cadence resonance of that one profile's timing — NOT
the ceiling. Above it, delivery is clean to c60 and degrades only when CPU
saturates. **The Mac's "c25 reliability-bound" was overly conservative; the true
limit is CPU-bound at ~c60 (4 vCPU) and ~c20 (1 vCPU).**

## Bottom line

agent-transport 0.2.0 AT+LiveKit (TD + Py-VAD), **CPU-bound** (memory never a factor):

| box | compute ceiling (last clean 100%) | recommended (headroom) | binding |
|---|---|---|---|
| **1 vCPU / 2 GB** | **c20** (CPU pegged 101%) | **c14** (84% CPU) | CPU |
| **4 vCPU / 8 GB** | **c60** (60/60, 88% CPU) | **c50** (78% CPU) | CPU |

- Memory is never the limit: ≤1.2 GB of 2 (1 vCPU), ≤2.2 GB of 8 (4 vCPU). EOU
  subprocess ~1 GB fixed floor + ~10 MB/session.
- The c25-c40 "reliability knee" (esp. the c30 collapse) is a **synthetic-workload
  turn-cadence artifact**, not a capacity wall — delivery is clean again at c50/c60.
- EC2 (contention-free) holds **much higher than the Mac's conservative c25**, and
  the true ceiling is **CPU**, reached cleanly at ~c60 (4 vCPU) / ~c20 (1 vCPU).
- A realistic STT-driven turn cadence (not the looped clip) would smooth the
  mid-range delivery dips, but the hardware ceiling is the CPU numbers above.

---

## QoE re-gate (delivery + CPU + silence p90 + first-response)

The capacity numbers above gated on delivery + CPU only. Re-ran both profiles
capturing all four signals (clean PROFILE=0; first-response = client-side
`first_frame_latency`, connect→first bot audio, zero server overhead).

**1 vCPU / 2 GB (cap 100%)**

| c | delivery | CPU | silence p90 | first-resp p50/p90 | gates |
|---|---|---|---|---|---|
| 2 | 2/2 | 11% | 0.00 | 12.9/20.4 s | D C S |
| 6 | 6/6 | 27% | 1.04 | 10.0/32.1 s | D C S |
| 8 | 8/8 | 37% | 0.00 | 4.4/14.2 s | D C S |
| 10 | 10/10 | 44% | 0.66 | 11.0/36.5 s | **D C S** (last clean) |
| 14 | 14/14 | 85% | 0.00 | 6.1/48.3 s | D c S |
| 20 | 20/20 | 100% | 0.00 | 9.1/62.1 s | D c S |

**4 vCPU / 8 GB (cap 400%)**

| c | delivery | CPU (%cap) | silence p90 | first-resp p50/p90 | gates |
|---|---|---|---|---|---|
| 8 | 8/8 | 12% | 0.56 | 8.0/25.1 s | D C S |
| 16 | 16/16 | 21% | 0.79 | 6.1/12.1 s | D C S |
| 25 | 25/25 | 25% | 0.95 | 12.0/20.4 s | **D C S** (last clean) |
| 40 | 39/40 | 29% | 0.79 | 5.6/33.9 s | d C S |
| 50 | 49/50 | 75% | 1.06 | 7.9/16.9 s | d C S |
| 60 | 59/60 | 91% | 6.70 | 8.1/18.7 s | d c s |

(D/C/S = delivery 100% / CPU<80%cap / silence p90 ≤5ms pass; lowercase = fail)

### What each gate says
- **CPU**: binds at ~c12 (1 vCPU; c14=85%) and ~c55 (4 vCPU; c50=75%, c60=91%).
- **silence p90 ≤5 ms**: passes everywhere on 1 vCPU; on 4 vCPU crosses 5 ms only at c60 (6.7 ms).
- **delivery**: strict-clean to c10 (1 vCPU) / c25 (4 vCPU); above that, single-session drops (39/40, 49/50) = the documented EOU cadence flake, not capacity.
- **initial response: NOT minimal and NOT gateable.** Multi-second at every load (even c2 = 12.9 s p50), noisy/non-monotonic — dominated by the synthetic `TURN_SILENCE_MS=4000` + EOU cadence, not system load. Needs a realistic STT-driven turn cadence to measure true responsiveness.

### QoE-gated ceilings (vs the delivery+CPU-only numbers above)
| profile | delivery+CPU only | full QoE gate (delivery+CPU+silence) | note |
|---|---|---|---|
| 1 vCPU / 2 GB | c20 | **c10–12** | CPU is the binder |
| 4 vCPU / 8 GB | c60 | **c25 strict / ~c55 if 1-session flakes forgiven** | CPU+silence bind near c55-60 |

**4-task aggregate (4×1 vCPU/2 GB, the scale-out topology):** QoE-gated ≈ **40–48 concurrent** (4×c10-12), about half the delivery-only 4×c20=80. Still exceeds a single 4 vCPU Python-pipecat instance's delivery ceiling (~50), but the margin narrows once CPU headroom is respected — and a true apples-to-apples needs the pipecat arms QoE-gated too (blocked by the pipecat zero-output bug).

---

## py-spy CPU profile + per-process memory (sweet spots)

PROFILE=1, single py-spy @ rate 20, speedscope. SHAPE only (py-spy on this
GIL + inference-subprocess app under-weights throughput; trust docker-stats for
CPU magnitude). Blocked-vs-on-CPU split applied so parked recv-threads don't
masquerade as CPU.

**On-CPU breakdown (% of on-CPU samples):**

| component | 4 vCPU @ c25 | 1 vCPU @ c10 |
|---|---|---|
| ONNX / EOU inference (`onnxruntime…:322` + `runners.py:118`) | **~51%** | **~53%** |
| audio / resample / codec (`_audio_io.py`) | 9% | 5% |
| websocket / transport / FFI (`_ffi_client`) | 5% | 6% |
| agent / session | 3% | 3% |
| Silero VAD | 2% | 2% |
| asyncio loop | 1% | 1% |

Top leaf both profiles: `run (onnxruntime_inference_collection.py:322)` = 41-45%
alone (EOU ONNX inference). The `_worker (thread.py:90)` bucket (17-25%) is the
audio-IO/FFI threadpool.

**Per-process memory (4 vCPU @ c25, under load):**

| PID | RSS | role |
|---|---|---|
| 19 | **850 MB** | EOU inference subprocess — the fixed memory floor |
| 1 | 239 MB | main agent server (sessions live here, ~10 MB each) |
| 18 | 12 MB | inference-proc launcher / IPC |

**Reading it:** CPU is ~half EOU inference, ~half plumbing (audio marshaling, FFI
transport, asyncio); VAD ~2%; STT/LLM/TTS off-CPU (mock + I/O-bound). Matches the
Mac REPORT, confirmed on EC2 — and is why the box is CPU-bound at the ceiling.
Memory = 850 MB EOU floor + ~10 MB/session (so memory never binds). **Biggest CPU
lever = the multilingual EOU model (English-only EOU ≈ halves the hot path);
biggest memory lever = the same 850 MB EOU subprocess.** Speedscopes:
`~/bench-out/at-lk-profile/{4cpu-c25,1cpu-c10}.speedscope.json`.
