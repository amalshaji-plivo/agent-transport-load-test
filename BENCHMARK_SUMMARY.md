# Benchmark

Concurrency ceiling comparison across three voice-agent server implementations,
measured at production-quality audio (audible silence gap p90 < 50 ms) with the
same mocked STT/LLM/TTS pipeline timings.

## Results

| Impl                              | Highest c (all 5 runs p90 < 50 ms) | avg p90 | max p90 | avg throughput | p90 σ   | thr σ   |
| :-------------------------------- | :--------------------------------: | :-----: | :-----: | :------------: | :-----: | :-----: |
| direct-pipecat vertical (4 CPU)   | c=50                               | 5.5 ms  | 6.1 ms  | 313 f/s        | 0.6 ms  | 0.2 f/s |
| AT + pipecat horizontal (4×1 CPU) | c=110                              | 28.0 ms | 40.1 ms | 1151 f/s       | 10.9 ms | 90 f/s  |
| AT + LiveKit horizontal (4×1 CPU) | c=140                              | 25.7 ms | 39.7 ms | 713 f/s        | 8.1 ms  | 43 f/s  |

`c` is the number of concurrent voice-agent sessions. `p90` is the audible
silence gap between adjacent output frames measured in steady state — the
metric that drives perceived call quality on a phone. Throughput is aggregate
output frames per second across all sessions.

## Testing setup

**Infrastructure.** macOS host, OrbStack as the Docker runtime, Python 3.13 in
each container, uvloop as the asyncio event loop. The benchmark client runs
in-process on the host and speaks the Plivo WebSocket protocol (μ-law, 8 kHz,
20 ms frames) directly to the server containers.

**Compute budget (4 CPU, 10 GB total, per topology):**

- **Vertical (direct-pipecat):** one container, 4 CPUs, 10 GB RAM.
- **Horizontal (AT + pipecat, AT + LiveKit):** four containers, 1 CPU + 4 GB
  RAM each. Sessions are routed round-robin across the four instances by the
  load harness — no external load balancer.

Both AT horizontal topologies share the same Rust agent-transport core; the
difference is the Python framework on top (pipecat vs livekit-agents). VAD
and turn-detection models are disabled in all three configs so the pipeline
exercises the same STT → LLM → TTS fast path on every turn.

**Pipeline timings (identical across all servers):** STT 200 ms processing;
LLM 150 ms time-to-first-token, then ~15 tokens at 40 ms each; TTS 80 ms
time-to-first-byte, then streams ~600 ms of audio in 20 ms chunks.

**Methodology.** For each implementation we sweep concurrency in coarse steps
to bracket the cliff, then do **5 back-to-back 70-second runs** (10 s warmup
discarded + 60 s steady-state measurement) at candidate concurrencies. The
"highest c" reported is the largest concurrency where **all 5 runs** pass the
`p90 < 50 ms` bar — not the average. Numbers in the table are 5-run averages;
σ is the sample standard deviation.

Each session streams real speech audio (a pre-recorded phrase) followed by
700 ms of silence, repeating for the full run duration, so the server sees
realistic speech/silence turn boundaries.
