"""音频桥纯逻辑单测。"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from types import SimpleNamespace

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


def test_serial_output_observer_tracks_only_written_agent_audio(monkeypatch):
    from agentcall.audio_bridge import AgentPcm, SerialPcmAudioBridge

    bridge = SerialPcmAudioBridge("unused", write_size=8, write_interval=0)
    observed = []
    writes = []

    class Port:
        is_open = True

        def write(self, payload):
            writes.append(payload)
            if len(writes) == 3:
                bridge._running = False
            return len(payload)

    bridge._ser = Port()
    bridge.set_output_observer(observed.append)
    bridge.write_modem_chunks([b"tone", AgentPcm(b"voice!")])
    bridge._running = True
    bridge._write_loop()

    assert b"".join(observed) == b"voice!"  # No tone or silence padding.
    assert bridge.output_progress_bytes() == 10  # Tone + voice, never filler/padding.
    assert writes == [b"tonevoic", b"e!\x00\x00\x00\x00\x00\x00", b"\x00" * 8]


def test_uac_observer_counts_failure_after_successful_write():
    from types import SimpleNamespace

    from agentcall.audio_bridge import AgentPcm, ModemAudioBridge

    bridge = object.__new__(ModemAudioBridge)
    observed = []
    bridge.set_output_observer(observed.append)
    def write(chunk):
        if chunk == b"bad!":
            raise OSError("输出设备断开")
    bridge._output_stream = SimpleNamespace(write=write)
    bridge.write_modem_chunks([AgentPcm(b"good")])
    with pytest.raises(OSError):
        bridge.write_modem_chunks([AgentPcm(b"bad!")])
    assert observed == [b"good"]
    assert bridge.output_write_errors == 1


def test_turn_callback_follows_written_spans_not_dropped_or_short_suffix():
    from agentcall.audio_bridge import AgentPcm, SerialPcmAudioBridge

    bridge = SerialPcmAudioBridge("unused", write_size=8, write_interval=0)
    bridge.set_output_observer(lambda pcm: None)
    written = []
    first, second = AgentPcm(b"one!"), AgentPcm(b"two!")
    first.on_written = lambda: written.append(1)
    second.on_written = lambda: written.append(2)
    bridge._queue_output_span(first)
    bridge._queue_output_span(second)
    ranges = bridge._take_output_ranges(8)
    bridge._notify_output(b"one", ranges)
    assert written == [1]


def test_serial_output_observer_ignores_timeout_and_discarded_audio():
    import serial

    from agentcall.audio_bridge import AgentPcm, SerialPcmAudioBridge

    bridge = SerialPcmAudioBridge("unused", write_size=4, write_interval=0)
    observed = []

    class Port:
        is_open = True
        calls = 0

        def write(self, payload):
            self.calls += 1
            if self.calls == 1:
                raise serial.SerialTimeoutException()
            bridge._running = False
            return 2  # A short successful write must not observe the suffix.

        def reset_output_buffer(self):
            pass

    bridge._ser = Port()
    bridge.set_output_observer(observed.append)
    bridge.write_modem_chunks([AgentPcm(b"drop")])
    assert bridge.discard_pending_output() == 4
    bridge.write_modem_chunks([AgentPcm(b"fail"), AgentPcm(b"good")])
    bridge._running = True
    bridge._write_loop()
    assert observed == [b"go"]
    assert bridge.output_progress_bytes() == 2  # Discards/timeouts/short-write suffix aren't progress.


def test_ffmpeg_output_observer_ignores_filler_tones_and_restarted_buffer(monkeypatch):
    from agentcall.audio_bridge import AgentPcm

    bridge = make_ffmpeg_bridge()
    bridge._play = FakeProcess(fd=95)
    observed = []
    bridge.set_output_observer(observed.append)
    bridge.write_modem_chunks([AgentPcm(b"old")])
    bridge._drop_stale_tx_buffer()
    bridge.write_modem_chunks([b"tone", AgentPcm(b"voice")])

    def write(_payload):
        bridge._running = False
        return True

    monkeypatch.setattr(bridge, "_write_play_payload", write)
    bridge._running = True
    bridge._write_loop()
    assert observed == [b"voice"]
    assert bridge.output_progress_bytes() == 9


def test_uac_observer_runs_after_output_write_and_never_observes_dtmf():
    from agentcall.audio_bridge import AgentPcm, ModemAudioBridge

    bridge = ModemAudioBridge.__new__(ModemAudioBridge)
    writes = []
    observed = []

    class Stream:
        def write(self, pcm):
            writes.append(pcm)

    bridge._output_stream = Stream()

    def observe(pcm):
        assert pcm in writes
        observed.append(pcm)

    bridge.set_output_observer(observe)
    bridge.write_modem_chunks([b"tone", AgentPcm(b"voice")])
    assert writes == [b"tone", b"voice"]
    assert observed == [b"voice"]
    assert bridge.output_progress_bytes() == 9


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


# ---- SerialPcmAudioBridge: macOS PTY 波特率回退 ----

def test_serial_pcm_open_falls_back_to_115200_on_enotty(monkeypatch):
    """PTY(USB→PTY 桥)不支持自定义波特率 ioctl(ENOTTY)时退回 115200 重开。"""
    import errno

    from agentcall.audio_bridge import SerialPcmAudioBridge

    tried = []

    class FakeSerial:
        def __init__(self, port, baudrate, **kw):
            tried.append(baudrate)
            if baudrate != 115200:
                raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
            self.is_open = True

    monkeypatch.setattr(audio_bridge.serial, "Serial", FakeSerial)
    bridge = SerialPcmAudioBridge("/tmp/sim7600-pcm", baudrate=921600)
    ser = bridge._open_serial()
    assert tried == [921600, 115200]
    assert ser.is_open is True


def test_serial_pcm_open_reraises_non_enotty(monkeypatch):
    """非 ENOTTY 的 OSError(如权限/设备不存在)不吞，照常上抛。"""
    import errno

    from agentcall.audio_bridge import SerialPcmAudioBridge

    def boom(port, baudrate, **kw):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(audio_bridge.serial, "Serial", boom)
    bridge = SerialPcmAudioBridge("/tmp/sim7600-pcm", baudrate=921600)
    with pytest.raises(OSError) as exc:
        bridge._open_serial()
    assert exc.value.errno == errno.EACCES


def test_serial_pcm_read_chunk_even_aligned_with_carry(monkeypatch):
    """串口读可能返回奇数字节；read_modem_chunk 须始终返回偶数（16-bit 对齐），
    奇出的 1 字节 carry 到下次——否则 np.frombuffer(int16) 崩掉整通。"""
    from agentcall.audio_bridge import SerialPcmAudioBridge

    bridge = SerialPcmAudioBridge("/tmp/sim7600-pcm")

    class FakeSer:
        def __init__(self):
            self._chunks = iter([b"\x01\x02\x03", b"\x04\x05", b""])

        def read(self, n):
            return next(self._chunks, b"")

    bridge._ser = FakeSer()
    r1 = bridge.read_modem_chunk()  # 3B -> 返回 2B，carry 1B
    r2 = bridge.read_modem_chunk()  # carry(1)+2 = 3B -> 返回 2B，carry 1B
    assert len(r1) % 2 == 0 and len(r2) % 2 == 0
    assert r1 == b"\x01\x02"
    assert r2 == b"\x03\x04"  # carry 保证不丢字节、不错位


def test_serial_pcm_discard_pending_output_clears_backlog():
    """barge-in 打断：一次性丢弃未播积压并返回字节数，缓冲清零。"""
    from agentcall.audio_bridge import SerialPcmAudioBridge

    bridge = SerialPcmAudioBridge("/tmp/sim7600-pcm")
    bridge._ser = object()  # write_modem_chunks 只要求非 None
    bridge.write_modem_chunks([b"\x00" * 3200, b"\x00" * 1600])
    assert bridge.pending_output_bytes() == 4800
    assert bridge.discard_pending_output() == 4800
    assert bridge.pending_output_bytes() == 0
    assert bridge.discard_pending_output() == 0  # 幂等


def test_uplink_backlog_logs_tty_before_read_and_buffer_separately(monkeypatch, caplog):
    bridge = audio_bridge.SerialPcmAudioBridge("fake-pcm")
    now = [0.0]
    monkeypatch.setattr(audio_bridge, "time", SimpleNamespace(monotonic=lambda: now[0]))
    caplog.set_level(logging.INFO)
    calls = []
    readings = iter(zip([640, 320, 0, 1022, 320, 0], [1, 4, 5, 6, 9, 10], strict=True))

    class Port:
        @property
        def in_waiting(self):
            self.waiting, self.at = next(readings)
            calls.append("sample")
            return self.waiting

        def read(self, count):
            calls.append("read")
            now[0] = self.at
            if self.at == 10:
                bridge._running = False
            return b"\x01\x00" * 160

    bridge._ser = Port()
    bridge._running = bridge._reader_alive = True
    bridge._read_loop()
    assert calls == ["sample", "read"] * 6
    messages = [r.message for r in caplog.records if "[timing] 上行积压" in r.message]
    assert len(messages) == 2  # 虚拟时钟跨过两个 5 秒窗口，不等待真实时间。
    pattern = (
        r"\[timing\] 上行积压: tty max=(\d+)B\(≈\d+ms\) 均值=(\d+)B \(n=(\d+)\)"
        r" \| 读线程缓冲 max=(\d+)B\(≈\d+ms\) 均值=(\d+)B \(n=(\d+)\)"
    )
    matches = [re.fullmatch(pattern, message) for message in messages]
    assert all(matches)
    assert [match.groups() for match in matches] == [
        ("640", "320", "3", "960", "640", "3"),
        ("1022", "447", "3", "1920", "1600", "3"),
    ]

    # 读线程已退出，故障回退路径只能统计 tty，不应把剩余进程缓冲混入日志。
    class DirectPort:
        in_waiting = 320

        def read(self, count):
            return b"\x02\x00"

    bridge._ser = DirectPort()
    bridge._rx_buffer.clear()
    now[0] = 15
    assert bridge.read_modem_chunk() == b"\x02\x00"
    assert re.fullmatch(
        r"\[timing\] 上行积压（直读）: tty max=320B\(≈\d+ms\) 均值=320B \(n=1\)",
        caplog.records[-1].message,
    )


@pytest.mark.parametrize("reader_alive", [False, True])
@pytest.mark.parametrize("fail_all", [False, True])
def test_uplink_backlog_sampling_failure_does_not_pollute_averages(
    monkeypatch, caplog, reader_alive, fail_all,
):
    bridge = audio_bridge.SerialPcmAudioBridge("fake-pcm")
    now = [0.0]
    monkeypatch.setattr(audio_bridge, "time", SimpleNamespace(monotonic=lambda: now[0]))
    caplog.set_level(logging.INFO)
    samples = iter([None, None if fail_all else 640])

    class Port:
        @property
        def in_waiting(self):
            value = next(samples)
            if value is None:
                raise OSError("fake tty sampling failure")
            return value

        def read(self, count):
            now[0] += 2.5
            if now[0] == 5:
                bridge._running = False
            return b"\x01\x00" * 160

    bridge._ser = Port()
    if reader_alive:
        bridge._reader_alive = bridge._running = True
        bridge._read_loop()
    else:
        # 直读在 read 前记录，第二次采样跨越日志窗口。
        bridge.read_modem_chunk()
        now[0] = 5
        bridge.read_modem_chunk()
    messages = [r.message for r in caplog.records if "[timing] 上行积压" in r.message]
    assert len(messages) == 1
    expected = ("-1", "-1", "0") if fail_all else ("640", "640", "1")
    match = re.search(r"tty max=(-?\d+)B\(≈-?\d+ms\) 均值=(-?\d+)B \(n=(\d+)\)", messages[0])
    assert match and match.groups() == expected
    assert ("读线程缓冲" in messages[0]) is reader_alive
    assert ("直读" in messages[0]) is not reader_alive
    if reader_alive:
        assert re.search(r"读线程缓冲 max=640B\(≈\d+ms\) 均值=480B \(n=2\)", messages[0])


def test_hangup_silence_stops_even_filler_and_restart_restores_writer(monkeypatch):
    from test_uplink_prebuffer import TtyPcmSource

    source = TtyPcmSource()
    bridge = audio_bridge.SerialPcmAudioBridge("fake-pcm", write_interval=0.002)
    monkeypatch.setattr(bridge, "_open_serial", lambda: source)
    writes = []
    observed = []
    written = threading.Event()
    played = threading.Event()
    cycles = threading.Event()
    checks = 0
    allow_write = False

    def ready():
        nonlocal checks
        checks += 1
        if checks >= 4:
            cycles.set()
        return allow_write

    def write(data):
        writes.append(data)
        written.set()
        return len(data)

    def observe(data):
        observed.append(data)
        played.set()

    bridge.set_ready_check(ready)
    bridge.set_output_observer(observe)
    monkeypatch.setattr(source, "write", write)
    bridge.start()
    try:
        bridge.write_modem_chunks([audio_bridge.AgentPcm(b"\x01\x00" * 1000)])
        assert bridge.silence_for_hangup() == 2000
        assert not bridge._output_spans
        assert bridge.silence_for_hangup() == 0
        bridge.write_modem_chunks([audio_bridge.AgentPcm(b"\x02\x00" * 320)])
        assert bridge.pending_output_bytes() == 0
        allow_write = True
        checks = 0
        cycles.clear()
        assert cycles.wait(1)
        assert writes == [] and observed == []
    finally:
        bridge.stop()
    source.is_open = True
    bridge.start()
    try:
        assert written.wait(1)
        assert writes[0] == bytes(bridge._write_size)
        bridge.write_modem_chunks([audio_bridge.AgentPcm(b"\x03\x00" * 256)])
        assert played.wait(1)
        bridge.silence_for_hangup()
        assert b"".join(observed) == b"\x03\x00" * 256
    finally:
        bridge.stop()


def test_hangup_waits_for_inflight_write_then_resets_output(monkeypatch):
    from test_uplink_prebuffer import TtyPcmSource

    source = TtyPcmSource()
    bridge = audio_bridge.SerialPcmAudioBridge("fake-pcm")
    monkeypatch.setattr(bridge, "_open_serial", lambda: source)
    entered = threading.Event()
    release = threading.Event()
    silenced = threading.Event()
    events = []

    def write(data):
        entered.set()
        assert release.wait(2)
        events.append("write")
        return len(data)

    def reset():
        events.append("reset")
        raise OSError("fake reset failure")

    monkeypatch.setattr(source, "write", write)
    bridge.start()
    assert entered.wait(2)
    monkeypatch.setattr(source, "reset_output_buffer", reset)
    bridge.write_modem_chunks([b"\x01\x00" * 1000])
    results = []

    def silence():
        results.append(bridge.silence_for_hangup())
        silenced.set()

    caller = threading.Thread(target=silence)
    try:
        caller.start()
        # 握手：挂断方已置位、正等在途写；不能用「20ms 内没完成」代替。
        deadline = time.monotonic() + 2
        while not bridge._hangup_silenced and time.monotonic() < deadline:
            time.sleep(0.001)
        assert bridge._hangup_silenced
        assert not silenced.is_set()
        release.set()
        assert silenced.wait(2)
        assert results == [2000]
        assert events == ["write", "reset"]
        assert bridge.pending_output_bytes() == 0
    finally:
        release.set()
        caller.join(2)
        bridge.stop()


# ---- 上行字节对齐自愈（真机 2026-08-12：整条上行恒偏移 1 字节）----

def _speech_like(n=20000, seed=7):
    """造一段相邻样本高度相关的「语音状」信号（真实语音 r>0.8）。"""
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.normal(0, 300, n))
    x = np.clip(x - np.convolve(x, np.ones(50) / 50, mode="same"), -8000, 8000)
    return (x * 3).astype(np.int16).tobytes()


def _feed(bridge, payload, chunk=640):
    out = bytearray()

    class _S:
        def __init__(self, data):
            self.data = data
            self.i = 0

        def read(self, n):
            piece = self.data[self.i:self.i + n]
            self.i += len(piece)
            return piece

    bridge._ser = _S(payload)
    for _ in range(len(payload) // chunk + 2):
        out += bridge.read_modem_chunk()
    return bytes(out)


def test_uplink_alignment_corrects_one_byte_offset():
    """流首多出 1 字节导致高低字节颠倒时，桥应自动丢 1 字节回到采样边界。"""
    from agentcall.audio_bridge import SerialPcmAudioBridge

    good = _speech_like()
    bridge = SerialPcmAudioBridge("/tmp/x")
    bridge._rx_carry = b""
    got = _feed(bridge, b"\x00" + good)      # 人为错位 1 字节
    assert bridge._align_locked
    tail = np.frombuffer(got[-16000:], dtype=np.int16).astype(float)
    r = float(np.corrcoef(tail[:-1], tail[1:])[0, 1])
    assert r > 0.8, f"纠正后应恢复语音相关性，实际 {r}"


def test_uplink_alignment_leaves_correct_stream_untouched():
    """本来就对齐的流不得被误改（否则反而把好数据弄坏）。"""
    from agentcall.audio_bridge import SerialPcmAudioBridge

    good = _speech_like()
    bridge = SerialPcmAudioBridge("/tmp/x")
    bridge._rx_carry = b""
    got = _feed(bridge, good)
    assert bridge._align_locked
    assert bridge._align_drop is False
    assert got[:200] == good[:200], "对齐正常时不应丢字节"


def test_uplink_alignment_waits_while_silent():
    """静音期不下判定（避免拿静音瞎判导致误纠正）。"""
    from agentcall.audio_bridge import SerialPcmAudioBridge

    bridge = SerialPcmAudioBridge("/tmp/x")
    bridge._rx_carry = b""
    _feed(bridge, b"\x00" * 40000)
    assert bridge._align_locked is False


@pytest.mark.parametrize("shifted", [False, True])
def test_uplink_alignment_locks_on_quiet_offset_within_quarter_second(shifted):
    from agentcall.audio_bridge import SerialPcmAudioBridge

    # 低幅背景噪声错位后成为大幅噪声；无需等来电者开口。
    quiet = np.random.default_rng(42).integers(-100, 101, size=2001, dtype=np.int16).tobytes()
    raw = (b"\x00" + quiet) if shifted else quiet
    bridge = SerialPcmAudioBridge("/fake-pcm")
    bridge._probe_alignment(raw[:3998])
    assert not bridge._align_locked
    bridge._probe_alignment(raw[3998:4000])
    assert bridge._align_locked
    assert bridge._align_drop is shifted


def test_uplink_alignment_reprobes_after_midstream_slip():
    """锁定后中途丢 1 字节（劣化模组丢字节）：监控应发现相关性塌陷并重探纠正。"""
    from agentcall.audio_bridge import SerialPcmAudioBridge

    good = _speech_like(n=60000)
    half = len(good) // 2
    # 前半正常 → 锁定「对齐」；中途丢 1 字节 → 后半整体错位。
    payload = good[:half] + good[half + 1:]
    bridge = SerialPcmAudioBridge("/tmp/x")
    bridge._rx_carry = b""
    got = _feed(bridge, payload)
    # 监控应已解锁重探，且重探判定错位（丢 1 字节归位后重新锁定）。
    tail = np.frombuffer(got[-8000:], dtype=np.int16).astype(float)
    r = float(np.corrcoef(tail[:-1], tail[1:])[0, 1])
    assert r > 0.8, f"中途错位应被复核纠正，尾段相关性实际 {r}"


def test_uplink_alignment_ambiguous_evidence_does_not_lock():
    """两个偏移的相关性都含糊（分不出胜负）时不得锁定——继续攒证据。

    真机教训（2026-08-12）：对齐0=0.815 / 偏移1=0.844 的含糊样本被锁成
    「对齐正常」，整通乱码没人管。
    """
    from agentcall.audio_bridge import SerialPcmAudioBridge

    bridge = SerialPcmAudioBridge("/tmp/x")
    bridge._rx_carry = b""
    # 恒定直流方波：两个偏移读出来相关性都高且接近 → 证据含糊。
    ambiguous = (b"\x10\x10" * 20000)
    _feed(bridge, ambiguous)
    assert bridge._align_locked is False


def test_played_counts_only_successful_partial_writes_not_discards():
    from agentcall.audio_bridge import AgentPcm, _OutputObserver

    bridge = _OutputObserver()
    bridge.set_output_observer(lambda _pcm: None)
    played = []
    pcm = AgentPcm(b"\x01\x02" * 320)
    pcm.on_played = played.append
    bridge._queue_output_span(pcm)
    first = bridge._take_output_ranges(160)
    assert not played
    bridge._notify_output(pcm[:160], first)
    assert played == [160]
    bridge._take_output_ranges(480)  # Discarded, not written.
    assert played == [160]
