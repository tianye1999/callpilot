"""CVSD SCO bridge. The controller, not a software timer, clocks PCM output."""

from __future__ import annotations

import logging
import os
import select
import threading
import time
from collections.abc import Iterable
from queue import Empty, Full, Queue
from typing import Any, Callable

from .audio_bridge import ModemAudioBridge, _OutputObserver, apply_pcm_gain

logger = logging.getLogger(__name__)


def authorize_sco(
    fd: int, *, poller: Any = None, read: Callable[[int, int], bytes] = os.read,
) -> None:
    """BT_DEFER_SETUP: authorize only when needed, then require POLLOUT."""
    poller = poller if poller is not None else getattr(select, "poll")()
    # Numeric constants also let policy tests run on Windows (no select.poll).
    writable, errors = 4, 8 | 16 | 32  # POLLOUT; POLLERR | POLLHUP | POLLNVAL
    poller.register(fd, writable | errors)
    events = poller.poll(0)
    if not events:
        read(fd, 1)
        events = poller.poll(5000)
    mask = events[0][1] if events else 0
    if mask & errors or not mask & writable:
        raise OSError("SCO deferred setup failed or timed out")


class BluetoothAudioBridge(_OutputObserver):
    """8 kHz s16le PCM with bounded queues and per-packet silence padding."""

    modem_to_agent = staticmethod(ModemAudioBridge.modem_to_agent)
    agent_to_modem = staticmethod(ModemAudioBridge.agent_to_modem)
    thread_name = "hfp-sco"
    _WRITE_EAGAIN_LOG_INTERVAL_SECONDS = 5.0

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._thread: threading.Thread | None = None
        self._downlink: Queue[bytes] = Queue(maxsize=512)
        self._output = bytearray()
        self.packet_count = 0
        self._read_eagain_count = 0
        self._write_eagain_count = 0
        self._last_write_eagain_log_at: float | None = None
        self.tx_gain = 1.0
        self.last_packet_at = 0.0

    def start(self) -> None:
        # The modem may receive SCO before CallSession finishes provider startup.
        pass

    def accept_fd(self, fd: int, codec: int) -> bool:
        with self._lifecycle:
            if codec != 1 or self._stop.is_set() or (self._thread and self._thread.is_alive()):
                os.close(fd)
                return False
            self._thread = threading.Thread(target=self._pump, args=(fd,), name=self.thread_name, daemon=True)
            try:
                self._thread.start()
            except BaseException:
                os.close(fd)
                raise
            return True

    def _pump(self, fd: int) -> None:
        try:
            os.set_blocking(fd, False)
            authorize_sco(fd)
            while not self._stop.is_set():
                if not select.select([fd], [], [], 0.1)[0]:
                    continue
                try:
                    packet = os.read(fd, 1024)
                except BlockingIOError:
                    self._read_eagain_count += 1
                    continue
                if not packet:
                    break
                self.packet_count += 1
                self.last_packet_at = time.monotonic()
                try:
                    self._downlink.put_nowait(packet)
                except Full:
                    try:
                        self._downlink.get_nowait()
                    except Empty:
                        pass
                    self._downlink.put_nowait(packet)
                with self._lock:
                    count = min(len(packet), len(self._output))
                    payload = bytes(self._output[:count]) + bytes(len(packet) - count)
                    # Exactly one write for every read; partial writes are a link failure.
                    try:
                        written = os.write(fd, payload)
                    except BlockingIOError:
                        # Drop this clock slot, including source spans, rather than
                        # replaying stale audio on a later downlink packet.
                        del self._output[:count]
                        self._take_output_ranges(count)
                        self._write_eagain_count += 1
                        self._log_write_eagain()
                        continue
                    if written != len(payload):
                        raise OSError("Short SCO packet write")
                    del self._output[:count]
                    ranges = self._take_output_ranges(count)
                    self._note_output_progress(count)
                self._notify_output(payload, ranges)
        except OSError:
            if not self._stop.is_set():
                logger.warning("SCO unavailable; waiting for audio reconnect", exc_info=True)
        finally:
            # Only the pump owns this fd. stop() joins it before returning.
            os.close(fd)

    def _log_write_eagain(self) -> None:
        now = time.monotonic()
        if (self._last_write_eagain_log_at is not None
                and now - self._last_write_eagain_log_at < self._WRITE_EAGAIN_LOG_INTERVAL_SECONDS):
            return
        self._last_write_eagain_log_at = now
        logger.warning(
            "SCO uplink packet dropped (EAGAIN): write_eagain=%d read_eagain=%d",
            self._write_eagain_count, self._read_eagain_count,
        )

    def stop(self) -> None:
        with self._lifecycle:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=6)
                if self._thread.is_alive():
                    raise RuntimeError("SCO worker did not stop")
            self.discard_pending_output()

    def read_modem_chunk(self) -> bytes:
        try:
            chunks = [self._downlink.get(timeout=0.05)]
        except Empty:
            return b""
        # CallSession sleeps 10 ms per iteration; a single 3.75 ms SCO packet
        # per read would lose most of the speech. Drain the already-clocked PCM.
        count = len(chunks[0])
        while count < 3200:
            try:
                packet = self._downlink.get_nowait()
            except Empty:
                break
            chunks.append(packet)
            count += len(packet)
        return b"".join(chunks)

    def amplify_for_modem(self, pcm_8k: bytes) -> bytes:
        return apply_pcm_gain(pcm_8k, self.tx_gain)

    def receiving_audio(self, now: float) -> bool:
        return bool(self.last_packet_at and now - self.last_packet_at < 4)

    def write_modem_chunks(self, chunks: Iterable[bytes]) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            for chunk in chunks:
                if chunk:
                    self._output.extend(chunk)
                    self._queue_output_span(chunk)
            if len(self._output) > 16000 * 30:
                dropped = len(self._output) - 16000 * 30
                del self._output[:dropped]
                self._take_output_ranges(dropped)

    def pending_output_bytes(self) -> int:
        with self._lock:
            return len(self._output)

    def discard_pending_output(self) -> int:
        with self._lock:
            count = len(self._output)
            self._output.clear()
            self._take_output_ranges(count)
            return count
