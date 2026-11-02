"""Bounded, content-free observations for one authenticated media endpoint."""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Callable, Protocol
from uuid import UUID

CLIENT_MARKS = frozenset({
    "answer_action", "dial_start", "claim_start", "claim_response", "configure_start",
    "configure_end", "fulfill", "did_activate", "activate_return", "connect_start",
    "connect_return", "room_connected", "audio_activated", "mic_enable_start",
    "mic_enable_return", "downlink_subscribed", "downlink_first_frame", "edge_connected",
    "hangup_intent", "hangup_publish_start", "hangup_publish_end", "hangup_publish_error",
    "hangup_retry_start", "hangup_retry_end", "hangup_retry_error", "hangup_ack", "disconnect",
    "claim_dns_start", "claim_dns_end", "claim_connect_start", "claim_connect_end",
    "claim_tls_start", "claim_tls_end", "claim_request_start", "claim_first_byte",
    "claim_connection_reused", "prewarm_start", "prewarm_end",
    "push_received", "prepare_start", "prepare_response", "preconnect_start", "preconnect_ready",
    "permission_ready", "transport_adopted", "cold_fallback", "claim_response_end",
})
MAX_TIMING_BYTES = 2048
MAX_TIMING_BATCH_MARKS = 16
MAX_TIMING_BATCHES = 64
MAX_TIMING_MARKS = 96
HANGUP_ACK_TIMEOUT = 0.25


class TimingRecord(Protocol):
    def log_event(self, type: str, **fields: Any) -> None: ...  # noqa: A002


def valid_command_id(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


class CallTiming:
    def __init__(self, record: TimingRecord | None = None, *,
                 clock: Callable[[], float] = time.monotonic, origin: float | None = None) -> None:
        self._clock = clock
        self._origin = clock() if origin is None else origin
        self._record = record
        self._marks: dict[str, float] = {}
        self._pending: list[tuple[str, float]] = []
        self._attempt: str | None = None
        self._seq = 0
        self.hangup_received = False

    def bind(self, record: TimingRecord | None) -> None:
        self._record = record
        if record is not None:
            for stage, ms in self._pending:
                record.log_event("latency", stage=stage, ms=ms)
            self._pending.clear()

    def _emit(self, stage: str, ms: float) -> None:
        if stage in self._marks or len(self._marks) >= MAX_TIMING_MARKS:
            return
        self._marks[stage] = ms
        if self._record is None:
            self._pending.append((stage, ms))
        else:
            self._record.log_event("latency", stage=stage, ms=ms)

    def mark(self, stage: str) -> None:
        self._emit("edge_" + stage, round((self._clock() - self._origin) * 1000, 3))

    def include_preconnect(self, other: CallTiming) -> None:
        for stage, ms in other._marks.items():
            self._emit(stage, round(ms + (other._origin - self._origin) * 1000, 3))

    def receive_client(self, command: dict[str, Any]) -> None:
        if set(command) != {"type", "v", "attempt", "seq", "marks"}:
            return
        if command["type"] != "client_timing" or type(command["v"]) is not int or command["v"] != 1:
            return
        attempt, seq, marks = command["attempt"], command["seq"], command["marks"]
        if (not valid_command_id(attempt) or type(seq) is not int or not 1 <= seq <= MAX_TIMING_BATCHES
                or seq <= self._seq or (self._attempt is not None and attempt != self._attempt)):
            return
        if not isinstance(marks, dict) or not 1 <= len(marks) <= MAX_TIMING_BATCH_MARKS:
            return
        for name, value in marks.items():
            if (not isinstance(name, str) or type(value) not in (int, float)
                    or not -60_000 <= value <= 86_400_000 or not math.isfinite(value)):
                return
        try:
            if len(json.dumps(command, separators=(",", ":")).encode()) > MAX_TIMING_BYTES:
                return
        except (TypeError, ValueError):
            return
        self._attempt, self._seq = attempt, seq
        for name, value in marks.items():
            if name in CLIENT_MARKS:
                self._emit("client_" + name, value)

    async def acknowledge_hangup(self, command: dict[str, Any],
                                 send: Callable[[dict[str, Any]], Any]) -> bool:
        """ACK before teardown; a broken data channel must not hold the modem open."""
        first = not self.hangup_received
        self.hangup_received = True
        self.mark("hangup_received")
        command_id = command.get("commandId")
        if valid_command_id(command_id):
            try:
                await asyncio.wait_for(send({"type": "hangup.ack", "commandId": command_id}),
                                       HANGUP_ACK_TIMEOUT)
                self.mark("hangup_ack_sent")
            except Exception:
                self.mark("hangup_ack_failed")
        return first


def attach_endpoint_timing(endpoint: Any, timing: CallTiming) -> None:
    setter = getattr(endpoint, "set_timing", None)
    if callable(setter):
        setter(timing)
