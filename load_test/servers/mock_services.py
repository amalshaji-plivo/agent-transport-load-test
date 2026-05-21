"""Wire-mock STT/LLM/TTS services — real sockets, real streaming,
realistic-timing distributions.

Replaces the previous inline `asyncio.sleep` shims and ALSO replaces the
prior fixed-timing wire mocks. Per-call latency is now sampled from
log-normal distributions tuned to match published p50/p95/p99 figures
for Deepgram-style STT, OpenAI gpt-4o-mini-style LLM, and OpenAI tts-1
streaming TTS.

Why this matters for the bench:
  * Fixed timings hide jitter that real services inject into the
    agent's pipeline (tail latency, slow tokens, the occasional 2s
    TTFT). The agent's CPU profile is invariant to mock latency, but
    queue depths, asyncio scheduling pressure, and AudioSource pacer
    behaviour vary measurably with response time variance.
  * Variable response *length* exercises the TTS streaming code path
    over a realistic range of audio durations.
  * Occasional slow turns (5 % p99 tail) reveal whether the agent's
    interruption / timeout machinery degrades gracefully.

Timing distributions
====================
All sampled from log-normal (constrained to a clip range to avoid
absurd outliers). Means / p99s are calibrated against the references
in `references/realism.md` (Deepgram nova-3, OpenAI gpt-4o-mini,
OpenAI tts-1 production telemetry mid-2024).

  STT      partial interval  : 200 ms ± 80 ms                   (p99 ~450 ms)
           final processing  : 200 ms ± 100 ms                  (p99 ~700 ms)
           tail-event freq.  :   3 % of finals → 1.5–3 s        (slow STT)

  LLM      TTFT              : log-normal μ=ln(0.5) σ=0.6        (p50 500 ms, p99 ~2 s)
           token interval    :  40 ms ± 15 ms                   (p99 ~120 ms)
           response length   : Poisson(λ=25) clipped 5–60        (mean 25 tokens)
           tail-event freq.  :   5 % of turns → TTFT 2–5 s

  TTS      first-chunk       : log-normal μ=ln(0.25) σ=0.5       (p50 250 ms, p99 ~900 ms)
           chunk interval    :  20 ms ± 4 ms                    (p99 ~32 ms)
           chunk count       : ~1 chunk / 2 chars of input      (variable per phrase)

Set MOCK_DETERMINISTIC=1 to fall back to fixed-mean timings (used by
unit smoke tests; bench should leave it off).
"""

from __future__ import annotations

import asyncio
import argparse
import json
import math
import os
import random
import struct
import sys
import time

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from loguru import logger


# ── Distribution helpers ─────────────────────────────────────────────────────
DETERMINISTIC = os.getenv("MOCK_DETERMINISTIC", "").lower() in ("1", "true", "yes")


def _lognorm(median: float, sigma: float, lo: float, hi: float) -> float:
    """Sample a log-normal whose median is `median` (log-mean ln(median)),
    clipped to [lo, hi]. Returns `median` when MOCK_DETERMINISTIC is set
    (used by unit smoke tests).
    """
    if DETERMINISTIC:
        return median
    mu = math.log(median)
    x = random.lognormvariate(mu, sigma)
    return min(max(x, lo), hi)


def _gauss(mean: float, sigma: float, lo: float, hi: float) -> float:
    """Sample a clipped normal. DETERMINISTIC → returns `mean`."""
    if DETERMINISTIC:
        return mean
    x = random.gauss(mean, sigma)
    return min(max(x, lo), hi)


def _poisson(lam: float) -> int:
    """Knuth's Poisson sampler. DETERMINISTIC → returns int(lam)."""
    if DETERMINISTIC:
        return int(lam)
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while p > L:
        k += 1
        p *= random.random()
    return k - 1


# ── STT timing ──────────────────────────────────────────────────────────────
STT_PARTIAL_INTERVAL_MEAN_S = 0.20
STT_PARTIAL_INTERVAL_SIGMA  = 0.08
STT_FRAMES_PER_TURN         = 100         # 100 × 20 ms = 2 s utterance -> final
STT_FINAL_PROC_MEDIAN_S     = 0.20
STT_FINAL_PROC_SIGMA        = 0.5         # log-normal sigma
STT_FINAL_TAIL_PROB         = 0.03        # 3 % of finals slow
STT_FINAL_TAIL_MIN_S        = 1.5
STT_FINAL_TAIL_MAX_S        = 3.0


def _stt_final_delay() -> float:
    if random.random() < STT_FINAL_TAIL_PROB and not DETERMINISTIC:
        return random.uniform(STT_FINAL_TAIL_MIN_S, STT_FINAL_TAIL_MAX_S)
    return _lognorm(STT_FINAL_PROC_MEDIAN_S, STT_FINAL_PROC_SIGMA, 0.05, 1.5)


def _stt_partial_interval() -> float:
    return _gauss(STT_PARTIAL_INTERVAL_MEAN_S, STT_PARTIAL_INTERVAL_SIGMA, 0.05, 0.5)


# ── LLM timing ──────────────────────────────────────────────────────────────
LLM_TTFT_MEDIAN_S     = 0.50
LLM_TTFT_SIGMA        = 0.6        # log-normal sigma — wide tail
LLM_TTFT_TAIL_PROB    = 0.05       # 5 % of turns "slow"
LLM_TTFT_TAIL_MIN_S   = 2.0
LLM_TTFT_TAIL_MAX_S   = 5.0
LLM_TOKEN_INTERVAL_MEAN_S = 0.040
LLM_TOKEN_INTERVAL_SIGMA  = 0.015
LLM_TOKEN_SLOW_PROB   = 0.05       # 5 % of tokens take 100-200 ms
LLM_TOKEN_SLOW_MIN_S  = 0.10
LLM_TOKEN_SLOW_MAX_S  = 0.20
LLM_TOKENS_MEAN       = 25         # Poisson lambda
LLM_TOKENS_MIN        = 5
LLM_TOKENS_MAX        = 60


def _llm_ttft() -> float:
    if random.random() < LLM_TTFT_TAIL_PROB and not DETERMINISTIC:
        return random.uniform(LLM_TTFT_TAIL_MIN_S, LLM_TTFT_TAIL_MAX_S)
    return _lognorm(LLM_TTFT_MEDIAN_S, LLM_TTFT_SIGMA, 0.05, 8.0)


def _llm_token_interval() -> float:
    if random.random() < LLM_TOKEN_SLOW_PROB and not DETERMINISTIC:
        return random.uniform(LLM_TOKEN_SLOW_MIN_S, LLM_TOKEN_SLOW_MAX_S)
    return _gauss(LLM_TOKEN_INTERVAL_MEAN_S, LLM_TOKEN_INTERVAL_SIGMA, 0.005, 0.3)


def _llm_response_token_count() -> int:
    n = _poisson(LLM_TOKENS_MEAN) if not DETERMINISTIC else LLM_TOKENS_MEAN
    return max(LLM_TOKENS_MIN, min(LLM_TOKENS_MAX, n))


# ── TTS timing ──────────────────────────────────────────────────────────────
TTS_FIRST_CHUNK_MEDIAN_S = 0.25
TTS_FIRST_CHUNK_SIGMA    = 0.5     # log-normal sigma
TTS_CHUNK_INTERVAL_MEAN_S = 0.020
TTS_CHUNK_INTERVAL_SIGMA  = 0.004
TTS_CHARS_PER_CHUNK       = 2.0    # ~1 audio chunk per 2 input chars
TTS_MIN_CHUNKS            = 8
TTS_MAX_CHUNKS            = 80
TTS_SAMPLE_RATE           = 8000


def _tts_first_chunk_delay() -> float:
    return _lognorm(TTS_FIRST_CHUNK_MEDIAN_S, TTS_FIRST_CHUNK_SIGMA, 0.05, 2.0)


def _tts_chunk_interval() -> float:
    return _gauss(TTS_CHUNK_INTERVAL_MEAN_S, TTS_CHUNK_INTERVAL_SIGMA, 0.005, 0.06)


def _tts_chunk_count(text: str) -> int:
    base = max(1, int(len(text) / TTS_CHARS_PER_CHUNK))
    return max(TTS_MIN_CHUNKS, min(TTS_MAX_CHUNKS, base))


PHRASES = [
    "hello how are you",
    "I need help with my account",
    "can you transfer me to billing",
    "thank you very much",
    "yes that sounds good",
    "no I'd like to speak to a manager",
    "what's the status of my order",
    "could you repeat that please",
]

RESPONSES = [
    "Hello! I'd be happy to help you with that. Let me look into your account right away.",
    "Sure, I can transfer you to our billing department. Please hold for just a moment.",
    "Thank you for calling. Is there anything else I can help you with today?",
    "I understand your concern. Let me check the details and get back to you shortly.",
    "That's a great question. The answer is that we offer several options for your needs.",
    "I'll connect you with someone who can help. One moment please while I transfer.",
    "Your order is currently being processed. Expected delivery within three to five business days.",
    "Of course. Let me explain that one more time, more slowly this time.",
]


def _make_pcm_frame() -> bytes:
    """One 20 ms PCM16-LE frame at 8 kHz — a 440 Hz sine."""
    n = TTS_SAMPLE_RATE // 50  # 160 samples
    return struct.pack(
        f"<{n}h",
        *[int(16000 * math.sin(2 * math.pi * 440 * i / TTS_SAMPLE_RATE))
          for i in range(n)],
    )


_PCM_FRAME = _make_pcm_frame()


app = FastAPI()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ── STT — Deepgram-style WebSocket ───────────────────────────────────────────
@app.websocket("/stt")
async def stt_ws(ws: WebSocket) -> None:
    await ws.accept()
    phrase_idx = random.randint(0, len(PHRASES) - 1)
    frames_in_turn = 0
    last_partial = 0.0
    partial_interval = _stt_partial_interval()

    try:
        while True:
            msg = await ws.receive()
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                break

            # Text frames = control
            if msg.get("text") is not None:
                try:
                    ctrl = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if ctrl.get("type") == "Finalize" and frames_in_turn > 0:
                    await asyncio.sleep(_stt_final_delay())
                    text = PHRASES[phrase_idx % len(PHRASES)]
                    phrase_idx += 1
                    await ws.send_text(json.dumps({
                        "type": "Results",
                        "channel": {
                            "alternatives": [
                                {"transcript": text, "confidence": 0.95}
                            ],
                        },
                        "is_final": True,
                        "speech_final": True,
                    }))
                    frames_in_turn = 0
                    last_partial = 0.0
                    partial_interval = _stt_partial_interval()
                continue

            if msg.get("bytes") is None:
                continue
            frames_in_turn += 1
            now = time.monotonic()

            if (now - last_partial) >= partial_interval:
                text = PHRASES[phrase_idx % len(PHRASES)]
                await ws.send_text(json.dumps({
                    "type": "Results",
                    "channel": {
                        "alternatives": [
                            {"transcript": text, "confidence": 0.85}
                        ],
                    },
                    "is_final": False,
                }))
                last_partial = now
                # Re-sample interval each time — partial cadence drifts a bit
                partial_interval = _stt_partial_interval()

            if frames_in_turn >= STT_FRAMES_PER_TURN:
                await asyncio.sleep(_stt_final_delay())
                text = PHRASES[phrase_idx % len(PHRASES)]
                phrase_idx += 1
                await ws.send_text(json.dumps({
                    "type": "Results",
                    "channel": {
                        "alternatives": [
                            {"transcript": text, "confidence": 0.95}
                        ],
                    },
                    "is_final": True,
                    "speech_final": True,
                }))
                frames_in_turn = 0
                last_partial = 0.0
                partial_interval = _stt_partial_interval()

    except WebSocketDisconnect:
        return


# ── LLM — OpenAI-style streaming SSE ─────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(req: Request) -> StreamingResponse:
    try:
        body = await req.json()
    except Exception:
        body = {}
    messages = body.get("messages") or []
    last_user = next(
        (m for m in reversed(messages) if m.get("role") == "user"),
        None,
    )
    seed = len(last_user.get("content", "")) if last_user else 0
    response_text = RESPONSES[seed % len(RESPONSES)]

    # Real LLMs generate a variable token count. We sample a Poisson and
    # repeat / truncate the canned response to match.
    target_tokens = _llm_response_token_count()
    canonical = response_text.split()
    if target_tokens > len(canonical):
        tokens = (canonical * ((target_tokens // len(canonical)) + 1))[:target_tokens]
    else:
        tokens = canonical[:target_tokens]

    response_id = f"chatcmpl-mock-{int(time.time()*1000)}-{random.randint(0,9999)}"
    created_ts = int(time.time())

    async def gen():
        await asyncio.sleep(_llm_ttft())

        first_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": body.get("model", "mock"),
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(first_chunk)}\n\n"

        for token in tokens:
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": body.get("model", "mock"),
                "choices": [{
                    "index": 0,
                    "delta": {"content": token + " "},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(_llm_token_interval())

        final_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": body.get("model", "mock"),
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            # OpenAI emits a `usage` block on the final chunk when stream
            # options request it; emit it always (livekit-agents ignores it
            # if it didn't ask for usage).
            "usage": {
                "prompt_tokens": sum(len(m.get("content","").split()) for m in messages),
                "completion_tokens": len(tokens),
                "total_tokens": len(tokens) + sum(
                    len(m.get("content","").split()) for m in messages
                ),
            },
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ── TTS — streaming WebSocket ────────────────────────────────────────────────
@app.websocket("/tts")
async def tts_ws(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            msg = await ws.receive()
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                break
            if msg.get("text") is None:
                continue
            try:
                req = json.loads(msg["text"])
            except json.JSONDecodeError:
                continue

            text = req.get("text", "")
            if not text:
                continue
            n_chunks = _tts_chunk_count(text)

            await asyncio.sleep(_tts_first_chunk_delay())

            for _ in range(n_chunks):
                await ws.send_bytes(_PCM_FRAME)
                await asyncio.sleep(_tts_chunk_interval())

            await ws.send_text(json.dumps({"event": "done"}))
    except WebSocketDisconnect:
        return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wire-mock STT/LLM/TTS for the agent-transport load test"
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.getenv("MOCK_SERVICES_PORT", "9000")),
        help="Single port hosting all three endpoints (default: 9000)",
    )
    parser.add_argument(
        "--workers", type=int,
        default=int(os.getenv("MOCK_SERVICES_WORKERS", "1")),
        help="Uvicorn worker processes — bump for very-high-concurrency runs",
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | mock-svc | {message}")
    logger.info(
        f"mock services on :{args.port}  "
        f"(WS /stt, POST /v1/chat/completions, WS /tts; workers={args.workers}, loop=uvloop, "
        f"deterministic={DETERMINISTIC})"
    )
    uvicorn.run(
        "load_test.servers.mock_services:app",
        host="0.0.0.0",
        port=args.port,
        log_level="warning",
        workers=args.workers,
        loop="uvloop",
        backlog=2048,
    )


if __name__ == "__main__":
    main()
