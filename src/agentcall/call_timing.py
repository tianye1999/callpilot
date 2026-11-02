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
    "audio_lease_start", "audio_lease_end",
    "sdp_submit_start", "sdp_submit_response",
    "cloud_cas_ms", "cloud_turn_ms", "cloud_sdp_relay_ms", "cloud_claim_finalize_ms", "cloud_total_ms",
    "cloud_edge_reserve_ms",
    "audio_engine_running", "audio_engine_failed", "direct_control_ready",
})
MAX_TIMING_BYTES = 2048
MAX_TIMING_BATCH_MARKS = 16
MAX_TIMING_BATCHES = 64
MAX_TIMING_MARKS = 96
HANGUP_ACK_TIMEOUT = 0.25
MAX_HTTP_TIMING_BATCHES = 8
MAX_HTTP_TIMING_BYTES = 4096
_ENGINE_FLAGS = frozenset({"running", "recording", "playing", "inputEnabled", "inputRunning",
                           "outputEnabled", "outputRunning", "inputMuted"})


class TimingRecord(Protocol):
    def log_event(self, type: str, **fields: Any) -> None: ...  # noqa: A002


def valid_command_id(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def valid_client_timing(command: Any) -> bool:
    """Validate a bounded telemetry envelope without advancing its sequence."""
    if not isinstance(command, dict) or set(command) - {"audioEngine"} != {"type", "v", "attempt", "seq", "marks"}:
        return False
    if command["type"] != "client_timing" or type(command["v"]) is not int or command["v"] != 1:
        return False
    if (not valid_command_id(command["attempt"]) or type(command["seq"]) is not int
            or not 1 <= command["seq"] <= MAX_TIMING_BATCHES):
        return False
    marks = command["marks"]
    if not isinstance(marks, dict) or not 1 <= len(marks) <= MAX_TIMING_BATCH_MARKS:
        return False
    for name, value in marks.items():
        if (not isinstance(name, str) or type(value) not in (int, float)
                or not -60_000 <= value <= 86_400_000 or not math.isfinite(value)):
            return False
    if "audioEngine" in command:
        engine = command["audioEngine"]
        if (not isinstance(engine, dict) or set(engine) != _ENGINE_FLAGS | {"muteMode"}
                or any(type(engine[key]) is not bool for key in _ENGINE_FLAGS)
                or type(engine["muteMode"]) is not int or not -1 <= engine["muteMode"] <= 2):
            return False
    try:
        return len(json.dumps(command, separators=(",", ":")).encode()) <= MAX_TIMING_BYTES
    except (TypeError, ValueError):
        return False


def valid_http_client_timing(batches: Any) -> bool:
    return (isinstance(batches, list) and len(batches) <= MAX_HTTP_TIMING_BATCHES
            and all(valid_client_timing(batch) and set(batch["marks"]) <= CLIENT_MARKS for batch in batches)
            and len(json.dumps(batches, separators=(",", ":")).encode()) <= MAX_HTTP_TIMING_BYTES)


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
        self._pending_engines: list[dict[str, Any]] = []
        self.hangup_received = False

    def bind(self, record: TimingRecord | None) -> None:
        self._record = record
        if record is not None:
            for stage, ms in self._pending:
                record.log_event("latency", stage=stage, ms=ms)
            self._pending.clear()
            for observation in self._pending_engines:
                record.log_event("client_audio_engine_state", **observation)
            self._pending_engines.clear()

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

    def summary(self) -> dict[str, float | None]:
        """Durations only within one clock domain; a renderer frame may be silent."""
        intervals = {
            "client_answer_to_claim_ms": ("client_answer_action", "client_claim_start"),
            "client_claim_http_ms": ("client_claim_start", "client_claim_response"),
            "client_sdp_http_ms": ("client_sdp_submit_start", "client_sdp_submit_response"),
            "client_answer_to_track_frame_ms": ("client_answer_action", "client_downlink_first_frame"),
            "client_connect_ms": ("client_connect_start", "client_connect_return"),
            "client_microphone_ms": ("client_mic_enable_start", "client_mic_enable_return"),
            "edge_claim_to_pcm_ms": ("edge_claim_received", "edge_pcm_ready"),
            "edge_claim_to_modem_frame_ms": ("edge_claim_received", "edge_first_modem_to_phone"),
            "edge_join_to_subscription_ms": ("edge_phone_joined", "edge_phone_track_subscribed"),
            "edge_sdp_answer_ms": ("edge_claim_received", "edge_direct_answer_ready"),
            "edge_claim_to_sdp_ms": ("edge_claim_received", "edge_direct_sdp_received"),
            "edge_claim_ack_ms": ("edge_claim_received", "edge_claim_ack_sent"),
            "edge_direct_sdp_handled_ms": ("edge_claim_received", "edge_direct_sdp_handled"),
            "edge_sdp_exchange_ms": ("edge_direct_sdp_received", "edge_direct_answer_ready"),
            "edge_direct_ice_ms": ("edge_direct_answer_ready", "edge_direct_connected"),
            "edge_connected_to_no_audio_ms": ("edge_direct_connected", "edge_no_phone_audio"),
            "edge_no_audio_to_ai_ms": ("edge_no_phone_audio", "edge_provisional_ai_opening"),
            "edge_claim_to_phone_frame_ms": ("edge_claim_received", "edge_phone_first_frame"),
        }
        result = {name: round(self._marks[end] - self._marks[start], 3)
                  if start in self._marks and end in self._marks else None
                  for name, (start, end) in intervals.items()}
        for name in ("cloud_cas_ms", "cloud_edge_reserve_ms", "cloud_turn_ms", "cloud_sdp_relay_ms",
                     "cloud_claim_finalize_ms", "cloud_total_ms"):
            result[name] = self._marks.get("client_" + name)
        for name in ("audio_engine_running", "audio_engine_failed", "direct_control_ready"):
            result["client_" + name + "_ms"] = self._marks.get("client_" + name)
        return result

    def include_preconnect(self, other: CallTiming) -> None:
        for stage, ms in other._marks.items():
            self._emit(stage, round(ms + (other._origin - self._origin) * 1000, 3))

    def receive_client(self, command: dict[str, Any]) -> None:
        if not valid_client_timing(command):
            return
        attempt, seq, marks = command["attempt"], command["seq"], command["marks"]
        if seq <= self._seq or (self._attempt is not None and attempt != self._attempt):
            return
        self._attempt, self._seq = attempt, seq
        for name, value in marks.items():
            if name in CLIENT_MARKS:
                self._emit("client_" + name, value)
        if "audioEngine" in command:
            observation = {"seq": seq, "audioEngine": dict(command["audioEngine"])}
            if self._record is not None:
                self._record.log_event("client_audio_engine_state", **observation)
            else:
                self._pending_engines.append(observation)

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
