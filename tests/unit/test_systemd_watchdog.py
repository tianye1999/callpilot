"""Watchdog notifications must depend on progress of the main event loop."""

import asyncio
import socket
import sys
import time

import pytest

from agentcall.systemd_watchdog import SystemdWatchdog


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix datagram sockets unavailable")
    # macOS limits Unix socket paths to 104 bytes.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="wd-", dir="/tmp") as directory:
        path = directory + "/notify"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind(path)
            sock.setblocking(False)
            monkeypatch.setenv("NOTIFY_SOCKET", path)
            monkeypatch.setenv("WATCHDOG_USEC", "60000")
            monkeypatch.delenv("WATCHDOG_PID", raising=False)
            yield sock


def test_ready_periodic_heartbeat_stops_with_loop_and_cleanup(receiver):
    loop = asyncio.new_event_loop()
    watchdog = SystemdWatchdog(loop)
    try:
        watchdog.start()
        assert receiver.recv(128) == b"READY=1"
        loop.run_until_complete(asyncio.sleep(0.05))
        messages = []
        while True:
            try:
                messages.append(receiver.recv(128))
            except BlockingIOError:
                break
        assert len(messages) >= 2 and set(messages) == {b"WATCHDOG=1"}
        # The watchdog deadline passes, but the event loop cannot run callbacks.
        time.sleep(0.05)
        with pytest.raises(BlockingIOError):
            receiver.recv(128)
        loop.run_until_complete(asyncio.sleep(0))
        assert receiver.recv(128) == b"WATCHDOG=1"
        watchdog.stop()
        loop.run_until_complete(asyncio.sleep(0.05))
        with pytest.raises(BlockingIOError):
            receiver.recv(128)
    finally:
        watchdog.stop()
        loop.close()


def test_no_notify_socket_sends_nothing(receiver, monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET")
    loop = asyncio.new_event_loop()
    watchdog = SystemdWatchdog(loop)
    try:
        watchdog.start()
        loop.run_until_complete(asyncio.sleep(0.03))
        with pytest.raises(BlockingIOError):
            receiver.recv(128)
    finally:
        watchdog.stop()
        loop.close()


@pytest.mark.parametrize("key,value", [
    ("WATCHDOG_USEC", None), ("WATCHDOG_USEC", "0"),
    ("WATCHDOG_USEC", "bad"), ("WATCHDOG_PID", "99999999"),
])
def test_ready_without_heartbeat_when_watchdog_unusable(receiver, monkeypatch, key, value):
    """Type=notify always needs READY=1; only the heartbeat depends on a usable WatchdogSec."""
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    loop = asyncio.new_event_loop()
    watchdog = SystemdWatchdog(loop)
    try:
        watchdog.start()
        assert receiver.recv(128) == b"READY=1"
        loop.run_until_complete(asyncio.sleep(0.03))
        with pytest.raises(BlockingIOError):
            receiver.recv(128)
        assert watchdog._handle is None
    finally:
        watchdog.stop()
        loop.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux abstract namespace")
def test_abstract_notify_socket(monkeypatch):
    import os

    name = f"callpilot-test-{os.getpid()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind("\0" + name)
        sock.settimeout(1)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
        monkeypatch.setenv("WATCHDOG_USEC", "60000000")
        monkeypatch.delenv("WATCHDOG_PID", raising=False)
        loop = asyncio.new_event_loop()
        watchdog = SystemdWatchdog(loop)
        try:
            watchdog.start()
            assert sock.recv(128) == b"READY=1"
        finally:
            watchdog.stop()
            loop.close()
