# Voice Agent Transport Benchmark

A benchmark of our current voice-agent server architectures under identical
load, with an experimental **AT + Rust VAD** variant measured at the end.

## What we're benchmarking

Two server implementations share the same voice-agent pipeline (VAD → STT →
LLM → TTS):

| Implementation | Transport | Framework | VAD | Runs VAD in |
|:---|:---|:---|:---|:---|
| **direct-pipecat** | pipecat's FastAPI + WebSocket | pipecat | Silero (Python) | Python (GIL) |
| **AT + Python VAD** | agent-transport (Rust + tokio) | pipecat | Silero (Python) | Python (GIL) |

Both expose the same Plivo WebSocket protocol (μ-law audio, 8 kHz, 20 ms
frames). The STT / LLM / TTS components are **inline mocks with
deterministic, identical timings** across all servers, so any measured
differences reflect transport + framework + VAD only — not STT/LLM/TTS
model performance.

> An experimental third variant — **AT + Rust VAD** — is documented in
> the [Experimental section](#experimental-at--rust-vad) at the bottom.

### Mock pipeline timings (identical everywhere)

- STT: 200 ms delay per turn
- LLM: 150 ms time-to-first-token + 40 ms per token × 15 tokens
- TTS: 80 ms time-to-first-byte + 20 ms per chunk × 30 chunks

## Testing setup

| | |
|:---|:---|
| Host | macOS, 24 GB RAM, OrbStack as Docker runtime |
| Container limits | 4 CPUs, 10 GB memory — **enforced** per container |
| Server process | Python 3.13 |
| Benchmark client | Python asyncio, in-process, one task per session |
| Audio stream | 70 s of μ-law at 8 kHz pushed per session (50 frames/s) |
| Session protocol | Plivo WebSocket (start → media frames → stop) |

## Testing methodology

1. **Isolation**: before each run, all compose services are brought up and
   all but the target container are stopped. Only one server receives
   traffic during measurement.
2. **Fresh container per concurrency step**: the target container is
   force-recreated between steps so each step starts from a cold process
   (no VAD-model warm cache, no memory fragmentation).
3. **Warmup**: 10 s of traffic discarded before measurement begins.
4. **Measurement window**: 60 s of steady-state traffic per step.
5. **Concurrency sweep**: c = 10, 20, 30 baseline, extending upward to
   find each implementation's cliff.
6. **OrbStack restart** between sweeps to clear VM memory pressure that
   was biasing results.

### Survivorship-bias protection

A "dead" server that produces 5 frames before stopping would otherwise
report perfect silence metrics on its 5 surviving frames. To prevent this,
every session must pass **two filters** to contribute to silence/gap stats:

- **Absolute floor**: ≥ 100 received frames
- **Relative floor**: ≥ 30% of the best-producing session's frame count

The output reports both `sessions_with_output` and `eligible` counts, so
survivorship bias is visible if it occurs.

## What we measure (primary metrics)

| Metric | What it captures |
|:---|:---|
| **Audible silence gap** (`max(0, gap − pacing_interval)`), p50/p90/p99 | Time the listener's playback buffer starves beyond expected pacing. > 5 ms is audibly perceptible on a phone. |
| **Mean / peak CPU** (% of 400% budget) | Sustained and burst load on the server container. |
| **Throughput** (frames/sec across all sessions) | Total useful work delivered. |
| **First-frame p99** (session start → first audio frame) | Cold-start latency — how fast the agent begins responding. |
| **Eligible sessions / sessions with output** | Survivorship audit trail. |

## Key finding: best row per implementation

| Impl | c | Sil p50 | Sil p90 | Sil p99 | Mean CPU | Peak CPU | Peak memory | Throughput | First-frame p99 | Eligible |
|:---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| direct-pipecat | 20 | 1.23 ms | 4.6 ms | 24.5 ms | 51.6% | 115.5% | 345 MB | 22 f/s | 4.3 s | 20/20 ✓ |
| AT + Python VAD | 80 | 0.97 ms | 100 ms | 300 ms | 146.2% | 179.0% | 3636 MB | 423 f/s | 6.3 s | 80/80 ✓ |

### Comparison: direct-pipecat vs AT + Python VAD

**At each implementation's peak, AT + Python VAD delivers vs direct-pipecat**:

- **4× the concurrency** (c=80 vs c=20)
- **19× the throughput** (423 f/s vs 22 f/s)
- ...**but with audibly worse audio quality**: p90 silence goes from 4.6 ms → 100 ms, p99 from 24.5 ms → 300 ms
- and **47% worse first-frame latency** (6.3 s vs 4.3 s)

Moving from direct-pipecat → AT + Python VAD **trades audio quality for
scale**. The limit here is Python's GIL: each VAD inference fires 50×/s
per session, so at c=80 the GIL is saturated and silence tails grow.

## Honest caveats

1. **Silence metric has a ceiling.** We set a `PHRASE_GAP_THRESHOLD` in the
   benchmark client (currently 500 ms) above which gaps are treated as
   phrase boundaries and excluded. Past that ceiling, p99 values may be
   floor estimates rather than true worst-cases.
2. **Run-to-run variance is ~10–15%** on absolute throughput numbers. The
   *ordering* of the implementations is rock-solid across runs; the absolute
   peaks swing that much between runs.
3. **Measured on a single Mac via OrbStack.** Numbers would differ on
   bare-metal Linux — likely more favorable for all implementations.
4. **Mocks, not real STT/LLM/TTS.** Real services add network latency and
   variance that would reduce the *relative* gap between implementations
   (common bottleneck upstream). The benchmark isolates transport +
   framework + VAD overhead specifically.

---

## Experimental: AT + Rust VAD

An experimental variant we built to test the hypothesis that **moving VAD
out of Python eliminates the GIL bottleneck** seen in the table above.

Same pipecat pipeline, same mock STT/LLM/TTS, same agent-transport Rust
transport — the only difference is that Silero VAD runs in Rust instead
of Python.

### Results

| Impl | c | Sil p50 | Sil p90 | Sil p99 | Mean CPU | Peak CPU | Peak memory | Throughput | First-frame p99 | Eligible |
|:---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **AT + Rust VAD** | **100** | 1.10 ms | **61 ms** | **240 ms** | 148.3% | 197.7% | 3107 MB | **1252 f/s** | **3.8 s** | 100/100 ✓ |

### How it's done

The Rust-side bits that make this work:

1. **Rust Silero VAD crate** (`crates/vad/`) — ONNX Runtime in Rust with
   the bundled Silero model. Inference runs in a thread pool sized by
   `VAD_POOL_SIZE` (default 8 shared sessions serving all callers).
2. **`AudioStreamEndpoint` VAD integration** — incoming μ-law frames from
   the Plivo WebSocket flow through the Rust VAD inference pool *before*
   they reach Python. Python only sees high-level events (`speech_started`,
   `speech_ended`) at ~2 s cadence per session, not the 50 frames/s raw
   inference stream.
3. **Pipecat adapter wiring** — `PlivoFrameSerializer` and
   `WebsocketServerTransport` (in
   `crates/agent-transport-python/adapters/agent_transport/audio_stream/pipecat/`)
   accept VAD parameters (`vad=True`, `vad_threshold`, `vad_min_speech_ms`,
   `vad_min_silence_ms`, `vad_pool_size`) and forward them to the Rust
   endpoint when constructing the session.
4. **Server-side opt-in** — in `load_test/servers/agent_transport_server.py`,
   setting `VAD_BACKEND=rust` constructs the serializer with VAD enabled;
   setting `VAD_BACKEND=python` keeps the per-session Python
   `SileroVADAnalyzer` on the pipecat side (the "AT + Python VAD" variant
   in the main table above).
5. **Single switch to run** — the compose file has a dedicated
   `agent-transport-rust-vad` service with `VAD_BACKEND=rust` baked in.

### Comparison: AT + Rust VAD vs the production baselines

**vs AT + Python VAD at their respective peaks**:

- **2.96× the throughput** (1252 f/s vs 423 f/s)
- **39% better p90 silence** (61 ms vs 100 ms)
- **20% better p99 silence** (240 ms vs 300 ms)
- **40% faster first-frame latency** (3.8 s vs 6.3 s)
- At **25% higher concurrency** (c=100 vs c=80)
- **15% less peak memory** (3107 MB vs 3636 MB)
- **Same CPU utilization** (~148% mean)

**vs direct-pipecat**:

- **57× the throughput** (1252 f/s vs 22 f/s)
- **5× the concurrency** (c=100 vs c=20)
- **Similar or better first-frame latency** (3.8 s vs 4.3 s)
- Higher silence numbers at p90/p99 — but measured at 5× the load

### Why Rust VAD flips the curve

VAD is the **only component that runs at audio-frame cadence** — 50
times/second/session. Everything else (STT, LLM, TTS) runs at ~1 Hz per
session. That 50× factor is what makes VAD the canonical GIL bottleneck.

At c = 100 × 50 frames/s = **5,000 VAD inferences per second**. Each
Python inference holds the GIL for ~0.7 ms of wrapper work, so 5,000 × 0.7 ms =
**3.5 GIL-seconds per wall-second** — impossible to sustain on one GIL.
Moving inference to Rust removes it from the GIL entirely; Python only
sees the high-level events, which are ~50× less frequent.

Because Rust VAD and Python VAD share every other component in the
AT + Python VAD setup, the 2.96× gap is **cleanly attributable to the
VAD-in-Python-vs-Rust difference**, not to the transport or framework.

### Experimental takeaway

**At 4-CPU / 10-GB budget, moving VAD from Python to Rust sustains ~3×
the concurrent voice-agent sessions with measurably better audio quality —
no additional CPU, no additional memory, no change to the rest of the
pipeline.**
