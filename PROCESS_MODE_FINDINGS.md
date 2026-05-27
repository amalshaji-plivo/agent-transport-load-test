# Process-mode capacity & profiling — 4 vCPU / 8 GB, VAD + multilingual TD on

`JOB_EXECUTOR_TYPE=process` (default). Numbers below are from CLEAN runs:
py-spy OFF (PROFILE=0), worker-registration gate ON, HOST_IP-proof teardown,
idle pool ≈ concurrency, log level warn.

> ⚠️ This supersedes the earlier version of this doc, which concluded process
> mode was CPU-bound, choked at c=35, and failed the latency gates at every
> concurrency. **All three were py-spy measurement artifacts** (see "What was
> wrong" below). The clean data tells a different story.

## Headline: memory-bound, NOT CPU-bound — and the latency gates PASS

| concurrency | sessions | CPU mean / peak | mem peak | wphase p99 | silence p99 |
|---|---|---|---|---|---|
| c20 | 20/20 ✓ | 79 % / 258 % | 6.4 GB | 23 ms ✓ | 3.1 ms ✓ |
| c25 | 25/25 ✓ | 120 % / 337 % | 7.2 GB | 26 ms ✓ | 5.8 ms |
| c30 | 30/30 ✓ | 108 % / 384 % | 8.0 GB | 26 ms ✓ | 6.2 ms |
| c33 | 30/33 ❌ | 81 % / 255 % | 8.1 GB | 24 ms ✓ | 3.7 ms ✓ |
| c35 | 35/35 ✓ | 106 % / 285 % | 8.1 GB | 24 ms ✓ | 4.1 ms ✓ |

- **CPU is NOT the constraint.** Mean stays 79–120 % of the 400 % budget through
  c=35; it never sustains near the cap.
- **Latency gates PASS** (wphase p99 ≤30: yes, ~23–26 ms; silence p90 ≤5: yes,
  p99 only ~3–6 ms).
- **Memory is the binding constraint.** Peak hits the 8 GB cap at c30+ — the idle
  worker pool (each fork holds Silero VAD + Qwen2 EOU models) is what fills it.
- The c33 drop (30/33 at only 81 % CPU but 8.1 GB) is **memory-pressure jitter at
  the cap**, not CPU saturation — c35 (also 8.1 GB) passed 35/35. Above idle≈30
  whether sessions survive depends on how close to the 8 GB cap you land.

**Capacity verdict:** ~c=30 with a comfortable idle pool today, limited by the
8 GB memory cap. To go higher, REDUCE per-worker memory (smaller/shared models,
right-size the idle pool) — it is NOT a CPU problem.

## Profile (PROFILE=1, single py-spy @ rate=20, c30 — clean config)

Where the on-CPU work actually goes (leaf + any-frame views):

| component | share | notes |
|---|---|---|
| **onnx inference (VAD + EOU)** | **34 %** (leaf `onnxruntime…:322`) | THE dominant real CPU consumer — Silero VAD + Qwen2 turn-detector |
| audio-recv threadpool (`_worker`) | 18 % leaf / 55 % any-stack | mostly threads PARKED in `queue.get()` — py-spy attributes blocked threads; not 55 % of a core burning |
| asyncio loop wakeups (`_write_to_self`/selector) | ~13 % | FFI event callbacks scheduling into the loop |
| FFI request/dispose | ~9 % | audio-frame marshaling across the process boundary |
| log IPC | ~1 % | minimal at WARN |

The genuinely CPU-burning work is **onnx ML inference (~34 %)** plus asyncio/FFI
plumbing. The big `_worker` bucket is mostly parked recv threads, not compute.

## Rust gateway vs Python (per-process /proc sampling, c30 no py-spy)

```
python (agent + forked workers + onnx inference):  ~206-238 %   (~96% of CPU)
livekit-gateway (Rust transport):                  ~9-11 %      (~1 core/10)
```

The Rust gateway transport is CHEAP and has large headroom. "Load-testing the
gateway" is really load-testing the Python livekit-agents ML pipeline.

## What was wrong (py-spy measurement artifacts)

The harness ran TWO concurrent py-spy instances (flamegraph + speedscope) at
`--rate 100 --subprocesses`. Measured overhead: ~180 % CPU under load (one
instance ~90 %; against the idle agent, one instance was already 69 %). On the
400 %-capped cgroup this:
- **inflated** the `docker stats` CPU reading (py-spy lives in the cgroup), and
- **starved** the workload — stealing ~180 % of the 400 % budget, which throttled
  the audio pipeline and inflated latency, and dropped sessions.

That fabricated the earlier conclusions: "CPU 286–307 %" (really ~110 %),
"wphase 63 ms / silence 43 ms gate-fail" (really ~24 ms / ~4 ms, passing), and
"CPU-saturation choke at c=35" (really memory at the 8 GB cap).

**Fix (committed):** single py-spy instance, speedscope only, `PROFILE_RATE=20`,
and a `PROFILE` switch — `PROFILE=0` (default) runs NO py-spy for clean capacity
numbers; `PROFILE=1` runs one low-rate instance for flamegraph shape only.
Validated: profiled c30 = 117 % CPU vs clean 108 % → overhead now ~9 %
(was ~180 %), and it still hit 30/30.

## Other fixes landed this investigation
- HOST_IP-proof teardown (compose interpolates livekit-server's required
  HOST_IP even on `down`; unset → leaked containers).
- Worker-registration gate: gateway rejects sessions arriving before the agent
  registers (`NoWorkers`, no retry); a fixed sleep raced it → 0/N..N/N. Now
  gates bench start on the gateway's `worker registered` log.
- Lazy LiveKitRtcClient import: gateway-only runs need neither `livekit` nor
  `livekit-api`.

## EC2 caveats (see RUN_ON_EC2_THREAD.md)
- Measure CPU with PROFILE=0 (py-spy off).
- Expect LOWER absolute concurrency than these Mac numbers: EC2 vCPUs are
  hyperthreads vs the Mac's full Apple-Silicon cores; the gateway+agent share
  the cgroup. But since the real limit here is MEMORY, the 8 GB cap behaves
  similarly across hardware — the idle-pool memory ceiling should transfer more
  faithfully than CPU-based numbers would.

## Artifacts
- `~/bench-out/PROC-FINAL/c{20,25,30,33,35}-p0/` — clean capacity (no py-spy)
- `~/bench-out/PROC-FINAL/c30-p1/` — profiled run (rate=20) + speedscope
- open `.speedscope.json` at https://speedscope.app
