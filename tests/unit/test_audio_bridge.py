"""音频桥纯逻辑单测。"""

from __future__ import annotations

import errno
import subprocess
import threading
import time

import numpy as np
import pytest

import agentcall.audio_bridge as audio_bridge
import agentcall.coreaudio as coreaudio
from agentcall.audio_bridge import (
    NMEA_WRITE_SIZE,
    FfmpegAudioBridge,
    PhoneAgc,
    StreamingPcmDownsampler,
    StreamingPcmRationalResampler,
    apply_pcm_gain,
    apply_phone_clarity,
    apply_soft_limit,
    configure_downlink_agc,
    configure_modem_rate,
    create_audio_bridge,
    make_downlink_agc,
    resample_pcm,
)


@pytest.mark.parametrize("src_rate,dst_rate", [(8000, 24000), (24000, 8000)])
def test_resample_pcm_round_trip_preserves_length_and_waveform(src_rate, dst_rate):
    duration = 0.1
    sample_count = int(src_rate * duration)
    phase = np.arange(sample_count, dtype=np.float64) / src_rate
    original = (np.sin(2 * np.pi * 440 * phase) * 12000).astype(np.int16)

    converted = resample_pcm(original.tobytes(), src_rate, dst_rate)
    restored_bytes = resample_pcm(converted, dst_rate, src_rate)
    restored = np.frombuffer(restored_bytes, dtype=np.int16)

    assert len(converted) == int(sample_count * dst_rate / src_rate) * 2
    assert restored.size == original.size
    assert np.corrcoef(original.astype(np.float64), restored.astype(np.float64))[0, 1] > 0.98
    assert np.sqrt(np.mean(restored.astype(np.float64) ** 2)) == pytest.approx(
        np.sqrt(np.mean(original.astype(np.float64) ** 2)), rel=0.05
    )


def test_configure_modem_rate_updates_simcom_frame_bytes():
    try:
        assert configure_modem_rate(16000) == 16000
        assert audio_bridge.MODEM_RATE == 16000
        assert audio_bridge.SIMCOM_WRITE_SIZE == 640  # 20ms @16k mono s16
        assert audio_bridge.SIMCOM_WIN_WRITE_SIZE == 3200  # 100ms @16k
        assert audio_bridge.NMEA_WRITE_SIZE == 3200
    finally:
        configure_modem_rate(8000)
        assert audio_bridge.SIMCOM_WRITE_SIZE == 320
        assert audio_bridge.SIMCOM_WIN_WRITE_SIZE == 1600


def test_nmea_bridge_frame_size_follows_reconfigured_rate():
    """默认参数是类定义时求值的，写成 write_size=NMEA_WRITE_SIZE 会把 8k 的
    帧长焊死在签名里 → 16k 下每 100ms 只喂一半字节 → 永久欠载、电话侧断续。"""
    try:
        configure_modem_rate(16000)
        bridge = audio_bridge.SerialPcmAudioBridge("/dev/null")
        assert bridge.write_size == audio_bridge.NMEA_WRITE_SIZE == 3200
    finally:
        configure_modem_rate(8000)


def test_streaming_rational_resampler_24k_to_16k_preserves_tone():
    """24k→16k 非整数倍路径应保留通带内正弦，且块切分结果一致。"""
    src = 24000
    dst = 16000
    t = np.arange(src, dtype=np.float64) / src
    pcm = (np.sin(2 * np.pi * 1000 * t) * 12000).astype("<i2").tobytes()
    whole = StreamingPcmRationalResampler(src, dst).process(pcm)
    parts = []
    streaming = StreamingPcmRationalResampler(src, dst)
    for start in range(0, len(pcm), 777):
        parts.append(streaming.process(pcm[start : start + 777]))
    joined = b"".join(parts)
    # 流式与整段长度接近（滤波延迟允许小差异）
    assert abs(len(joined) - len(whole)) <= 8
    y = np.frombuffer(whole, dtype="<i2").astype(np.float64)
    assert y.size > dst // 2
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    freqs = np.fft.rfftfreq(len(y), 1 / dst)
    peak_hz = float(freqs[int(np.argmax(spec[1:])) + 1])
    assert 900 < peak_hz < 1100


def test_streaming_downsampler_is_invariant_to_realtime_chunk_boundaries():
    """同一条连续语音不应因 WebSocket delta 切法不同而产生块边界爆音。"""
    rate = 24000
    phase = np.arange(rate, dtype=np.float64) / rate
    pcm = (
        (np.sin(2 * np.pi * 440 * phase) + 0.2 * np.sin(2 * np.pi * 2800 * phase))
        * 10000
    ).astype("<i2").tobytes()

    whole = StreamingPcmDownsampler(rate, 8000).process(pcm)
    # 故意在 int16 采样中间切开两个 delta；实现必须把半个采样带到下一块。
    split_at = [7786 * 2 + 1, 13001 * 2, 17879 * 2 + 1]
    parts = []
    start = 0
    streaming = StreamingPcmDownsampler(rate, 8000)
    for end in [*split_at, len(pcm)]:
        parts.append(streaming.process(pcm[start:end]))
        start = end

    assert b"".join(parts) == whole


def test_streaming_downsampler_rejects_above_phone_nyquist_alias():
    """24kHz 的 6kHz 分量不能像旧 np.interp 那样折叠成电话里的 2kHz 杂音。"""
    rate = 24000
    phase = np.arange(rate, dtype=np.float64) / rate
    passband = (np.sin(2 * np.pi * 1000 * phase) * 12000).astype("<i2")
    stopband = (np.sin(2 * np.pi * 6000 * phase) * 12000).astype("<i2")

    passed = np.frombuffer(
        StreamingPcmDownsampler(rate, 8000).process(passband.tobytes()),
        dtype="<i2",
    ).astype(np.float64)
    rejected = np.frombuffer(
        StreamingPcmDownsampler(rate, 8000).process(stopband.tobytes()),
        dtype="<i2",
    ).astype(np.float64)

    # 去掉 127-tap FIR 的短暂启动区再比较稳态能量。
    passed_rms = np.sqrt(np.mean(passed[100:] ** 2))
    rejected_rms = np.sqrt(np.mean(rejected[100:] ** 2))
    assert passed_rms > 7000
    assert rejected_rms < passed_rms * 0.01


def test_apply_phone_clarity_lifts_highband_relative_to_lowband():
    """电话清晰度：2.5kHz 相对 300Hz 的能量比应上升（减轻发闷）。"""
    rate = 8000
    t = np.arange(rate, dtype=np.float64) / rate
    mixed = (
        np.sin(2 * np.pi * 300 * t) * 6000 + np.sin(2 * np.pi * 2500 * t) * 6000
    ).astype("<i2")
    out = np.frombuffer(apply_phone_clarity(mixed.tobytes()), dtype="<i2").astype(
        np.float64
    )
    src = mixed.astype(np.float64)

    def band_ratio(x: np.ndarray) -> float:
        spec = np.abs(np.fft.rfft(x[64:] * np.hanning(len(x) - 64)))
        freqs = np.fft.rfftfreq(len(x) - 64, 1 / rate)
        low = float(spec[(freqs >= 200) & (freqs <= 400)].mean())
        high = float(spec[(freqs >= 2200) & (freqs <= 2800)].mean())
        return high / max(low, 1e-9)

    assert band_ratio(out) > band_ratio(src) * 1.25
    assert abs(out).max() <= 32767


def test_serial_pcm_write_timeout_drops_frame_and_resets_output():
    """写超时丢帧并 reset_output_buffer，避免回队把 COM 写死成静音。"""

    class _TimeoutSerial:
        def __init__(self) -> None:
            self.is_open = True
            self.writes = 0
            self.reset_calls = 0

        def write(self, payload: bytes) -> None:
            self.writes += 1
            raise audio_bridge.serial.SerialTimeoutException("timeout")

        def reset_output_buffer(self) -> None:
            self.reset_calls += 1

    bridge = audio_bridge.SerialPcmAudioBridge("COM6", 115200, write_size=320)
    bridge._ser = _TimeoutSerial()
    bridge._running = True
    bridge._tx_primed = True
    bridge._preroll_bytes = 0
    bridge.write_interval_seconds = 0.01
    frame = b"\x11" * 320
    bridge.write_modem_chunks([frame])

    t = threading.Thread(target=bridge._write_loop, daemon=True)
    t.start()
    time.sleep(0.05)
    bridge._running = False
    t.join(timeout=1)

    assert bridge._ser.writes >= 1
    assert bridge._ser.reset_calls >= 1
    assert bridge.pending_output_bytes() == 0


def test_serial_pcm_optional_preroll_holds_until_buffer_fills():
    """显式打开 preroll 时，开场先积再吐。"""
    bridge = audio_bridge.SerialPcmAudioBridge("COM6", 115200, write_size=320)
    bridge._ser = _FakeSerial()
    bridge._preroll_bytes = 640
    bridge._tx_primed = False
    silence = b"\x00" * 320
    bridge.write_modem_chunks([b"\x01" * 320])
    assert bridge._next_write_payload(silence) == silence
    assert bridge._tx_primed is False
    bridge.write_modem_chunks([b"\x02" * 320])
    assert bridge._next_write_payload(silence) == b"\x01" * 320
    assert bridge._tx_primed is True


def test_soft_limit_is_transparent_below_threshold():
    """常规响度不过限：软限幅应原样返回，避免压高频发闷。"""
    rate = 8000
    t = np.arange(rate, dtype=np.float64) / rate
    pcm = (np.sin(2 * np.pi * 800 * t) * 12000).astype("<i2").tobytes()
    assert apply_soft_limit(pcm) == pcm


def test_soft_limit_engages_only_on_hot_peaks():
    """尖峰超过阈值才压缩。"""
    rate = 8000
    t = np.arange(rate, dtype=np.float64) / rate
    # 构造明确超 28000 的尖峰块
    hot = apply_pcm_gain(
        (np.sin(2 * np.pi * 800 * t) * 22000).astype("<i2").tobytes(), 1.5
    )
    y = np.frombuffer(apply_soft_limit(hot), dtype="<i2")
    assert int(np.abs(y).max()) < 30000


# ---- 下行 AGC（#119 后续：窄带里靠动态压缩救句尾/轻辅音）----


def _rms_dbfs(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    return 20.0 * np.log10(max(float(np.sqrt(np.mean(np.square(x)))), 1e-12))


def _tone(amplitude: float, seconds: float = 1.0, rate: int = 8000) -> bytes:
    t = np.arange(int(rate * seconds), dtype=np.float64) / rate
    return (np.sin(2 * np.pi * 700 * t) * amplitude).astype("<i2").tobytes()


def _agc_stream(agc: PhoneAgc, pcm: bytes, frame: int = 320) -> bytes:
    """按 20ms 帧喂，模拟真实流式调用（也顺带验证跨块状态可用）。"""
    return b"".join(
        agc.process(pcm[i : i + frame]) for i in range(0, len(pcm), frame)
    )


def _peak(pcm: bytes) -> float:
    """int16 上不能直接 np.abs：-32768 取绝对值仍是 -32768（回绕），
    负轨削顶会被漏掉。先转 float64 再取。"""
    return float(np.max(np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.float64))))


def test_phone_agc_lifts_quiet_speech_by_makeup_gain():
    """阈值以下的轻声段不压缩，整体抬 makeup(= target - threshold)。"""
    agc = PhoneAgc(8000, target_dbfs=-18.0)
    quiet = _tone(900)  # ≈ -34 dBFS，在默认阈值 -28 之下
    out = _agc_stream(agc, quiet)

    assert _rms_dbfs(quiet) < agc.threshold_dbfs
    assert agc.makeup_db == pytest.approx(10.0)
    tail = _rms_dbfs(out[-8000:])
    assert tail == pytest.approx(_rms_dbfs(quiet) + agc.makeup_db, abs=0.5)


def test_phone_agc_opens_at_steady_gain_without_fade_in():
    """增益从 makeup 起步：从 0dB 起会被 release 拖出约 500ms 渐强，
    而 PhoneAgc 是每通新建的——那等于每通开场白都淡入。"""
    agc = PhoneAgc(8000, target_dbfs=-18.0)
    out = _agc_stream(agc, _tone(900))

    head = _rms_dbfs(out[:800])  # 前 100ms
    assert head == pytest.approx(_rms_dbfs(out[-8000:]), abs=0.5)


def test_phone_agc_pulls_loud_input_down_without_clipping():
    agc = PhoneAgc(8000, target_dbfs=-18.0)
    loud = _tone(26000)  # ≈ -5 dBFS
    out = _agc_stream(agc, loud)

    tail = _rms_dbfs(out[-8000:])
    assert tail < _rms_dbfs(loud) - 3.0
    assert _peak(out) < 32767


def test_phone_agc_does_not_hard_clip_on_loud_onset_after_gate_hold():
    """门限下冻结的高增益撞上突然的强音起头，若不做前瞻会在 AGC 内部硬削。

    AGC 内部一旦削平，下游 apply_soft_limit 只是把已失真的波形整体缩小，
    救不回来——所以这里必须钉住「AGC 自己不产生满幅样本」。
    """
    agc = PhoneAgc(8000, target_dbfs=-18.0)
    quiet = _tone(900, seconds=0.5)          # 增益爬到 +10dB
    hush = (np.zeros(1600) + 2).astype("<i2").tobytes()  # 200ms 门限下，冻结增益
    onset = _tone(30000, seconds=0.3)        # 近满幅突入

    out = _agc_stream(agc, quiet + hush + onset)

    assert _peak(out) < 32767


def test_phone_agc_narrows_call_to_call_loudness_spread():
    """AGC 的主要价值：不同通话/不同 TTS 电平收敛到接近的落点。

    真机三通 downlink 的 speech RMS 原本散在 -16.7 ~ -21.6 dBFS(≈4.9dB)，
    过 AGC 后收到 ~1.3dB 内。3:1 压缩不做全量归一（那会听着不自然），
    所以这里按「压缩比」验收而不是钉死绝对值。
    """
    amplitudes = (2600, 3600, 5000)  # 都在阈值之上，跨度 ~5.7dB
    ins = [_rms_dbfs(_tone(a)) for a in amplitudes]
    outs = []
    for amplitude in amplitudes:
        agc = PhoneAgc(8000, target_dbfs=-18.0)
        outs.append(_rms_dbfs(_agc_stream(agc, _tone(amplitude, seconds=1.5))[-8000:]))

    assert (max(outs) - min(outs)) < (max(ins) - min(ins)) / 2.5


def test_phone_agc_target_knob_stays_effective_across_its_range():
    """threshold 若写死，target 在自己的量程里会有一大截完全不起作用
    （target ≤ threshold 时 makeup 恒为 0，日志却照样报 target）。"""
    speech = _tone(4600)  # ≈ -20 dBFS，各档 target 下都在阈值之上
    levels = [
        _rms_dbfs(_agc_stream(PhoneAgc(8000, target_dbfs=t), speech)[-8000:])
        for t in (-30.0, -24.0, -18.0, -12.0)
    ]
    assert all(b > a + 3.0 for a, b in zip(levels, levels[1:]))


def test_phone_agc_partial_trailing_block_does_not_spike_gain():
    """块不整除时尾块靠补零凑长度，RMS 会被稀释 → 增益虚高，逐块累积成周期性抖动。

    喂「整除」和「多出半块」两种长度的同一信号，尾部稳态增益应一致。
    """
    steady = _tone(3000, seconds=0.4)
    aligned = PhoneAgc(8000, target_dbfs=-18.0)
    ragged = PhoneAgc(8000, target_dbfs=-18.0)

    a = _agc_stream(aligned, steady, frame=320)  # 20ms = 整 4 个 5ms 块
    b = _agc_stream(ragged, steady, frame=360)   # 22.5ms = 4.5 个块

    assert _rms_dbfs(a[-4000:]) == pytest.approx(_rms_dbfs(b[-4000:]), abs=0.3)


def test_phone_agc_holds_gain_on_silence_instead_of_climbing():
    """gate 以下**冻结**增益：静音段不会被越抬越高（呼吸声/底噪泵动）。

    注意冻结不等于不放大——已冻住的增益照样作用在底噪上；能钉住的是
    「不随时长继续爬」。用刚跑过语音的实例测才有意义，新实例是白测。
    """
    agc = PhoneAgc(8000, target_dbfs=-18.0)
    _agc_stream(agc, _tone(3000, seconds=0.5))  # 先跑语音，让增益处于工作态
    near_silence = (np.zeros(8000) + 3).astype("<i2").tobytes()
    out = _agc_stream(agc, near_silence)

    assert _rms_dbfs(out[-1600:]) == pytest.approx(_rms_dbfs(out[:1600]), abs=0.5)


def test_phone_agc_gain_is_continuous_across_chunk_boundaries():
    """块间线性插值增益：帧边界不得出现台阶（否则听成咔哒）。

    必须用「有电平变化」的信号 + 「非整块」的帧长，否则增益本来就几乎不动，
    把插值整段删掉测试照样过（实测删掉插值仍只有 0.049，而阈值写的 0.05）。
    判据也不能钉绝对值：attack 期间块间增益本来就该大幅变化，要钉的是
    「这个变化被摊到了整块 40 个样本上」而不是一步跳完。
    """
    agc = PhoneAgc(8000, target_dbfs=-18.0)
    pcm = _tone(600, seconds=0.25) + _tone(9000, seconds=0.25)
    # 166 样本 = 332 字节，不是 5ms 块(40 样本)的整数倍，走尾块残留那条路径
    out = np.frombuffer(_agc_stream(agc, pcm, frame=332), dtype="<i2").astype(
        np.float64
    )
    src = np.frombuffer(pcm, dtype="<i2").astype(np.float64)

    # 只在幅度够大处反解增益，且只比较**原始下标相邻**的两点：正弦过零附近被
    # 滤掉后，剩下的点在数组里相邻、在时间上却隔着半个周期，直接 diff 会虚高。
    idx = np.flatnonzero(np.abs(src) > 200)
    applied = out[idx] / src[idx]
    steps = np.abs(np.diff(applied))[np.diff(idx) == 1]

    assert float(steps.max()) < (float(applied.max()) - float(applied.min())) / 20.0


def test_phone_agc_state_is_not_shared_between_instances():
    """每通新建实例：上一通结尾的增益不能带进下一通开头。"""
    warmed = PhoneAgc(8000, target_dbfs=-18.0)
    _agc_stream(warmed, _tone(26000))  # 大声段把增益压到负值
    fresh = PhoneAgc(8000, target_dbfs=-18.0)

    probe = _tone(900, seconds=0.05)  # 短到 release 爬不上来
    assert _rms_dbfs(fresh.process(probe)) > _rms_dbfs(warmed.process(probe)) + 5.0


def test_make_downlink_agc_respects_process_configuration():
    try:
        configure_downlink_agc(False, -18.0)
        assert make_downlink_agc() is None

        configure_downlink_agc(True, -14.0)
        agc = make_downlink_agc()
        assert agc is not None
        assert agc.target_dbfs == pytest.approx(-14.0)

        # 目标电平钳位：太热会常驻限幅，太冷等于没开。
        configure_downlink_agc(True, 6.0)
        assert make_downlink_agc().target_dbfs == pytest.approx(-6.0)

        # NaN 穿得过 min/max（与 NaN 比较恒 False），而 NaN 增益 = 整通数字静音。
        configure_downlink_agc(True, float("nan"))
        assert make_downlink_agc().target_dbfs == pytest.approx(-18.0)
    finally:
        configure_downlink_agc(True, -18.0)


def test_make_downlink_agc_follows_reconfigured_modem_rate():
    """采样率要在建 AGC 时现取：写死 8k 的话 16k 下块长会短一半。"""
    try:
        audio_bridge.configure_modem_rate(16000)
        configure_downlink_agc(True, -18.0)
        agc = make_downlink_agc()
        assert agc.sample_rate == 16000
        assert agc._block == 80
    finally:
        audio_bridge.configure_modem_rate(8000)
        configure_downlink_agc(True, -18.0)


def test_serial_bridge_amplify_runs_agc_when_enabled():
    """桥的下行链路要真的挂上 AGC，且关闭时保持旧行为（仅 clarity+gain+limit）。"""
    quiet = _tone(700)
    try:
        configure_downlink_agc(False, -18.0)
        off = audio_bridge.SerialPcmAudioBridge("COM6", 115200, write_size=320)
        assert off._downlink_agc is None
        baseline = off.amplify_for_modem(quiet)

        configure_downlink_agc(True, -18.0)
        on = audio_bridge.SerialPcmAudioBridge("COM6", 115200, write_size=320)
        assert on._downlink_agc is not None
        boosted = on.amplify_for_modem(quiet)
    finally:
        configure_downlink_agc(True, -18.0)

    assert _rms_dbfs(boosted[-8000:]) > _rms_dbfs(baseline[-8000:]) + 3.0


def make_ffmpeg_bridge() -> FfmpegAudioBridge:
    bridge = FfmpegAudioBridge.__new__(FfmpegAudioBridge)
    bridge._tx_buffer = bytearray()
    bridge._tx_lock = threading.Lock()
    bridge._writer_thread = None
    bridge._running = False
    bridge._cap = None
    bridge._play = None
    bridge._dropped_bytes = 0
    bridge._drop_events = 0
    bridge._consecutive_play_restarts = 0
    bridge._silence_writes = 0
    bridge._write_stats = FakeWriteStats()
    return bridge


class FakeWriteStats:
    def __init__(self) -> None:
        self.payloads = []

    def add(self, payload: bytes) -> None:
        self.payloads.append(payload)

    def maybe_log(self, **_fields) -> bool:
        return False


class FakePipe:
    def __init__(self, fd: int = 91) -> None:
        self.fd = fd

    def fileno(self) -> int:
        return self.fd


class FakeProcess:
    def __init__(self, *, wait_times_out: bool = False, fd: int = 91) -> None:
        self.stdin = FakePipe(fd)
        self.wait_times_out = wait_times_out
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> int:
        if self.wait_times_out and not self.killed:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


def test_ffmpeg_uac_write_payload_keeps_silence_clock_when_empty():
    bridge = make_ffmpeg_bridge()
    silence = b"\x00" * NMEA_WRITE_SIZE

    assert bridge._next_write_payload(silence) == (silence, 0)


def test_ffmpeg_uac_write_payload_pads_partial_agent_audio():
    bridge = make_ffmpeg_bridge()
    silence = b"\x00" * NMEA_WRITE_SIZE
    bridge.write_modem_chunks([b"\x01\x02\x03"])

    payload, real_bytes = bridge._next_write_payload(silence)

    assert len(payload) == NMEA_WRITE_SIZE
    assert payload[:3] == b"\x01\x02\x03"
    assert payload[3:] == b"\x00" * (NMEA_WRITE_SIZE - 3)
    assert real_bytes == 3
    assert bridge.pending_output_bytes() == 0


def test_ffmpeg_uac_write_payload_consumes_one_realtime_frame():
    bridge = make_ffmpeg_bridge()
    silence = b"\x00" * NMEA_WRITE_SIZE
    first_frame = b"\x11" * NMEA_WRITE_SIZE
    remainder = b"\x22" * 7
    bridge.write_modem_chunks([first_frame + remainder])

    payload, real_bytes = bridge._next_write_payload(silence)

    assert payload == first_frame
    assert real_bytes == NMEA_WRITE_SIZE
    assert bridge.pending_output_bytes() == len(remainder)


def test_ffmpeg_uac_tx_buffer_drops_oldest_aligned_pcm_without_blocking():
    bridge = make_ffmpeg_bridge()
    bridge._MAX_TX_BUFFER_BYTES = 8
    bridge._tx_buffer.extend(b"\x00\x01")

    writer = threading.Thread(
        target=bridge.write_modem_chunks,
        args=([bytes(range(2, 12))],),
    )
    writer.start()
    writer.join(timeout=0.2)

    assert not writer.is_alive()
    assert bytes(bridge._tx_buffer) == bytes(range(4, 12))
    assert bridge._dropped_bytes == 4
    assert bridge._dropped_bytes % 2 == 0


def test_ffmpeg_uac_tx_buffer_keeps_normal_realtime_burst_with_default_cap():
    """真机回归（#82 验收发现）：realtime TTS 是 burst 推送，正常长句 pending
    可达 10-30s——生产默认上限必须完整容纳，否则正常语音开头被丢
    （实测 3s 上限把开场白切掉 12.6s）。锁生产常量，防误改回小值。"""
    bridge = make_ffmpeg_bridge()
    burst_30s = b"\x01\x02" * (audio_bridge.MODEM_RATE * 30)

    bridge.write_modem_chunks([burst_30s])

    assert bridge.pending_output_bytes() == len(burst_30s)
    assert bridge._dropped_bytes == 0


def test_ffmpeg_uac_nonblocking_write_handles_partial_os_writes(monkeypatch):
    bridge = make_ffmpeg_bridge()
    bridge._running = True
    bridge._play = FakeProcess(fd=92)
    written = bytearray()

    monkeypatch.setattr(audio_bridge.select, "select", lambda *_args: ([], [92], []))

    def partial_write(fd, payload):
        assert fd == 92
        part = bytes(payload[:3])
        written.extend(part)
        return len(part)

    monkeypatch.setattr(audio_bridge.os, "write", partial_write)

    assert bridge._write_play_payload(b"abcdefghij") is True
    assert bytes(written) == b"abcdefghij"


def test_ffmpeg_uac_spawn_makes_play_pipe_nonblocking(monkeypatch):
    bridge = make_ffmpeg_bridge()
    bridge.output_index = 3
    process = FakeProcess(fd=99)
    blocking_calls = []

    monkeypatch.setattr(audio_bridge.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        audio_bridge.os,
        "set_blocking",
        lambda fd, blocking: blocking_calls.append((fd, blocking)),
    )

    bridge._spawn_play()

    assert bridge._play is process
    assert blocking_calls == [(99, False)]


def test_ffmpeg_uac_stall_kills_old_process_and_respawns(monkeypatch):
    bridge = make_ffmpeg_bridge()
    old_process = FakeProcess(wait_times_out=True, fd=93)
    bridge._play = old_process
    bridge._tx_buffer.extend(b"old voice")
    bridge._running = True
    bridge._WRITE_DEADLINE_SECONDS = 0.01
    bridge._PLAY_RESTART_DELAY_SECONDS = 0.0
    spawned = []

    monkeypatch.setattr(audio_bridge.select, "select", lambda *_args: ([], [], []))

    def spawn_play():
        spawned.append(True)
        bridge._play = FakeProcess(fd=94)
        bridge._running = False

    monkeypatch.setattr(bridge, "_spawn_play", spawn_play)
    writer = threading.Thread(target=bridge._write_loop)
    writer.start()
    writer.join(timeout=0.3)

    assert not writer.is_alive()
    assert old_process.terminated is True
    assert old_process.killed is True
    assert spawned == [True]
    assert bridge.pending_output_bytes() == 0
    assert bridge._write_stats.payloads == []


def test_ffmpeg_uac_success_resets_consecutive_restart_limit(monkeypatch):
    bridge = make_ffmpeg_bridge()
    bridge._play = FakeProcess(fd=95)
    bridge._running = True
    bridge._MAX_PLAY_RESTARTS = 1
    bridge._PLAY_RESTART_DELAY_SECONDS = 0.0
    outcomes = iter([False, True, False, True])
    spawns = []

    monkeypatch.setattr(audio_bridge, "NMEA_WRITE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        bridge,
        "_next_write_payload",
        lambda _silence: (b"\x00" * NMEA_WRITE_SIZE, 0),
    )

    def write_payload(_payload):
        result = next(outcomes)
        if result and len(spawns) == 2:
            bridge._running = False
        return result

    def spawn_play():
        spawns.append(True)
        bridge._play = FakeProcess(fd=95 + len(spawns))

    monkeypatch.setattr(bridge, "_write_play_payload", write_payload)
    monkeypatch.setattr(bridge, "_spawn_play", spawn_play)

    bridge._write_loop()

    assert spawns == [True, True]
    assert bridge._consecutive_play_restarts == 0


def test_ffmpeg_uac_stop_returns_while_writer_is_stalled(monkeypatch):
    bridge = make_ffmpeg_bridge()
    bridge._play = FakeProcess(wait_times_out=True, fd=98)
    bridge._running = True
    bridge._WRITE_DEADLINE_SECONDS = 1.0

    def stalled_select(_read, _write, _error, timeout):
        time.sleep(min(timeout, 0.01))
        return [], [], []

    monkeypatch.setattr(audio_bridge.select, "select", stalled_select)
    bridge._writer_thread = threading.Thread(target=bridge._write_loop)
    bridge._writer_thread.start()
    time.sleep(0.02)

    started = time.monotonic()
    bridge.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert not bridge._writer_thread.is_alive()


# ---- 平台约束：uac_ffmpeg 仅 macOS（Windows 路径待硬件验证）----


def test_ffmpeg_bridge_rejected_on_non_macos(monkeypatch):
    monkeypatch.setattr(audio_bridge.platforms, "IS_MACOS", False)

    with pytest.raises(RuntimeError, match="仅支持 macOS.*MODEM_AUDIO_MODE=uac"):
        FfmpegAudioBridge("Interface")


def test_create_audio_bridge_uac_ffmpeg_rejected_on_non_macos(monkeypatch):
    monkeypatch.setattr(audio_bridge.platforms, "IS_MACOS", False)

    with pytest.raises(RuntimeError, match="仅支持 macOS"):
        create_audio_bridge("uac_ffmpeg", "Interface", None, 921600)


def test_ffmpeg_bridge_constructs_on_macos(monkeypatch):
    """平台检查不误伤 macOS：设备探测打桩后应正常完成构造。"""
    monkeypatch.setattr(audio_bridge.platforms, "IS_MACOS", True)
    monkeypatch.setattr(
        FfmpegAudioBridge, "_find_avfoundation_input", staticmethod(lambda keyword: 1)
    )
    monkeypatch.setattr(coreaudio, "find_output_index", lambda keyword: 2)

    bridge = FfmpegAudioBridge("Interface")

    assert bridge.input_index == 1
    assert bridge.output_index == 2


def test_create_audio_bridge_invalid_mode_mentions_macos_constraint():
    with pytest.raises(ValueError, match="仅 macOS"):
        create_audio_bridge("bogus", "Interface", None, 921600)


# ---- simcom_pcm 模式：SIMCom(SIM7600 系)PCM over USB ----


def test_create_audio_bridge_simcom_pcm_uses_serial_bridge(monkeypatch):
    """SIMCom PCM 与 NMEA 同为 8k/mono 裸流，复用同一条串口传输实现。

    PTY 路径保持官方 20ms/320B；``/tmp/ec20-pcm`` 在未解析到真实 PTY 时
    ``_is_pty`` 为 False，故显式打成 True 锁定 macOS/Linux 桥行为。
    """
    monkeypatch.setattr(audio_bridge, "_is_pty", lambda _port: True)
    bridge = create_audio_bridge(
        mode="simcom_pcm",
        device_keyword="",
        pcm_port="/tmp/ec20-pcm",
        pcm_baudrate=921600,
        tx_gain=2.0,
    )
    assert isinstance(bridge, audio_bridge.SerialPcmAudioBridge)
    assert bridge.port == "/tmp/ec20-pcm"
    assert bridge.tx_gain == 2.0
    assert bridge.write_size == audio_bridge.SIMCOM_WRITE_SIZE == 320
    assert bridge.write_interval_seconds == audio_bridge.SIMCOM_WRITE_INTERVAL_SECONDS == 0.02


def test_simcom_pcm_paces_usb_audio_as_20ms_frames(monkeypatch):
    """SIM7600 工作样例按 20ms/320B 喂 audio 口，不得退回 100ms 突发。"""
    monkeypatch.setattr(audio_bridge, "_is_pty", lambda _port: True)
    bridge = create_audio_bridge(
        mode="simcom_pcm",
        device_keyword="",
        pcm_port="/tmp/ec20-pcm",
        pcm_baudrate=115200,
    )
    bridge._ser = _FakeSerial()
    bridge._tx_primed = True  # 跳过开场 preroll，只验证 20ms 分帧
    bridge.write_modem_chunks([b"\x01" * 500])
    silence = b"\x00" * bridge.write_size

    assert bridge._next_write_payload(silence) == b"\x01" * 320
    assert bridge.pending_output_bytes() == 180


def test_create_audio_bridge_simcom_pcm_windows_com_uses_100ms_frames():
    """Windows SimTech Audio COM：100ms/1600B@8k，降低每秒 USB 写事务。"""
    bridge = create_audio_bridge(
        mode="simcom_pcm",
        device_keyword="",
        pcm_port="COM6",
        pcm_baudrate=115200,
        tx_gain=1.0,
    )
    assert isinstance(bridge, audio_bridge.SerialPcmAudioBridge)
    assert bridge.write_size == audio_bridge.SIMCOM_WIN_WRITE_SIZE == 1600
    assert (
        bridge.write_interval_seconds
        == audio_bridge.SIMCOM_WIN_WRITE_INTERVAL_SECONDS
        == 0.1
    )


def test_simcom_pcm_windows_com_paces_as_100ms_frames():
    bridge = create_audio_bridge(
        mode="simcom_pcm",
        device_keyword="",
        pcm_port="COM6",
        pcm_baudrate=115200,
    )
    bridge._ser = _FakeSerial()
    bridge._tx_primed = True
    bridge.write_modem_chunks([b"\x01" * 2000])
    silence = b"\x00" * bridge.write_size

    assert bridge._next_write_payload(silence) == b"\x01" * 1600
    assert bridge.pending_output_bytes() == 400


def test_nmea_pcm_keeps_existing_100ms_frames():
    bridge = create_audio_bridge(
        mode="nmea",
        device_keyword="",
        pcm_port="/tmp/ec20-nmea",
        pcm_baudrate=921600,
    )

    assert bridge.write_size == audio_bridge.NMEA_WRITE_SIZE == 1600
    assert bridge.write_interval_seconds == audio_bridge.NMEA_WRITE_INTERVAL_SECONDS == 0.1


def test_create_audio_bridge_simcom_pcm_requires_pcm_port():
    """漏配 MODEM_PCM_PORT 时要明确报错，而不是开一条读不到数据的空桥。"""
    with pytest.raises(RuntimeError, match="MODEM_PCM_PORT"):
        create_audio_bridge(
            mode="simcom_pcm",
            device_keyword="",
            pcm_port="",
            pcm_baudrate=921600,
        )


def test_create_audio_bridge_rejects_unknown_mode_listing_simcom():
    with pytest.raises(ValueError, match="simcom_pcm"):
        create_audio_bridge(
            mode="not_a_mode",
            device_keyword="",
            pcm_port="",
            pcm_baudrate=921600,
        )


# ---- PCM 口是 PTY 时的波特率兜底（真机 2026-08-01：921600 直接炸掉整通电话）----


def test_serial_pcm_bridge_falls_back_when_pty_rejects_baudrate(monkeypatch):
    """macOS PTY 只接受 ≤230400；921600 抛 ENOTTY 时应降速重开而不是让通话失败。

    这里测的是**判不出**目标是 PTY 时的兜底路径（软链接失效、非 macOS 命名），
    所以显式把 _is_pty 打成 False——否则本机真跑过桥时 /tmp/ec20-pcm 已是 PTY
    软链接，走的就是快路径，断言会随环境漂。
    """
    attempts: list[int] = []

    def fake_serial(port, baudrate, timeout, write_timeout):
        attempts.append(baudrate)
        if baudrate > audio_bridge.PTY_SAFE_BAUDRATE:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
        return _FakeSerial()

    monkeypatch.setattr(audio_bridge, "_is_pty", lambda _port: False)
    monkeypatch.setattr(audio_bridge.serial, "Serial", fake_serial)
    bridge = audio_bridge.SerialPcmAudioBridge("/tmp/ec20-pcm", 921600)
    bridge.start()
    try:
        assert attempts == [921600, audio_bridge.PTY_SAFE_BAUDRATE]
    finally:
        bridge.stop()


def test_serial_pcm_bridge_keeps_real_serial_errors(monkeypatch):
    """真串口的其他 OSError 不能被吞成"降速重试"——那会掩盖接错口之类的问题。"""

    def fake_serial(port, baudrate, timeout, write_timeout):
        raise OSError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(audio_bridge.serial, "Serial", fake_serial)
    with pytest.raises(OSError) as excinfo:
        audio_bridge.SerialPcmAudioBridge("/tmp/nope", 921600).start()
    assert excinfo.value.errno == errno.ENOENT


@pytest.mark.parametrize("path,expected", [
    ("/dev/ttys003", True),          # macOS PTY slave
    ("/dev/pts/3", True),            # Linux PTY slave
    ("/dev/cu.usbserial-1420", False),  # 真串口：波特率有物理意义，不能替用户降
    ("/dev/ttys003x", False),        # 前缀相同但不是 PTY
    ("COM4", False),
])
def test_is_pty_recognises_pseudo_terminals(path, expected):
    assert audio_bridge._is_pty(path) is expected


def test_is_pty_follows_symlinks(tmp_path):
    """桥给出的是 /tmp/ec20-pcm 这样的软链接，要解析到真实设备名再判。"""
    link = tmp_path / "ec20-pcm"
    link.symlink_to("/dev/ttys007")     # 目标不必真实存在，realpath 只做路径解析
    assert audio_bridge._is_pty(str(link)) is True


def test_serial_pcm_bridge_opens_pty_at_safe_baudrate_directly(monkeypatch, tmp_path):
    """目标是 PTY 时一次开对：省掉每通电话必然失败一次的 open 和那条像故障的告警。"""
    link = tmp_path / "ec20-pcm"
    link.symlink_to("/dev/ttys007")
    attempts: list[int] = []

    def fake_serial(port, baudrate, timeout, write_timeout):
        attempts.append(baudrate)
        if baudrate > audio_bridge.PTY_SAFE_BAUDRATE:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
        return _FakeSerial()

    monkeypatch.setattr(audio_bridge.serial, "Serial", fake_serial)
    bridge = audio_bridge.SerialPcmAudioBridge(str(link), 921600)
    bridge.start()
    try:
        assert attempts == [audio_bridge.PTY_SAFE_BAUDRATE]
    finally:
        bridge.stop()


def test_serial_pcm_bridge_keeps_configured_baudrate_on_real_serial(monkeypatch):
    """真串口不降速——那里的 921600 是有物理意义的。"""
    attempts: list[int] = []
    timeouts: list[float] = []

    def fake_serial(port, baudrate, timeout, write_timeout):
        attempts.append(baudrate)
        timeouts.append(write_timeout)
        return _FakeSerial()

    monkeypatch.setattr(audio_bridge.serial, "Serial", fake_serial)
    bridge = audio_bridge.SerialPcmAudioBridge("/dev/cu.usbserial-1420", 921600)
    bridge.start()
    try:
        assert attempts == [921600]
        assert timeouts == [1.0]  # Windows Audio COM 需较长写超时
    finally:
        bridge.stop()


def test_serial_pcm_bridge_preclaim_opens_without_writer(monkeypatch):
    """preclaim 只占口，不启写线程；start 复用已打开的串口。"""
    opens = 0

    def fake_serial(port, baudrate, timeout, write_timeout):
        nonlocal opens
        opens += 1
        return _FakeSerial()

    monkeypatch.setattr(audio_bridge.serial, "Serial", fake_serial)
    bridge = audio_bridge.SerialPcmAudioBridge("COM6", 115200)
    bridge.preclaim()
    assert opens == 1
    assert bridge._writer_thread is None
    bridge.start()
    try:
        assert opens == 1  # 不重复 open
        assert bridge._writer_thread is not None
    finally:
        bridge.stop()


def test_serial_pcm_bridge_release_claim_closes_for_cpcmreg_reset(monkeypatch):
    """端点复位前必须能释放 PCM 口，复位后再 preclaim。"""
    opens = 0

    def fake_serial(port, baudrate, timeout, write_timeout):
        nonlocal opens
        opens += 1
        return _FakeSerial()

    monkeypatch.setattr(audio_bridge.serial, "Serial", fake_serial)
    bridge = audio_bridge.SerialPcmAudioBridge("COM6", 115200)
    bridge.preclaim()
    bridge.release_claim()
    assert bridge._ser is None
    bridge.preclaim()
    assert opens == 2
    bridge.stop()


def test_serial_pcm_bridge_does_not_retry_when_already_safe(monkeypatch):
    """已经是安全波特率还报 ENOTTY，说明是别的问题，不该无意义重试。"""
    attempts: list[int] = []

    def fake_serial(port, baudrate, timeout, write_timeout):
        attempts.append(baudrate)
        raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

    monkeypatch.setattr(audio_bridge.serial, "Serial", fake_serial)
    with pytest.raises(OSError):
        audio_bridge.SerialPcmAudioBridge("/tmp/ec20-pcm", 115200).start()
    assert attempts == [115200]


class _FakeSerial:
    """最小 pyserial 替身：只满足 SerialPcmAudioBridge.start/stop 的调用面。"""

    is_open = True

    def reset_input_buffer(self): pass
    def reset_output_buffer(self): pass
    def close(self): self.is_open = False
    def read(self, n): return b""
    def write(self, data): return len(data)


# ---- PTY 半采样对齐（真机 2026-08-01：接通 4.3s 后必现，整通电话炸掉）----


class _ChunkSerial(_FakeSerial):
    """按脚本给定的分片返回，模拟 PTY 在采样中间切断的 read()。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, n):
        return self.chunks.pop(0) if self.chunks else b""


def _bridge_with(chunks):
    bridge = audio_bridge.SerialPcmAudioBridge("/tmp/ec20-pcm", 115200)
    bridge._ser = _ChunkSerial(chunks)
    return bridge


def test_read_chunk_holds_back_odd_trailing_byte():
    """奇数字节直接进 np.frombuffer(int16) 会抛 buffer size 错误。"""
    bridge = _bridge_with([b"\x01\x02\x03"])          # 3 字节 = 1.5 个采样
    out = bridge.read_modem_chunk()
    assert len(out) % 2 == 0
    assert out == b"\x01\x02"
    assert bridge._rx_carry == b"\x03"


def test_read_chunk_rejoins_carry_without_losing_bytes():
    """落单字节必须拼回下一块：丢掉会让后续采样整体错位半字节，全流变噪音。"""
    bridge = _bridge_with([b"\x01\x02\x03", b"\x04\x05\x06"])
    first = bridge.read_modem_chunk()
    second = bridge.read_modem_chunk()
    # 6 字节原样保序流出，一个不丢一个不乱
    assert first + second == b"\x01\x02\x03\x04\x05\x06"
    assert len(second) % 2 == 0


def test_read_chunk_output_always_parses_as_int16():
    """对任意分片方式，输出都必须能被 numpy 当 int16 解析。"""
    import numpy as np

    bridge = _bridge_with([b"\x01", b"\x02\x03", b"\x04\x05\x06\x07", b"\x08"])
    for _ in range(4):
        chunk = bridge.read_modem_chunk()
        np.frombuffer(chunk, dtype=np.int16)   # 不抛即通过


def test_simcom_read_chunk_repairs_one_byte_pcm_phase_shift():
    """低幅人声错一字节会变满幅噪音；SIMCom 路径应自动丢一字节重对齐。"""
    phase = np.arange(320, dtype=np.float64) / audio_bridge.MODEM_RATE
    expected = (np.sin(2 * np.pi * 440 * phase) * 1000).astype("<i2").tobytes()
    bridge = _bridge_with([b"\x7f" + expected])
    bridge.auto_realign = True

    out = bridge.read_modem_chunk()

    assert out == expected
    assert bridge._rx_carry == b""


def test_simcom_realign_does_not_touch_legitimate_loud_pcm():
    """正常的大音量语音不能仅因 RMS 高就被误判为错相。"""
    expected = np.resize(np.array([-12000, 12000], dtype="<i2"), 320).tobytes()
    bridge = _bridge_with([expected])
    bridge.auto_realign = True

    assert bridge.read_modem_chunk() == expected


def test_simcom_startup_guard_mutes_uncertain_full_scale_boundary(monkeypatch):
    """启动边界上两种相位都像噪音时，宁可静音一帧也不送进录音/模型。"""
    noise = np.resize(
        np.array([-30000, 18000, 29000, -17000], dtype="<i2"), 320
    ).tobytes()
    bridge = _bridge_with([noise, noise])
    bridge.auto_realign = True
    bridge.startup_guard_seconds = 2.5
    bridge._started_at = 100.0

    monkeypatch.setattr(audio_bridge.time, "monotonic", lambda: 101.0)
    assert bridge.read_modem_chunk() == b"\x00" * len(noise)

    monkeypatch.setattr(audio_bridge.time, "monotonic", lambda: 103.0)
    assert bridge.read_modem_chunk() == noise


def test_start_clears_stale_carry(monkeypatch):
    """上一通剩的半个字节不能漏到下一通，否则新流从一开始就错位。"""
    monkeypatch.setattr(
        audio_bridge.serial, "Serial",
        lambda port, baudrate, timeout, write_timeout: _FakeSerial(),
    )
    bridge = audio_bridge.SerialPcmAudioBridge("/tmp/ec20-pcm", 115200)
    bridge._rx_carry = b"\x99"
    bridge.start()
    try:
        assert bridge._rx_carry == b""
    finally:
        bridge.stop()
