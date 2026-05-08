"""Mu-law audio generation helpers for the benchmark client.

The benchmark replays short speech turns followed by silence so the VAD-backed
paths see realistic speech/silence boundaries instead of an endless test tone.
"""

import base64
import math
import struct
from pathlib import Path

# ITU G.711 mu-law compression parameter
_MU = 255
_BIAS = 0x84
_CLIP = 32635
_CHUNK_SAMPLES = 160  # 20ms at 8kHz
_SILENCE_FRAMES_PER_TURN = 35  # 700ms trailing silence after each utterance
_SPEECH_PCM16_8KHZ_B64_PATH = Path(__file__).with_name("hello_there_pcm16_8khz.b64")

# Precomputed PCM-to-mulaw lookup for the sign+magnitude encoding
_EXP_TABLE = [0, 132, 396, 924, 1980, 4092, 8316, 16764]


def _pcm_sample_to_mulaw(sample: int) -> int:
    """Encode a single 16-bit signed PCM sample to mu-law byte."""
    sign = 0
    if sample < 0:
        sign = 0x80
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS

    exponent = 7
    for i in range(7, 0, -1):
        if sample >= _EXP_TABLE[i]:
            exponent = i
            break
    else:
        exponent = 0

    mantissa = (sample >> (exponent + 3)) & 0x0F
    mulaw_byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return mulaw_byte


def _pcm_chunk_to_mulaw(pcm_chunk: bytes) -> bytes:
    sample_count = len(pcm_chunk) // 2
    pcm_samples = struct.unpack(f"<{sample_count}h", pcm_chunk)
    return bytes(_pcm_sample_to_mulaw(sample) for sample in pcm_samples)


def _split_pcm16le_chunks(pcm: bytes) -> list[bytes]:
    chunk_bytes = _CHUNK_SAMPLES * 2
    remainder = len(pcm) % chunk_bytes
    if remainder:
        pcm += b"\x00" * (chunk_bytes - remainder)
    return [pcm[i:i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]


def _build_speech_turn_chunks() -> list[bytes]:
    pcm_b64 = _SPEECH_PCM16_8KHZ_B64_PATH.read_text()
    pcm = base64.b64decode(pcm_b64)
    return [_pcm_chunk_to_mulaw(chunk) for chunk in _split_pcm16le_chunks(pcm)]


_SILENCE_MULAW_CHUNK = bytes([_pcm_sample_to_mulaw(0)]) * _CHUNK_SAMPLES
_SPEECH_TURN_CHUNKS = _build_speech_turn_chunks()


class MulawAudioGenerator:
    """Generates 20ms mu-law audio chunks at 8kHz."""

    def __init__(self, sample_rate: int = 8000, frequency: float = 440.0, amplitude: float = 0.5):
        self._sample_rate = sample_rate
        self._frequency = frequency
        self._amplitude = amplitude
        self._chunk_samples = sample_rate // 50
        self._phase = 0.0
        self._phase_increment = 2.0 * math.pi * frequency / sample_rate

    def next_chunk(self) -> bytes:
        """Generate one 20ms chunk of mu-law encoded audio."""
        mulaw_bytes = bytearray(self._chunk_samples)
        for i in range(self._chunk_samples):
            pcm_float = self._amplitude * math.sin(self._phase)
            pcm_sample = int(pcm_float * 32767)
            pcm_sample = max(-32768, min(32767, pcm_sample))

            mulaw_bytes[i] = _pcm_sample_to_mulaw(pcm_sample)
            self._phase += self._phase_increment

        self._phase %= 2.0 * math.pi
        return bytes(mulaw_bytes)

    def next_chunk_pcm(self) -> bytes:
        """Generate one 20ms chunk of raw PCM16-LE audio."""
        samples = []
        for _ in range(self._chunk_samples):
            pcm_float = self._amplitude * math.sin(self._phase)
            pcm_sample = int(pcm_float * 32767)
            pcm_sample = max(-32768, min(32767, pcm_sample))
            samples.append(pcm_sample)
            self._phase += self._phase_increment
        self._phase %= 2.0 * math.pi
        return struct.pack(f"<{len(samples)}h", *samples)


def pregenerate_chunks(n: int = 50, **kwargs) -> list[bytes]:
    """Legacy sine-wave helper retained for ad hoc experiments."""
    gen = MulawAudioGenerator(**kwargs)
    return [gen.next_chunk() for _ in range(n)]


def pregenerate_turn_chunks(silence_frames: int = _SILENCE_FRAMES_PER_TURN) -> list[bytes]:
    """Return one repeatable user turn: speech followed by trailing silence."""
    return [*_SPEECH_TURN_CHUNKS, *([_SILENCE_MULAW_CHUNK] * silence_frames)]
