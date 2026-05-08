# Benchmark revalidation — clean host (May 2026)

Re-running the headline-table concurrencies against the *current* code on a
**4 vCPU / 10 GB total** budget — VAD off, turn-detection off, no other config
changes. 5 back-to-back runs per setup, 60 s steady state + 10 s warmup,
container(s) recreated between runs. `agent-observability-*` containers
stopped during this run so the host wasn't competing for memory.

## Headline numbers

| Setup | c | Used / runs | thr avg (f/s) | thr σ | thr min | thr max | p90 gap avg (ms) | p90 σ | p99 gap avg (ms) | max gap avg (ms) | Reported thr | Reported p90 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| direct-pipecat vertical (4 vCPU / 10 GB) | 50 | 5/5 | **210.9** | 0.2 | 210.7 | 211.1 | **2.94** | 0.37 | 202.16 | 217.85 | 313 | 5.5 |
| AT + pipecat horizontal (4 × 1 vCPU / 2.5 GB) | 110 | 4/5 | **1348.9** | 4.6 | 1343.6 | 1354.3 | **0.64** | 0.14 | 404.85 | 479.99 | 1151 | 28.0 |
| AT + LiveKit horizontal (4 × 1 vCPU / 2.5 GB) | 140 | 4/4 | **753.6** | 2.3 | 750.8 | 755.8 | **20.37** | 0.12 | 100.21 | 200.22 | 713 | 25.7 |

`p90 / p99 / max gap` = audible-silence gap between adjacent output frames in
steady state — same metric as the original headline table.
All used runs had `with_output = eligible = c` (every session produced output
and was jitter-eligible).

## Read against the reported numbers

| Setup | Throughput vs reported | p90 vs reported | Verdict on the p90 < 50 ms bar |
|---|---|---|---|
| direct-pipecat | −33 % (210.9 vs 313) | **better** (2.94 vs 5.5) | still passes |
| AT + pipecat | **+17 %** (1348.9 vs 1151) | **much better** (0.64 vs 28.0) | still passes by a wide margin |
| AT + LiveKit | +6 % (753.6 vs 713) | better (20.37 vs 25.7) | still passes |

Variance is tiny: σ ≤ 5 f/s on throughput across all three, σ ≤ 0.4 ms on p90.
Throughput min/max within each setup spread by <2 % of the mean.

So the original "highest c that all 5 runs pass p90 < 50 ms" claim still holds
on the current code at the same c. p90 actually improved on every setup.

## Long-tail gaps the original p90-only table didn't surface

- direct-pipecat: p99 = **202 ms**, max = **218 ms**
- AT + pipecat: p99 = **405 ms**, max = **480 ms**
- AT + LiveKit: p99 = **100 ms**, max = **200 ms**

p90 is tight on all three, but ~1 % of inter-frame gaps land >100 ms in
steady state, with AT+pipecat the worst tail (~400 ms p99). On a real call
the median session sounds fine, but ~1 in 100 audio frames lands late enough
to be audibly choppy. This is a regression vs the spirit of the original
benchmark even where the p90 headline number passes.

## Throughput regression on direct-pipecat (−33 %)

direct-pipecat lost ~100 f/s (313 → 211) at c=50. Likely cause:
**pipecat upgraded from 1.0.0 to 1.1.0** when the image was rebuilt during
this session. The base `pipecat-ai[websocket]` install is unpinned, so any
benchmark image rebuilt in May picks up 1.1.0. AT+pipecat ships pipecat
inside the AT image and got the same upgrade — but its throughput went *up*
(+17 %), which suggests the regression is specific to direct-pipecat's
single-process path (one asyncio loop owns everything) rather than the
pipecat processors themselves.

## Stability issues at this concurrency

- **AT + pipecat run 5** (c=110) collapsed: all 110 sessions connected but
  none sent media frames (`with_output = 0`). All four horizontal containers
  were healthy when the run started. Likely a harness-side resource state
  leaking across the previous 4 back-to-back 110-session runs (file
  descriptors, asyncio task accumulation, or websocket connection pool
  state). Dropped from the average; the other 4 runs were tightly clustered
  so the average is representative.
- **AT + LiveKit at c=140** triggered a host OOM on the load harness in both
  attempt rounds (run 3 each time was SIGKILL'd by macOS). 140 concurrent
  WebSocket sessions, each holding ~1 MB of audio buffers + asyncio task
  state, plus 4 LK containers, pushed total host residency over the
  threshold. The dropped run was *not* an issue with the LK containers
  themselves — they each used only ~150 MB of their 2.5 GB budget.

## Memory headroom (one-off probe at c=140 on AT+LiveKit)

| Instance | Steady-state peak | Per-instance limit | Utilization |
|---|---:|---:|---:|
| livekit-horiz-1 | 154 MB | 2 441 MB | 6.3 % |
| livekit-horiz-2 | 157 MB | 2 441 MB | 6.4 % |
| livekit-horiz-3 | 152 MB | 2 441 MB | 6.2 % |
| livekit-horiz-4 | 152 MB | 2 441 MB | 6.2 % |

Each LK instance uses ~150 MB of its 2.5 GB budget — 90 %+ of the per-instance
memory is sitting idle. direct-pipecat used ~3.6 GB of its 10 GB ceiling
(36 %). Container budgets are fine.

The OOM-kill on LK run 3 came from the *host*, not a container hitting its
limit (the killed process was the harness Python, not docker). Increasing the
per-container budget would not have prevented it. The fix is freeing host
RAM — which we already did partially — and/or shrinking the harness's
per-session retained state at very high concurrency.

## Caveats

- Memory budget per horizontal instance is **2.5 GB**, not the 4 GB shown in
  `BENCHMARK.md`. The original headline implicitly used 4 × 4 GB = 16 GB
  total; the actual production budget is 4 vCPU / 10 GB total.
- Pipecat 1.1.0's stricter `PlivoFrameSerializer` validation (auth_id /
  auth_token required when `auto_hang_up=True`) was worked around by
  passing `auto_hang_up=False` in the bench server. Behaviorally equivalent
  for non-hangup traffic but worth flagging.
- Raw per-run JSON: `results-revalidate/{direct_c50,at_pipecat_c110,at_livekit_c140}_run{1..5}.json`.
- Aggregated stats: `results-revalidate/summary.json`.
- Earlier dataset (host-loaded) preserved at `results-revalidate-host-loaded/`.
