"""Thin async clients for the wire-mock STT/LLM/TTS services.

Used by both the pipecat and the livekit benchmark plugins so all four
benchmark targets share a single wire path and we only have one place to
adjust mock timings / protocols.

URL resolution:

  MOCK_STT_URL   (default ws://localhost:9000/stt)
  MOCK_LLM_URL   (default http://localhost:9000/v1/chat/completions)
  MOCK_TTS_URL   (default ws://localhost:9000/tts)

The mock service runs all three endpoints on a single port; pass
MOCK_SERVICES_HOST=host:port to override the host part of all three at
once (useful inside docker-compose where the service name resolves).
"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator

import aiohttp


def _default_host() -> str:
    return os.environ.get("MOCK_SERVICES_HOST", "localhost:9000")


def stt_url() -> str:
    return os.environ.get("MOCK_STT_URL", f"ws://{_default_host()}/stt")


def llm_url() -> str:
    return os.environ.get(
        "MOCK_LLM_URL", f"http://{_default_host()}/v1/chat/completions"
    )


def tts_url() -> str:
    return os.environ.get("MOCK_TTS_URL", f"ws://{_default_host()}/tts")


# ── LLM: streaming SSE over HTTP ─────────────────────────────────────────────
async def llm_stream_tokens(
    messages: list[dict],
    *,
    model: str = "mock",
    session: aiohttp.ClientSession | None = None,
) -> AsyncIterator[str]:
    """Yield content tokens as they arrive from the mock LLM SSE stream.

    Accepts an optional pre-built aiohttp session so callers can share
    a single connection pool across many requests (one per turn).
    """
    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True

    try:
        async with session.post(
            llm_url(),
            json={"model": model, "messages": messages, "stream": True},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            resp.raise_for_status()
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="ignore").rstrip("\r\n")
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: "):]
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                token = delta.get("content")
                if token:
                    yield token
    finally:
        if own_session:
            await session.close()


__all__ = ["stt_url", "llm_url", "tts_url", "llm_stream_tokens"]
