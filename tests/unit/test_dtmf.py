"""DTMF 带内双音合成。

测试覆盖：
- 双频识别 (Goertzel)
- 峰值 dBFS（独立于 Goertzel selectivity）
- DTMF bin 选择性 (selectivity)——注意这不是历史 0.63 的「纯度」
  （后者分母是分析窗全谱能量，本测试分母仅为 8 个 DTMF bin 的能量和）
- 前后静音隔离带
- 结构校验（时长、间隔、幅度上界）
- guard 拼接正确性（数组 concat，不声称真实 Queue/bridge 并发）
- 非法入参防护
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from agentcall.dtmf import (
    DEFAULT_AMPLITUDE,
    DEFAULT_GAP_MS,
    DEFAULT_LEAD_MS,
    DEFAULT_TAIL_MS,
    DEFAULT_TONE_MS,
    DTMF_FREQUENCIES,
    dtmf_tone,
)

ALL_DTMF_FREQS = (697, 770, 852, 941, 1209, 1336, 1477, 1633)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _goertzel_power(samples: np.ndarray, sample_rate: int, freq: float) -> float:
    """Goertzel 算法返回 |X(ω)|^2（连续频率，未归一化）。"""
    omega = 2.0 * math.pi * freq / sample_rate
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in samples.astype(np.float64):
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


def _compute_dbfs(peak_abs: float) -> float:
    """20*log10(|peak|/32768)。int16 满幅 = 32767，dBFS 参考 = 32768。"""
    if peak_abs <= 0:
        return -float("inf")
    return 20.0 * math.log10(peak_abs / 32768.0)


def _dtmf_bin_selectivity(
    samples: np.ndarray, sample_rate: int, low_freq: int, high_freq: int
) -> float:
    """DTMF bin 选择性：双频能量 / 全部 8 个 DTMF 频点能量。

    **注意**：这不是历史 #73 标定的 0.63「Goertzel 纯度」。
    历史纯度 = 双频能量 / 分析窗**全谱**总能量（Parseval），分母包含
    所有频率成分（含 guard 静音区、非 DTMF 频段）。本函数分母仅为
    8 个 DTMF bin，衡量的不是「双频占窗总能量比」而是「双频在 DTMF
    频段内的优势度（selectivity）」。两者数值不可直接比较。
    """
    powers = {f: _goertzel_power(samples, sample_rate, f) for f in ALL_DTMF_FREQS}
    target = powers[low_freq] + powers[high_freq]
    total = sum(powers.values())
    return target / total if total > 0 else 0.0


# ---------------------------------------------------------------------------
# 频率成分
# ---------------------------------------------------------------------------


def test_dtmf_tone_contains_standard_dual_frequencies():
    """核心窗 Goertzel 双频功率远超其他 DTMF 频点（≥20×）。"""
    sample_rate = 8000
    pcm = dtmf_tone("5", sample_rate, tone_ms=120, gap_ms=80, lead_ms=0, tail_ms=0)
    samples = np.frombuffer(pcm, dtype=np.int16)
    tone_samples = samples[: int(sample_rate * 0.12)]
    low_freq, high_freq = DTMF_FREQUENCIES["5"]

    low_power = _goertzel_power(tone_samples, sample_rate, low_freq)
    high_power = _goertzel_power(tone_samples, sample_rate, high_freq)
    other_power = max(
        _goertzel_power(tone_samples, sample_rate, freq)
        for freq in ALL_DTMF_FREQS
        if freq not in {low_freq, high_freq}
    )

    assert low_power > other_power * 20
    assert high_power > other_power * 20


# ---------------------------------------------------------------------------
# 峰值 dBFS —— 独立于 selectivity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("digit", ["1", "5", "9", "0", "*", "#"])
def test_dtmf_core_peak_near_target_dbfs(digit: str):
    """默认幅度 0.50 → 合成公式 composite_peak ≈ amplitude → 约 -6 dBFS。

    这是 PCM 幅度的直接断言，不是 selectivity 反推。
    """
    sr = 8000
    pcm = dtmf_tone(digit, sr, lead_ms=0, tail_ms=0)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    peak = float(np.max(np.abs(samples)))
    dbfs = _compute_dbfs(peak)

    # target -6.02 dBFS，离散采样允差 ±1.5 dB
    assert -7.5 <= dbfs <= -5.0, (
        f"{digit}: peak={peak:.0f} dbfs={dbfs:.2f} "
        f"(期望约 -6.0 dBFS，对应 amplitude={DEFAULT_AMPLITUDE})"
    )


# ---------------------------------------------------------------------------
# DTMF bin 选择性 (selectivity) —— 独立于 PCM 幅度
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("digit", ["1", "5", "9", "0", "*", "#"])
def test_dtmf_bin_selectivity_high(digit: str):
    """核心窗 DTMF bin 选择性应 >0.90。

    选择性 = 双频能量 / 全部 8 个 DTMF 频点能量。这是「在 DTMF 族内的
    优势度」，独立于 PCM 幅度。
    """
    sr = 8000
    pcm = dtmf_tone(digit, sr, lead_ms=0, tail_ms=0)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    low_f, high_f = DTMF_FREQUENCIES[digit]

    sel = _dtmf_bin_selectivity(samples, sr, low_f, high_f)
    assert sel > 0.90, (
        f"{digit}: bin selectivity={sel:.4f}（应 >0.90，目标 {low_f}+{high_f}）"
    )


def test_dtmf_selectivity_and_amplitude_are_independent():
    """Bin 选择性不随幅度变化：0.25/0.50/0.75 选择性一致，peak 线性缩放。

    这证明了 0.63（旧值，历史 Goertzel 纯度）是另一个量纲——它不是
    selectivity（本测试验证不随幅度变），也不是 amplitude（peak 随它线性变）。
    如果把 0.63 当 amplitude 用，会得到 -4.0 dBFS 而非目标的 -6 dBFS。
    """
    sr = 8000
    results: dict[float, dict] = {}
    for amp in (0.25, 0.50, 0.75):
        pcm = dtmf_tone("5", sr, amplitude=amp, lead_ms=0, tail_ms=0)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        low_f, high_f = DTMF_FREQUENCIES["5"]
        sel = _dtmf_bin_selectivity(samples, sr, low_f, high_f)
        peak = float(np.max(np.abs(samples)))
        dbfs = _compute_dbfs(peak)
        results[amp] = {"selectivity": sel, "peak": peak, "dbfs": dbfs}

    # 选择性在不同幅度下一致
    selectivities = [r["selectivity"] for r in results.values()]
    assert max(selectivities) - min(selectivities) < 0.03, (
        "选择性应独立于幅度: "
        + ", ".join(f"amp={a}: sel={r['selectivity']:.4f}" for a, r in results.items())
    )

    # peak 随幅度线性缩放
    for amp, r in results.items():
        expected_peak = amp * 32767
        assert r["peak"] >= expected_peak * 0.85, (
            f"amp={amp}: peak={r['peak']:.0f} 远低于 expected≈{expected_peak:.0f}"
        )

    # 0.63 旧值如果当幅度用：dbfs 约 -4.0，与目标 -6.0 差 2dB
    amp_063_peak = 0.63 * 32767
    amp_063_dbfs = _compute_dbfs(amp_063_peak)
    assert amp_063_dbfs > -4.5, (
        f"旧值 0.63 若当 amplitude 峰值 ≈ {amp_063_dbfs:.1f} dBFS，"
        f"远高于目标 -6 dBFS → 证明了 0.63 是纯度/选择性、不是幅度"
    )


# ---------------------------------------------------------------------------
# 结构校验
# ---------------------------------------------------------------------------


def test_dtmf_tone_duration_gap_and_amplitude_are_bounded():
    """多位 DTMF：gap 仅在 digit 之间，末位不跟 gap。"""
    sample_rate = 8000
    tone_ms = 100
    gap_ms = 50
    pcm = dtmf_tone(
        "12", sample_rate, tone_ms=tone_ms, gap_ms=gap_ms, amplitude=0.35,
        lead_ms=0, tail_ms=0,
    )
    samples = np.frombuffer(pcm, dtype=np.int16)
    tone_n = int(sample_rate * tone_ms / 1000)
    gap_n = int(sample_rate * gap_ms / 1000)

    # 结构: tone1 + gap + tone2（仅 digit 间有 gap，末位无）
    assert len(samples) == tone_n * 2 + gap_n
    assert np.max(np.abs(samples)) <= int(32767 * 0.35) + 1
    assert np.any(samples[:tone_n] != 0)
    assert np.all(samples[tone_n:tone_n + gap_n] == 0)
    assert np.any(samples[tone_n + gap_n:] != 0)


def test_dtmf_tone_default_has_silence_isolation_pads():
    """默认输出自带前后静音隔离带(#73:防止与语音块边界交错混叠)。"""
    sample_rate = 8000
    pcm = dtmf_tone("1", sample_rate)
    samples = np.frombuffer(pcm, dtype=np.int16)
    lead = int(sample_rate * DEFAULT_LEAD_MS / 1000)
    tail = int(sample_rate * DEFAULT_TAIL_MS / 1000)
    tone = int(sample_rate * DEFAULT_TONE_MS / 1000)

    assert np.all(samples[:lead] == 0)  # 头部隔离带全静音
    assert np.any(samples[lead:lead + tone] != 0)  # 中间是双音
    assert np.all(samples[-tail:] == 0)  # 尾部隔离带全静音


def test_dtmf_tone_default_amplitude_near_calibrated_peak():
    """默认幅度按 #80-D 标定(0.50 ≈ -6dBFS),合成峰值 ≈ amplitude、不削顶。"""
    sample_rate = 8000
    pcm = dtmf_tone("5", sample_rate)
    samples = np.frombuffer(pcm, dtype=np.int16)
    peak = int(np.max(np.abs(samples)))

    assert peak <= int(32767 * DEFAULT_AMPLITUDE) + 1
    # 双音包络峰值≈amplitude(两正弦同相位点),显著高于旧 0.35 标定
    assert peak > int(32767 * DEFAULT_AMPLITUDE * 0.7)


def test_dtmf_tone_rejects_unknown_digit():
    with pytest.raises(ValueError, match="不支持"):
        dtmf_tone("12x", 8000)


def test_dtmf_tone_rejects_negative_pads():
    with pytest.raises(ValueError, match="不能为负数"):
        dtmf_tone("1", 8000, lead_ms=-1)


# ---------------------------------------------------------------------------
# 非法入参防护：tone_ms<=0 / amplitude<=0 / NaN → 拒绝而非静音
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_tone_ms", [0, -1, -100])
def test_dtmf_tone_rejects_non_positive_tone_ms(bad_tone_ms: int):
    """tone_ms<=0 会生成纯静音，必须拒绝而非返回 success。"""
    with pytest.raises(ValueError, match="tone_ms 必须 >0"):
        dtmf_tone("1", 8000, tone_ms=bad_tone_ms)


@pytest.mark.parametrize("bad_amp,label", [
    (0.0, "zero"),
    (-0.1, "negative"),
    (float("nan"), "NaN"),
    (float("-inf"), "-inf"),
])
def test_dtmf_tone_rejects_non_positive_or_non_finite_amplitude(
    bad_amp: float, label: str
):
    """amplitude<=0/NaN/Inf 会生成静音或垃圾，必须拒绝而非返回 success。"""
    with pytest.raises(ValueError, match="amplitude 必须为正有限值"):
        dtmf_tone("5", 8000, amplitude=bad_amp)


def test_dtmf_tone_rejects_amplitude_above_one():
    """amplitude>1 静默 clamp 到满幅，容易误配置，应明确拒绝。"""
    with pytest.raises(ValueError, match="amplitude 必须在 \\(0, 1\\]"):
        dtmf_tone("5", 8000, amplitude=1.5)


def test_dtmf_tone_accepts_amplitude_at_boundary():
    """边界合法值应正常接受。"""
    # 接近 0 的正值合法
    pcm = dtmf_tone("5", 8000, amplitude=0.001)
    assert len(pcm) > 0
    # 上限 1.0 合法（不再 clamp，直接使用）
    pcm2 = dtmf_tone("5", 8000, amplitude=1.0)
    assert len(pcm2) > 0
    peak = np.max(np.abs(np.frombuffer(pcm2, dtype=np.int16)))
    assert peak <= 32767  # 不削顶


# ---------------------------------------------------------------------------
# guard 拼接正确性（数组 concat 级，不声称真实 Queue/bridge 并发）
# ---------------------------------------------------------------------------


def test_dtmf_guards_protect_core_in_array_concat():
    """验证 guard 隔离带在数组拼接后位置正确。

    **注意**：此测试仅验证 `concatenate(ai, dtmf, ai)` 数组拼接后
    guard 区域位置与 core window 纯净性，不证明真实 bridge._tx_buffer
    或 Queue 并发写入时的 byte 交错安全性。后者需要 G2 真机/压测验证。
    """
    sr = 8000
    rng = np.random.RandomState(20260712)

    ai_n = int(sr * 0.15)
    ai_before = (rng.randn(ai_n) * 0.3 * 32767).astype(np.int16)
    ai_after = (rng.randn(ai_n) * 0.3 * 32767).astype(np.int16)

    dtmf_pcm = dtmf_tone("5", sr)
    dtmf_samples = np.frombuffer(dtmf_pcm, dtype=np.int16)

    combined = np.concatenate([ai_before, dtmf_samples, ai_after])

    lead_n = int(sr * DEFAULT_LEAD_MS / 1000)
    tail_n = int(sr * DEFAULT_TAIL_MS / 1000)
    tone_n = int(sr * DEFAULT_TONE_MS / 1000)

    dtmf_start = len(ai_before)

    # lead guard 全为零
    lead_region = combined[dtmf_start: dtmf_start + lead_n]
    assert np.all(lead_region == 0), (
        f"lead guard 应全为零，max={np.max(np.abs(lead_region))}"
    )

    # core DTMF 窗 bin 选择性高
    core_start = dtmf_start + lead_n
    core = combined[core_start: core_start + tone_n].astype(np.float64)
    low_f, high_f = DTMF_FREQUENCIES["5"]
    sel = _dtmf_bin_selectivity(core, sr, low_f, high_f)
    assert sel > 0.90, f"core bin selectivity={sel:.4f}"

    # tail guard 全为零
    tail_start = core_start + tone_n
    tail_region = combined[tail_start: tail_start + tail_n]
    assert np.all(tail_region == 0), (
        f"tail guard 应全为零，max={np.max(np.abs(tail_region))}"
    )

    # AI 音频在 guard 之外完好
    assert np.any(combined[:dtmf_start] != 0), "DTMF 之前应有 AI 音频"
    after_start = tail_start + tail_n
    assert np.any(combined[after_start:] != 0), "DTMF 之后应有 AI 音频"


def test_dtmf_multi_digit_guards_in_array_concat():
    """多位 DTMF 的 guard/gap 拼接位置正确性。

    **注意**：同 `test_dtmf_guards_protect_core_in_array_concat`——
    数组拼接正确 ≠ 真实并发安全，待 G2 真机验证。
    """
    sr = 8000
    rng = np.random.RandomState(20260712)
    ai_n = int(sr * 0.1)
    ai_before = (rng.randn(ai_n) * 0.3 * 32767).astype(np.int16)
    ai_after = (rng.randn(ai_n) * 0.3 * 32767).astype(np.int16)

    dtmf_pcm = dtmf_tone("123", sr)
    dtmf_samples = np.frombuffer(dtmf_pcm, dtype=np.int16)

    combined = np.concatenate([ai_before, dtmf_samples, ai_after])
    dtmf_start = len(ai_before)

    lead_n = int(sr * DEFAULT_LEAD_MS / 1000)
    gap_n = int(sr * DEFAULT_GAP_MS / 1000)
    tone_n = int(sr * DEFAULT_TONE_MS / 1000)

    # lead guard
    assert np.all(combined[dtmf_start: dtmf_start + lead_n] == 0)

    # 每位 core window 选择性高
    for i, digit in enumerate("123"):
        offset = dtmf_start + lead_n + i * (tone_n + gap_n)
        core = combined[offset: offset + tone_n].astype(np.float64)
        low_f, high_f = DTMF_FREQUENCIES[digit]
        sel = _dtmf_bin_selectivity(core, sr, low_f, high_f)
        assert sel > 0.90, f"digit {digit}: bin selectivity={sel:.4f}"

    # gap 区域全为零
    for i in range(2):
        gap_offset = dtmf_start + lead_n + tone_n + i * (tone_n + gap_n)
        gap_region = combined[gap_offset: gap_offset + gap_n]
        assert np.all(gap_region == 0), (
            f"gap {i}: 应全为零，max={np.max(np.abs(gap_region))}"
        )
