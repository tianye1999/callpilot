"""Dependency-free systemd notifications driven only by the service event loop."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys

logger = logging.getLogger(__name__)


class SystemdWatchdog:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._address = ""
        self._interval = 20.0
        self._handle: asyncio.TimerHandle | None = None

    def start(self) -> None:
        """Call only after HTTP listening and application startup are complete."""
        self.stop()
        address = os.environ.get("NOTIFY_SOCKET", "")
        if not address:
            return
        self._address = "\0" + address[1:] if address.startswith("@") else address
        # Type=notify waits for READY=1 whether or not WatchdogSec is set; never withhold it.
        self._notify(b"READY=1")
        usec = os.environ.get("WATCHDOG_USEC", "")
        if not usec:
            return
        try:
            timeout = int(usec)
            pid = int(os.environ.get("WATCHDOG_PID", str(os.getpid())))
        except ValueError:
            logger.warning("Invalid systemd watchdog environment; heartbeat disabled")
            return
        if timeout <= 0 or pid != os.getpid():
            return
        self._interval = min(20.0, timeout / 3_000_000)
        self._handle = self._loop.call_later(self._interval, self._heartbeat)

    def _notify(self, message: bytes) -> None:
        if sys.platform == "win32":
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                # A full supervisor receive queue must not hang the event loop.
                sock.setblocking(False)
                sock.sendto(message, self._address)
        except OSError as exc:
            logger.warning("Systemd notification failed: %s", exc)

    def _heartbeat(self) -> None:
        self._notify(b"WATCHDOG=1")
        self._handle = self._loop.call_later(self._interval, self._heartbeat)

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
