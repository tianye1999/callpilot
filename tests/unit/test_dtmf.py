"""DTMF 带内双音合成。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from agentcall.dtmf import DTMF_FREQUENCIES, dtmf_tone


def _goertzel_power(samples: np.ndarray, sample_rate: int, freq: float) -> float:
    omega = 2.0 * math.pi * freq / sample_rate
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in samples.astype(np.float64):
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


def test_dtmf_tone_contains_standard_dual_frequencies():
    sample_rate = 8000
    pcm = dtmf_tone("5", sample_rate, tone_ms=120, gap_ms=80)
    samples = np.frombuffer(pcm, dtype=np.int16)
    tone_samples = samples[: int(sample_rate * 0.12)]
    low_freq, high_freq = DTMF_FREQUENCIES["5"]

    low_power = _goertzel_power(tone_samples, sample_rate, low_freq)
    high_power = _goertzel_power(tone_samples, sample_rate, high_freq)
    other_power = max(
        _goertzel_power(tone_samples, sample_rate, freq)
        for freq in (697, 770, 852, 941, 1209, 1336, 1477, 1633)
        if freq not in {low_freq, high_freq}
    )

    assert low_power > other_power * 20
    assert high_power > other_power * 20


def test_dtmf_tone_duration_gap_and_amplitude_are_bounded():
    sample_rate = 8000
    pcm = dtmf_tone("12", sample_rate, tone_ms=100, gap_ms=50, amplitude=0.35)
    samples = np.frombuffer(pcm, dtype=np.int16)
    samples_per_digit = int(sample_rate * 0.15)
    tone_samples = int(sample_rate * 0.10)

    assert len(samples) == samples_per_digit * 2
    assert np.max(np.abs(samples)) <= int(32767 * 0.35) + 1
    assert np.any(samples[:tone_samples] != 0)
    assert np.all(samples[tone_samples:samples_per_digit] == 0)
    assert np.any(samples[samples_per_digit:samples_per_digit + tone_samples] != 0)


def test_dtmf_tone_rejects_unknown_digit():
    with pytest.raises(ValueError, match="不支持"):
        dtmf_tone("12x", 8000)
