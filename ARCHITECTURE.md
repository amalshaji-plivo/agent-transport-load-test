# Agent Transport Benchmark Architecture

## Overview

A comparative benchmarking framework that measures voice agent performance under realistic load. It pits two implementations head-to-head:

- **Direct Pipecat (Native)** — pure Python, everything on a single asyncio event loop
- **Agent Transport (AT)** — Rust-backed transport layer with Python pipeline orchestration

The core question: *does offloading transport work to Rust improve voice quality under concurrent load?*

## Service Topology

```
+----------------------------------------------------------+
|                  Benchmark Runner (Host)                  |
|                                                          |
|  +---------------+  +---------------+  +--------------+  |
|  |  Session Mgr  |  |  Session Mgr  |  |   System     |  |
|  |  (N clients)  |  |  (N clients)  |  |   Monitor    |  |
|  +-------+-------+  +-------+-------+  +--------------+  |
+-----------+-----------------+----------------------------+
            | ws://           | ws://
   +--------v-------+  +-----v----------+
   |     Native     |  | Agent Transport|   <-- Docker containers
   |     :8080      |  |     :8081      |       (4 vCPU, 10GB each)
   +--------+-------+  +------+---------+
            |                 |
            +--------+--------+
                     |
            +--------v---------+
            |  Mock Services   |   <-- Single container
            |                  |
            |  STT  :9001 (WS) |   Deepgram-like
            |  LLM  :9002 (SSE)|   OpenAI-like
            |  TTS  :9003 (WS) |   Streaming TTS
            +------------------+
```

## Audio Flow

Each simulated call uses the **Plivo Audio Stream Protocol** over WebSocket:

```
Client (PlivoWsClient)                       Server
  |                                            |
  |---- start {callId, streamId} ------------->|
  |                                            |
  |---- media {mulaw, 20ms frames} ----------->|
  |---- media -------------------------------->|    STT -> LLM -> TTS
  |     ...real-time pacing...                 |    (Python pipeline)
  |                                            |
  |<---- playAudio {mulaw response} -----------|
  |<---- playAudio ----------------------------|
  |     ...paced at 20ms...                    |
  |                                            |
  |---- stop --------------------------------->|
```

## Mock Services

The mocks simulate production streaming latencies, not just echo:

| Service | Protocol | Behavior |
|---------|----------|----------|
| **STT** (:9001) | WebSocket | Partial transcripts every 200ms, final after 500ms silence |
| **LLM** (:9002) | HTTP SSE | 150ms time-to-first-token, then tokens at 40ms intervals |
| **TTS** (:9003) | WebSocket | 80ms synthesis startup, then 30 chunks at 20ms real-time rate |

## Benchmark Profile

The benchmark uses a stepped load profile that progressively increases concurrent sessions:

```
Concurrency

150 |                                          +----------+
    |                                          |  150 ses |
100 |                              +-----------+  90s     |
    |                              |  100 ses  |          |
 75 |                  +-----------+  90s      |          |
    |                  |   75 ses  |           |          |
 50 |      +-----------+   90s     |           |          |
    |      |   50 ses  |           |           |          |
 25 |------+   90s     |           |           |          |
    |25 ses|           |           |           |          |
    | 90s  |           |           |           |          |
    +------+-----------+-----------+-----------+----------+--> Time
```

Each step runs for **90 seconds** with a **10-second warmup** (metrics discarded). Sessions are staggered at startup to avoid thundering herd.

## Metrics Collected

### Voice Quality Metrics

| Metric | What It Measures | Why It Matters |
|--------|------------------|----------------|
| **Within-phrase jitter** | Frame-to-frame timing deviation within a TTS phrase | High jitter = choppy audio |
| **Inter-frame gaps** | Time between consecutive output frames | Gaps > 5ms = audible silence |
| **First-frame latency** | Time from first input to first output | Perceived responsiveness |
| **Frame loss rate** | Dropped frames / sent frames | Missing audio segments |

### System Metrics

| Metric | Collection Method |
|--------|-------------------|
| **CPU %** (mean, peak) | `docker stats` API |
| **Memory MB** (mean, peak) | `docker stats` API |
| **Throughput** | frames/second |

All metrics are aggregated into **percentiles** (p50, p75, p90, p95, p99) per concurrency level, then compared as percentage deltas between the two implementations.

## Execution Flow

```
CLI (--profile custom --target both)
 |
 +- 1. docker compose up
 |     +- mock-services, native, agent-transport
 |     +- health checks (TCP connect, 15 retries)
 |
 +- 2. For each target (native, AT):
 |     +- For each step (25, 50, 75, 100, 150 sessions):
 |         +- Start docker stats monitor (1s sampling)
 |         +- Spawn N WebSocket clients (staggered)
 |         +- Warmup 10s (discard metrics)
 |         +- Measure 90s (collect frame-level metrics)
 |         +- Aggregate: percentiles, peaks, means
 |
 +- 3. Compare results by concurrency level
 |     +- Compute % deltas (AT vs native)
 |
 +- 4. Print comparison table / export JSON
 |
 +- 5. docker compose down
```

## The Architectural Difference Being Tested

```
Native (single Python event loop does everything):
+---------------------------------------------+
|              Python asyncio loop             |
|   transport + pipeline (all compete for GIL) |
+---------------------------------------------+

Agent Transport (split Rust transport + Python pipeline):
+------------------+    +---------------------+
|  Rust transport   |    |  Python pipeline     |
|  (tokio, no GIL)  |<-->|  (asyncio)           |
+------------------+    +---------------------+
```

The benchmark answers: under N concurrent voice sessions, does the Rust transport layer deliver frames more consistently (lower jitter, fewer gaps) while using less CPU?
