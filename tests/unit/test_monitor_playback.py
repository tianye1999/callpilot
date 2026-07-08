"""MonitorPlayback 监听旁路单测（不真起 ffmpeg / 不碰 CoreAudio）。"""

from __future__ import annotations

import logging
import threading
import time

import pytest

import agentcall.coreaudio as coreaudio
import agentcall.monitor_playback as monitor_playback
from agentcall.audio_bridge import apply_pcm_gain
from agentcall.monitor_playback import MonitorPlayback, create_monitor_playback


class FakeStdin:
    """记录写入内容的假 stdin；可选阻塞以模拟 ffmpeg 消费慢。"""

    def __init__(self, block: bool = False) -> None:
        self.data = bytearray()
        self.closed = False
        self.wrote = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()

    def write(self, payload: bytes) -> int:
        self.release.wait(timeout=5)
        self.data.extend(payload)
        self.wrote.set()
        return len(payload)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakePopen:
    """假 ffmpeg 播放子进程。"""

    instances: list["FakePopen"] = []

    def __init__(self, cmd, **kwargs) -> None:
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.stdin = FakeStdin(block=getattr(FakePopen, "block_stdin", False))
        self.returncode: int | None = None
        FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout=None) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    """默认夹具：视作 macOS、设备可找到(序号 3)、Popen 被替换、每测重置实例记录。

    IS_MACOS 固定为 True 让主体用例在任意 CI 平台上行为一致；
    非 macOS 降级路径由专门用例显式改回 False 覆盖。
    """
    FakePopen.instances = []
    FakePopen.block_stdin = False
    monkeypatch.setattr(monitor_playback.platforms, "IS_MACOS", True)
    monkeypatch.setattr(coreaudio, "find_output_index", lambda keyword: 3)
    monkeypatch.setattr(monitor_playback.subprocess, "Popen", FakePopen)
    yield


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def make_started(**kwargs) -> MonitorPlayback:
    mp = MonitorPlayback("MacBook", **kwargs)
    mp.start()
    assert mp.active
    return mp


# ---- start：优雅禁用 ----


def test_start_disabled_when_device_not_found(monkeypatch):
    monkeypatch.setattr(coreaudio, "find_output_index", lambda keyword: None)
    mp = MonitorPlayback("NoSuchDevice")

    mp.start()  # 不应抛异常

    assert not mp.active
    assert FakePopen.instances == []  # 没起子进程
    mp.feed(b"\x01\x02")  # 禁用态 feed 也安全
    mp.stop()


def test_start_disabled_when_device_enum_raises(monkeypatch):
    def boom(keyword):
        raise OSError("coreaudiod down")

    monkeypatch.setattr(coreaudio, "find_output_index", boom)
    mp = MonitorPlayback("MacBook")

    mp.start()

    assert not mp.active
    assert FakePopen.instances == []


def test_start_degrades_to_noop_on_non_macos(monkeypatch, caplog):
    """非 macOS：start 只告警不抛，且不触碰 CoreAudio / 不起子进程。"""
    monkeypatch.setattr(monitor_playback.platforms, "IS_MACOS", False)
    monkeypatch.setattr(
        coreaudio,
        "find_output_index",
        lambda keyword: pytest.fail("非 macOS 不应触碰 CoreAudio"),
    )
    mp = MonitorPlayback("MacBook")

    with caplog.at_level(logging.WARNING):
        mp.start()

    assert not mp.active
    assert FakePopen.instances == []
    assert any("暂不支持" in rec.message for rec in caplog.records)
    mp.feed(b"\x01\x02")  # no-op 实例 feed/stop 全程安全
    mp.stop()
    mp.stop()


def test_start_disabled_when_popen_fails(monkeypatch):
    def no_ffmpeg(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg not installed")

    monkeypatch.setattr(monitor_playback.subprocess, "Popen", no_ffmpeg)
    mp = MonitorPlayback("MacBook")

    mp.start()

    assert not mp.active
    mp.stop()


def test_start_builds_expected_ffmpeg_command():
    mp = make_started(sample_rate=24000)
    cmd = FakePopen.instances[0].cmd

    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-ar") + 1] == "24000"
    assert "-f" in cmd and "audiotoolbox" in cmd
    assert cmd[cmd.index("-audio_device_index") + 1] == "3"
    assert cmd[cmd.index("-i") + 1] == "pipe:0"
    mp.stop()


def test_empty_keyword_omits_device_index(monkeypatch):
    """未指定设备名（空）→ 不传 -audio_device_index，交给系统默认输出（最稳）。"""
    called = {"find": 0}

    def fake_find(keyword):
        called["find"] += 1
        return 3

    monkeypatch.setattr(coreaudio, "find_output_index", fake_find)
    mp = MonitorPlayback("")  # 空关键字
    mp.start()
    assert mp.active
    assert called["find"] == 0  # 不做按名解析
    cmd = FakePopen.instances[-1].cmd
    assert "-audio_device_index" not in cmd  # 无序号 = ffmpeg 用默认输出
    assert "audiotoolbox" in cmd
    mp.stop()


# ---- feed：非阻塞 + 丢帧 ----


def test_feed_never_blocks_when_queue_full(monkeypatch):
    monkeypatch.setenv("MONITOR_PLAYBACK_QUEUE_MAXLEN", "8")
    FakePopen.block_stdin = True  # 喂养线程卡在 write，队列必然涨满
    mp = make_started()
    stdin = FakePopen.instances[0].stdin

    frame = b"\x01\x00" * 160
    started_at = time.monotonic()
    for _ in range(200):
        mp.feed(frame)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5  # 200 次高频 feed 全程不卡
    assert mp.dropped_frames >= 180  # 队列 8 帧 + 在写 1 帧，其余全部丢弃
    stdin.release.set()  # 放行喂养线程，让 stop 快速收尾
    mp.stop()
    assert not mp.active


# ---- gain ----


def test_gain_applied_before_write():
    pcm = (1000).to_bytes(2, "little", signed=True) * 10
    mp = make_started(gain=2.0)
    stdin = FakePopen.instances[0].stdin

    mp.feed(pcm)

    assert stdin.wrote.wait(timeout=2)
    assert bytes(stdin.data) == apply_pcm_gain(pcm, 2.0)
    assert bytes(stdin.data) != pcm  # 确认增益确实生效
    mp.stop()


def test_gain_one_passthrough():
    pcm = b"\x34\x12" * 8
    mp = make_started(gain=1.0)
    stdin = FakePopen.instances[0].stdin

    mp.feed(pcm)

    assert stdin.wrote.wait(timeout=2)
    assert bytes(stdin.data) == pcm
    mp.stop()


# ---- ffmpeg 死亡 → 自动禁用 ----


def test_auto_disable_when_ffmpeg_dies():
    mp = make_started()
    proc = FakePopen.instances[0]

    proc.returncode = 1  # 模拟 ffmpeg 已退出
    mp.feed(b"\x01\x00" * 16)

    assert wait_until(lambda: not mp.active)
    mp.feed(b"\x01\x00" * 16)  # 禁用后继续 feed 不抛
    mp.stop()


def test_auto_disable_when_pipe_breaks(monkeypatch):
    mp = make_started()
    stdin = FakePopen.instances[0].stdin

    def broken(payload):
        raise BrokenPipeError("pipe closed")

    monkeypatch.setattr(stdin, "write", broken)
    mp.feed(b"\x01\x00" * 16)

    assert wait_until(lambda: not mp.active)
    mp.stop()


# ---- stop：幂等 ----


def test_stop_idempotent():
    mp = make_started()

    mp.stop()
    mp.stop()
    mp.stop()

    assert not mp.active
    assert FakePopen.instances[0].returncode is not None  # 子进程已被回收


def test_stop_before_start_is_safe():
    mp = MonitorPlayback("MacBook")
    mp.stop()
    mp.stop()
    assert not mp.active


# ---- 工厂：环境变量 ----


def test_factory_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MONITOR_AI_PLAYBACK", raising=False)
    assert create_monitor_playback() is None


def test_factory_reads_env(monkeypatch):
    monkeypatch.setenv("MONITOR_AI_PLAYBACK", "1")
    monkeypatch.setenv("MONITOR_OUTPUT_DEVICE", "Speakers")
    monkeypatch.setenv("MONITOR_PLAYBACK_RATE", "16000")
    monkeypatch.setenv("MONITOR_AI_GAIN", "0.5")

    mp = create_monitor_playback()

    assert mp is not None
    assert mp.device_keyword == "Speakers"
    assert mp.sample_rate == 16000
    assert mp.gain == 0.5
