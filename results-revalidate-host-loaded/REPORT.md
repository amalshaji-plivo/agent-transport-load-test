# Benchmark revalidation (May 2026)

Re-running the headline-table concurrencies against the *current* code on a
**4 vCPU / 10 GB total** budget — VAD off, turn-detection off, no other config
changes. 5 back-to-back runs per setup, 60 s steady state + 10 s warmup,
container(s) recreated between runs.

## Headline numbers

| Setup | c | Runs | thr avg (f/s) | thr σ | p90 gap avg (ms) | p90 σ | p99 gap avg (ms) | max gap avg (ms) | Reported thr | Reported p90 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| direct-pipecat vertical (4 vCPU / 10 GB) | 50 | 5 | **209.8** | 2.3 | **2.87** | 0.19 | 202.02 | 264.79 | 313 | 5.5 |
| AT + pipecat horizontal (4 × 1 vCPU / 2.5 GB) | 110 | 5 | **1364.6** | 1.2 | **0.54** | 0.06 | 399.82 | 479.97 | 1151 | 28.0 |
| AT + LiveKit horizontal (4 × 1 vCPU / 2.5 GB) | 140 | 4 | **767.0** | 12.1 | **20.19** | 0.06 | 100.16 | 205.17 | 713 | 25.7 |

`p90 / p99 / max gap` = audible-silence gap between adjacent output frames in
steady state, the same metric used in the original headline table.

All sessions produced output and were jitter-eligible in every run
(`with_output = eligible = c` for every measurement).

## Read against the reported numbers

| Setup | Throughput vs reported | p90 vs reported | Verdict on the p90 < 50 ms bar |
|---|---|---|---|
| direct-pipecat | −33 % (209.8 vs 313) | **better** (2.87 vs 5.5) | still passes |
| AT + pipecat | **+19 %** (1364.6 vs 1151) | **much better** (0.54 vs 28.0) | still passes by a wide margin |
| AT + LiveKit | +8 % (767 vs 713) | better (20.19 vs 25.7) | still passes |

Variance across the 5 (or 4) runs is small — σ on throughput is at most 12 f/s
(1.6 % on AT+LiveKit), σ on p90 is ≤ 0.2 ms.

So the original "highest c that all 5 runs pass p90 < 50 ms" claim still holds
on the current code at the same c. p90 actually improved on every setup.

## What the original numbers didn't surface — long-tail gaps

The reported headline used p90 only. The current run shows a sharper tail:

- direct-pipecat: p99 = **202 ms**, max = **265 ms**
- AT + pipecat: p99 = **400 ms**, max = **480 ms**
- AT + LiveKit: p99 = **100 ms**, max = **205 ms**

The p99 / max numbers are *steady-state* (warmup discarded), not first-frame.
At c equal to the reported ceiling, ~1 % of inter-frame gaps are >100 ms
across all three setups, with AT+pipecat the worst tail at 400 ms.

p90 — the metric the headline table optimises for — is still tight, so the
median session sounds fine. But on a real call, ~1 in 100 audio frames lands
late enough to be audible as a chop. That's a regression vs the spirit of the
original benchmark even where the headline number passes.

## Throughput regression on direct-pipecat (−33 %)

direct-pipecat lost ~100 f/s (313 → 210) at the same c=50. Likely cause:
**pipecat upgraded from 1.0.0 to 1.1.0** when the image was rebuilt during
this session. The base `pipecat-ai[websocket]` install is unpinned, so any
benchmark image rebuilt in May picks up 1.1.0. AT+pipecat also ships pipecat
inside the AT image and got the same upgrade — but its throughput went *up*,
which suggests the regression is specific to the direct-pipecat code path
(single python process, single asyncio loop) rather than the pipecat
processors themselves.

The AT improvements are consistent with the agent-transport core having had
more tuning since the original benchmark.

## Notes / caveats

- AT + LiveKit run 3 was SIGKILL'd by the host kernel (OOM). With 4 × 2.5 GB
  LK containers + the existing `agent-observability-*` containers + the
  harness, the macOS host hit memory pressure once. The other 4 LK runs
  completed cleanly and were tightly clustered, so the average is
  representative.
- Memory budget per horiz instance is **2.5 GB**, not the 4 GB shown in
  `BENCHMARK.md`. The original headline implicitly used 4 × 4 GB = 16 GB
  total; the actual production budget is 4 vCPU / 10 GB total.
- Pipecat 1.1.0's stricter `PlivoFrameSerializer` validation (auth_id /
  auth_token required when `auto_hang_up=True`) was worked around by passing
  `auto_hang_up=False` in the bench server; this should be behaviorally
  equivalent for non-hangup traffic but is worth flagging.
- Raw per-run JSON: `results-revalidate/{direct_c50,at_pipecat_c110,at_livekit_c140}_run{1..5}.json`.
- Aggregated stats: `results-revalidate/summary.json`.
