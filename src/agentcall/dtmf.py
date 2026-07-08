"""In-band DTMF tone synthesis for 8 kHz PCM call uplink."""

from __future__ import annotations

import math

import numpy as np

DTMF_FREQUENCIES: dict[str, tuple[int, int]] = {
    "1": (697, 1209),
    "2": (697, 1336),
    "3": (697, 1477),
    "A": (697, 1633),
    "4": (770, 1209),
    "5": (770, 1336),
    "6": (770, 1477),
    "B": (770, 1633),
    "7": (852, 1209),
    "8": (852, 1336),
    "9": (852, 1477),
    "C": (852, 1633),
    "*": (941, 1209),
    "0": (941, 1336),
    "#": (941, 1477),
    "D": (941, 1633),
}

DEFAULT_TONE_MS = 120
DEFAULT_GAP_MS = 80
DEFAULT_AMPLITUDE = 0.35


def dtmf_tone(
    digit: str,
    sample_rate: int,
    tone_ms: int = DEFAULT_TONE_MS,
    gap_ms: int = DEFAULT_GAP_MS,
    amplitude: float = DEFAULT_AMPLITUDE,
) -> bytes:
    """Return s16le mono PCM for one or more DTMF digits plus inter-digit gaps."""
    digits = (digit or "").strip().upper()
    if not digits:
        return b""
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须为正数")
    if tone_ms < 0 or gap_ms < 0:
        raise ValueError("tone_ms/gap_ms 不能为负数")
    safe_amplitude = max(0.0, min(float(amplitude), 1.0))
    tone_len = int(sample_rate * tone_ms / 1000)
    gap_len = int(sample_rate * gap_ms / 1000)
    gap = np.zeros(gap_len, dtype=np.int16)
    chunks: list[np.ndarray] = []

    for ch in digits:
        if ch not in DTMF_FREQUENCIES:
            raise ValueError(f"不支持的 DTMF 按键: {ch}")
        low_freq, high_freq = DTMF_FREQUENCIES[ch]
        t = np.arange(tone_len, dtype=np.float64) / sample_rate
        mixed = (
            np.sin(2.0 * math.pi * low_freq * t)
            + np.sin(2.0 * math.pi * high_freq * t)
        ) * (safe_amplitude / 2.0)
        chunks.append(np.clip(mixed * 32767.0, -32768, 32767).astype(np.int16))
        if gap_len:
            chunks.append(gap)

    return np.concatenate(chunks).tobytes() if chunks else b""
