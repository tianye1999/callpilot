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
    create_audio_bridge,
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


def test_create_audio_bridge_simcom_pcm_uses_serial_bridge():
    """SIMCom PCM 与 NMEA 同为 8k/mono 裸流，复用同一条串口传输实现。"""
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
    """macOS PTY 只接受 ≤230400；921600 抛 ENOTTY 时应降速重开而不是让通话失败。"""
    attempts: list[int] = []

    def fake_serial(port, baudrate, timeout, write_timeout):
        attempts.append(baudrate)
        if baudrate > audio_bridge.PTY_SAFE_BAUDRATE:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
        return _FakeSerial()

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
