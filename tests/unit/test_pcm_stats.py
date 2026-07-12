"""PcmFlowStats：音频链路周期统计（隐私安全，只记数字）。"""

from __future__ import annotations

import logging
import struct

from agentcall.pcm_stats import PcmFlowStats


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def make_stats(interval: float = 5.0) -> tuple[PcmFlowStats, FakeClock]:
    clock = FakeClock()
    return PcmFlowStats("test_seg", interval_seconds=interval, clock=clock), clock


def pcm_of(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_no_log_before_interval(caplog):
    stats, clock = make_stats()
    stats.add(pcm_of(1, 2, 3))
    clock.now += 4.9

    assert stats.due() is False
    with caplog.at_level(logging.INFO, logger="agentcall.pcm_stats"):
        assert stats.maybe_log() is False
    assert not caplog.records

    clock.now += 0.1
    assert stats.due() is True


def test_logs_frames_bytes_peak_and_resets(caplog):
    stats, clock = make_stats()
    stats.add(pcm_of(100, -32768, 5))
    stats.add(pcm_of(7))
    clock.now += 5.0

    with caplog.at_level(logging.INFO, logger="agentcall.pcm_stats"):
        assert stats.maybe_log(queued=3) is True
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "test_seg" in message
    assert "frames=2" in message
    assert "bytes=8" in message
    assert "peak=32768" in message
    assert "queued=3" in message

    # 复位后新窗口从零累计。
    caplog.clear()
    clock.now += 5.0
    with caplog.at_level(logging.INFO, logger="agentcall.pcm_stats"):
        assert stats.maybe_log() is True
    assert "frames=0 bytes=0 peak=0" in caplog.records[0].getMessage()


def test_zero_frame_window_still_logs(caplog):
    """整窗一帧未到也要按期打 frames=0——断流定位靠这个信号。"""
    stats, clock = make_stats()
    clock.now += 6.0

    with caplog.at_level(logging.INFO, logger="agentcall.pcm_stats"):
        assert stats.maybe_log() is True
    assert "frames=0 bytes=0 peak=0" in caplog.records[0].getMessage()


def test_all_zero_pcm_keeps_peak_zero(caplog):
    """全零 PCM 帧数照记、峰值保持 0——区分「无帧」与「有帧但全零」。"""
    stats, clock = make_stats()
    stats.add(b"\x00" * 640)
    clock.now += 5.0

    with caplog.at_level(logging.INFO, logger="agentcall.pcm_stats"):
        assert stats.maybe_log() is True
    message = caplog.records[0].getMessage()
    assert "frames=1" in message
    assert "peak=0" in message


def test_odd_length_pcm_does_not_crash():
    stats, clock = make_stats()
    stats.add(b"\x01")  # 半个采样：字节照计，峰值跳过
    clock.now += 5.0
    assert stats.maybe_log() is True
