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

# 候选标定(2026-07-12,#80-D):120ms/0.35 幅度的双音经 AMR 压缩后处于 IVR 识别
# 下限,且与 Agent 语音在播放队列中边界交错造成混叠。加长、加响,并给双音自带
# 前后静音隔离带。幅度选择：合成公式 composite_peak ≈ amplitude（两路 sin 同相
# 时相加）；amplitude=0.50 → -6.0 dBFS，留足余量同时保证 IVR 识别。注意 0.63
# 是离线 Goertzel 实测纯度（双频能量/窗总能量比），不是 PCM 幅度，不得混用。
# 「整块入队即可独占时间窗」目前仅为候选标定假设——单元测试只验证了 guard 隔离带
# 的数组拼接正确性，尚未在真实 Queue/bridge 并发下证明 guard 不被已有缓冲破坏；
# 待 G2 真机/压测验证后再降级为已证实。
DEFAULT_TONE_MS = 200
DEFAULT_GAP_MS = 120
DEFAULT_AMPLITUDE = 0.50
DEFAULT_LEAD_MS = 100
DEFAULT_TAIL_MS = 120


def dtmf_tone(
    digit: str,
    sample_rate: int,
    tone_ms: int = DEFAULT_TONE_MS,
    gap_ms: int = DEFAULT_GAP_MS,
    amplitude: float = DEFAULT_AMPLITUDE,
    lead_ms: int = DEFAULT_LEAD_MS,
    tail_ms: int = DEFAULT_TAIL_MS,
) -> bytes:
    """Return s16le mono PCM for one or more DTMF digits plus inter-digit gaps.

    输出首尾各带 ``lead_ms``/``tail_ms`` 静音隔离带,把双音与前后语音块的
    边界隔开(防混叠);置 0 可得到裸双音序列。
    """
    digits = (digit or "").strip().upper()
    if not digits:
        return b""
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须为正数")
    if tone_ms <= 0 or gap_ms < 0 or lead_ms < 0 or tail_ms < 0:
        raise ValueError("tone_ms 必须 >0，gap_ms/lead_ms/tail_ms 不能为负数")
    amp = float(amplitude)
    if not math.isfinite(amp) or amp <= 0.0:
        raise ValueError(
            f"amplitude 必须为正有限值，收到 {amplitude!r}——"
            f"零/负/NaN/Inf 会生成纯静音却返回 success"
        )
    if amp > 1.0:
        raise ValueError(
            f"amplitude 必须在 (0, 1] 范围内，收到 {amplitude!r}——"
            f">1 会静默 clamp 到满幅，容易误配置"
        )
    safe_amplitude = amp
    tone_len = int(sample_rate * tone_ms / 1000)
    gap_len = int(sample_rate * gap_ms / 1000)
    lead_len = int(sample_rate * lead_ms / 1000)
    tail_len = int(sample_rate * tail_ms / 1000)
    gap = np.zeros(gap_len, dtype=np.int16)
    chunks: list[np.ndarray] = []
    if lead_len:
        chunks.append(np.zeros(lead_len, dtype=np.int16))

    digit_count = 0
    for ch in digits:
        if ch not in DTMF_FREQUENCIES:
            raise ValueError(f"不支持的 DTMF 按键: {ch}")
        low_freq, high_freq = DTMF_FREQUENCIES[ch]
        t = np.arange(tone_len, dtype=np.float64) / sample_rate
        mixed = (
            np.sin(2.0 * math.pi * low_freq * t)
            + np.sin(2.0 * math.pi * high_freq * t)
        ) * (safe_amplitude / 2.0)
        if digit_count > 0 and gap_len:
            chunks.append(gap)
        chunks.append(np.clip(mixed * 32767.0, -32768, 32767).astype(np.int16))
        digit_count += 1

    if tail_len:
        chunks.append(np.zeros(tail_len, dtype=np.int16))
    return np.concatenate(chunks).tobytes() if chunks else b""
