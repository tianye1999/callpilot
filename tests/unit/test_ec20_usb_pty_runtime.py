"""Runtime helper tests for the bundled EC20 USB bridge."""

from __future__ import annotations

import pytest

pytest.importorskip("fcntl", reason="EC20 PTY bridge is POSIX-only")

import usb.core

from scripts import ec20_usb_pty


def test_bundled_libusb_path_uses_pyinstaller_resources(tmp_path, monkeypatch):
    lib = tmp_path / "lib" / "libusb-1.0.0.dylib"
    lib.parent.mkdir()
    lib.write_bytes(b"placeholder")

    monkeypatch.setattr(ec20_usb_pty.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert ec20_usb_pty.bundled_libusb_path() == lib


def test_bundled_libusb_path_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ec20_usb_pty.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert ec20_usb_pty.bundled_libusb_path() is None


# ---- USB 写超时容忍（真机 2026-08-01：一次超时把 AT 口的桥一起拆了，通话中断）----


class _FakeDev:
    """只实现 bridge_port 用到的 USB 调用面。"""

    def __init__(self, write_outcomes):
        self.write_outcomes = list(write_outcomes)
        self.writes = 0

    def write(self, endpoint, data, timeout=None):
        self.writes += 1
        outcome = (
            self.write_outcomes.pop(0) if self.write_outcomes else None
        )
        if outcome is not None:
            raise outcome
        return len(data)


def _drive_writes(monkeypatch, outcomes):
    """跑 pty_to_usb 的写分支逻辑，返回 (写次数, 是否判定链路已死)。

    直接复刻脚本里的容忍规则，避免为测一个分支去起真 PTY / 真 USB。
    """
    tolerance = ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE
    dev = _FakeDev(outcomes)
    consecutive = 0
    died = False
    for _ in range(len(outcomes)):
        try:
            dev.write(0x05, b"\x00" * 320)
            consecutive = 0
        except usb.core.USBTimeoutError:
            consecutive += 1
            if consecutive >= tolerance:
                died = True
                break
    return dev.writes, died


def test_single_write_timeout_is_not_fatal(monkeypatch):
    """丢一帧音频远好过拆掉整座桥。"""
    timeout = usb.core.USBTimeoutError("timed out", None, None)
    writes, died = _drive_writes(monkeypatch, [timeout, None, None])
    assert writes == 3 and died is False


def test_consecutive_timeouts_reset_on_success(monkeypatch):
    """中间成功一次就该清零，否则长通话里零星超时会累积到误杀。"""
    t = usb.core.USBTimeoutError("timed out", None, None)
    outcomes = []
    for _ in range(6):
        outcomes += [t, t, None]        # 每两次超时后成功一次
    writes, died = _drive_writes(monkeypatch, outcomes)
    assert died is False and writes == len(outcomes)


def test_sustained_timeouts_eventually_declare_link_dead(monkeypatch):
    """真的一直写不进去，还是要停——否则死链路上会无限空转。"""
    t = usb.core.USBTimeoutError("timed out", None, None)
    outcomes = [t] * (ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE + 5)
    writes, died = _drive_writes(monkeypatch, outcomes)
    assert died is True
    assert writes == ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE
