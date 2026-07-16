"""Read-only, privacy-bounded content repository for mobile sync protocol v1."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast

from .call_log import CallLogger
from .events import EventHub

ContentResource = Literal[
    "messages.list",
    "call_records.list",
    "call_records.get",
    "call_timeline.list",
]

_PUBLIC_ID_RE = re.compile(r"^[a-z]+_[A-Za-z0-9_-]{12,80}$")
_CALL_ID_RE = re.compile(r"^call_[A-Za-z0-9_-]{12,80}$")
_MESSAGE_ID_RE = re.compile(r"^msg_[A-Za-z0-9_-]{12,80}$")
_SMS_TIMESTAMP_RE = re.compile(
    r"^(?P<stamp>\d{2}/\d{2}/\d{2},\d{2}:\d{2}:\d{2})(?P<zone>[+-]\d{2})?$"
)
_CALL_ID_NAMESPACE = b"callpilot-content-sync-call-v1\0"
_CURSOR_PREFIX = "cursor_"


class ContentSyncError(ValueError):
    """A stable, client-safe content repository failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ContentSyncRepository:
    """Normalize the Edge's SMS and call artifacts into the frozen v1 DTOs."""

    def __init__(self, hub: EventHub, call_logger: CallLogger) -> None:
        self._hub = hub
        self._call_logger = call_logger

    def read(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        if resource == "messages.list":
            limit, cursor = _list_params(params)
            return self.list_messages(limit=limit, cursor=cursor)
        if resource == "call_records.list":
            limit, cursor = _list_params(params)
            return self.list_call_records(limit=limit, cursor=cursor)
        if resource == "call_records.get":
            call_id = _call_id_params(params)
            return self.get_call_record(call_id)
        if resource == "call_timeline.list":
            call_id, limit, cursor = _timeline_params(params)
            return self.list_call_timeline(call_id, limit=limit, cursor=cursor)
        raise ContentSyncError("INVALID_REQUEST")

    def list_messages(
        self, *, limit: int = 25, cursor: str | None = None
    ) -> dict[str, Any]:
        _validate_limit(limit)
        messages = self._message_items()
        return _paginate(
            messages,
            resource="messages.list",
            limit=limit,
            cursor=cursor,
            timestamp_field="occurredAt",
            id_field="messageId",
            newest_first=True,
        )

    def list_call_records(
        self, *, limit: int = 25, cursor: str | None = None
    ) -> dict[str, Any]:
        _validate_limit(limit)
        calls = [artifact.record for artifact in self._call_artifacts()]
        return _paginate(
            calls,
            resource="call_records.list",
            limit=limit,
            cursor=cursor,
            timestamp_field="startedAt",
            id_field="callId",
            newest_first=True,
        )

    def get_call_record(self, call_id: str) -> dict[str, Any]:
        _validate_call_id(call_id)
        artifact = self._find_call(call_id)
        return {
            "v": 1,
            "record": artifact.record,
            "summary": artifact.summary,
            "timelineRevision": _revision("timeline", artifact.timeline),
        }

    def list_call_timeline(
        self,
        call_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _validate_call_id(call_id)
        _validate_limit(limit)
        artifact = self._find_call(call_id)
        return _paginate(
            artifact.timeline,
            resource=f"call_timeline.list:{call_id}",
            limit=limit,
            cursor=cursor,
            timestamp_field="occurredAt",
            id_field="timelineItemId",
            newest_first=False,
        )

    def _message_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for event in self._hub.history():
            event_type = event.get("type")
            if event_type not in {"sms_in", "sms_out"}:
                continue
            message_id = event.get("message_id")
            if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(
                message_id
            ):
                continue
            recorded_at = _epoch_ms(event.get("ts"))
            occurred_at = _sms_epoch_ms(event.get("sms_ts")) or recorded_at
            direction = "INBOUND" if event_type == "sms_in" else "OUTBOUND"
            address_key = "sender" if event_type == "sms_in" else "number"
            public = {
                "messageId": message_id,
                "direction": direction,
                "address": str(event.get(address_key) or ""),
                "text": str(event.get("text") or ""),
                "occurredAt": occurred_at,
                "recordedAt": recorded_at,
                "status": _message_status(event),
            }
            public["revision"] = _revision("message", public)
            items.append(public)
        return items

    def _call_artifacts(self) -> list[_CallArtifact]:
        artifacts: list[_CallArtifact] = []
        base_dir = self._call_logger.base_dir
        if not base_dir.is_dir():
            return artifacts
        for path in base_dir.iterdir():
            if not path.is_dir():
                continue
            artifact = _read_call_artifact(path)
            if artifact is not None:
                artifacts.append(artifact)
        return artifacts

    def _find_call(self, call_id: str) -> _CallArtifact:
        for artifact in self._call_artifacts():
            if artifact.record["callId"] == call_id:
                return artifact
        raise ContentSyncError("NOT_FOUND")


class _CallArtifact:
    def __init__(
        self,
        record: dict[str, Any],
        summary: dict[str, Any] | None,
        timeline: list[dict[str, Any]],
    ) -> None:
        self.record = record
        self.summary = summary
        self.timeline = timeline


def _read_call_artifact(path: Path) -> _CallArtifact | None:
    meta_path = path / "meta.json"
    meta = _read_json_object(meta_path)
    if meta is None:
        return None
    public_id = _ensure_public_call_metadata(path, meta)
    if public_id is None:
        return None
    source = _call_source(meta.get("source"))
    summary = (
        None
        if source == "REMOTE_HANDSET"
        else _normalized_summary(path / "summary.json")
    )
    if (
        source != "REMOTE_HANDSET"
        and summary is None
        and str(meta.get("summary_state") or "").upper() in {"READY", "FAILED"}
    ):
        summary = _failed_summary()
    events = _read_events(path / "events.jsonl")
    timeline = _build_timeline(public_id, meta, source, events, summary)
    summary_state = _summary_state(source, path / "summary.json", summary, meta)
    summary_preview = _summary_preview(summary) if summary_state == "READY" else None
    record: dict[str, Any] = {
        "callId": public_id,
        "direction": _call_direction(meta.get("direction")),
        "address": _optional_string(meta.get("number")),
        "startedAt": _epoch_ms(meta.get("started_at")),
        "endedAt": _optional_epoch_ms(meta.get("ended_at")),
        "durationMs": _duration_ms(meta),
        "status": _call_status(meta.get("status")),
        "answered": meta.get("answered") is True
        or (
            "answered" not in meta
            and any(event.get("type") == "answered" for _, event in events)
        ),
        "source": source,
        "summaryState": summary_state,
        "summaryPreview": summary_preview,
        "hasTranscript": any(item["type"] == "TRANSCRIPT" for item in timeline),
        "triageOutcome": _triage_outcome(meta, events),
    }
    record["revision"] = _revision("call", record)
    return _CallArtifact(record, summary, timeline)


def _ensure_public_call_metadata(path: Path, meta: dict[str, Any]) -> str | None:
    public_id = meta.get("public_id")
    changed = False
    if not isinstance(public_id, str) or _CALL_ID_RE.fullmatch(public_id) is None:
        local_id = str(meta.get("id") or path.name)
        public_id = _opaque_digest("call", _CALL_ID_NAMESPACE + local_id.encode("utf-8"))
        meta["public_id"] = public_id
        changed = True
    updated = meta.get("content_updated_at")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        candidates = [
            _finite_number(meta.get("ended_at")),
            _finite_number(meta.get("started_at")),
        ]
        summary_path = path / "summary.json"
        try:
            candidates.append(summary_path.stat().st_mtime)
        except OSError:
            pass
        meta["content_updated_at"] = max((v for v in candidates if v is not None), default=0.0)
        changed = True
    if changed and not _atomic_write_json(path / "meta.json", meta):
        return None
    return public_id


def _build_timeline(
    public_id: str,
    meta: dict[str, Any],
    source: str,
    events: list[tuple[int, dict[str, Any]]],
    summary: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if source == "REMOTE_HANDSET":
        return []
    timeline: list[dict[str, Any]] = []
    for line_number, event in events:
        event_type = event.get("type")
        fields: dict[str, Any] | None = None
        if event_type == "transcript":
            role = _transcript_role(event.get("role"))
            text = event.get("text")
            if role is not None and isinstance(text, str) and text:
                fields = {"type": "TRANSCRIPT", "role": role, "text": text}
        elif event_type == "inbound_triage_consumed":
            fields = _triage_timeline_fields(event)
        elif event_type in {
            "takeover_requested",
            "takeover_committed",
            "takeover_owner_hangup",
            "takeover_rollback",
            "takeover_notice_then_hangup",
        }:
            fields = _takeover_timeline_fields(event)
        elif event_type == "call_finished":
            fields = {
                "type": "RESULT",
                "status": _call_status(event.get("status", meta.get("status"))),
                "summary": _summary_text(summary),
            }
        if fields is None:
            continue
        occurred_at = _epoch_ms(event.get("ts"))
        item_id = _timeline_item_id(public_id, line_number, event)
        timeline.append(
            {"timelineItemId": item_id, "occurredAt": occurred_at, **fields}
        )
    timeline.sort(key=lambda item: (item["occurredAt"], item["timelineItemId"]))
    return timeline


def _triage_timeline_fields(event: dict[str, Any]) -> dict[str, Any]:
    category = str(event.get("category") or "unknown").upper()
    if category == "SERVICE":
        category = "NEEDS_OWNER" if event.get("outcome") == "transfer" else "UNKNOWN"
    if category not in {"MARKETING", "PERSONAL", "NEEDS_OWNER", "UNKNOWN"}:
        category = "UNKNOWN"
    action = str(event.get("outcome") or event.get("action") or "continue_ai").upper()
    if action not in {"CLARIFY", "CONTINUE_AI", "REJECT", "TRANSFER"}:
        action = "CONTINUE_AI"
    confidence = _finite_number(event.get("confidence"))
    return {
        "type": "TRIAGE",
        "category": category,
        "action": action,
        "confidence": min(1.0, max(0.0, confidence or 0.0)),
        "reasonCode": _reason_code(event.get("reason_code")),
    }


def _takeover_timeline_fields(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("type")
    state = {
        "takeover_requested": "REQUESTED",
        "takeover_committed": "COMMITTED",
        "takeover_owner_hangup": "OWNER_HANGUP",
        "takeover_rollback": "FAILED",
        "takeover_notice_then_hangup": "FAILED",
    }.get(str(event_type), "FAILED")
    reason = event.get("reason")
    if reason is None and event_type == "takeover_notice_then_hangup":
        reason = "MEDIA_DISCONNECTED"
    return {
        "type": "TAKEOVER",
        "state": state,
        "reasonCode": _reason_code(reason) if reason is not None else None,
    }


def _paginate(
    items: list[dict[str, Any]],
    *,
    resource: str,
    limit: int,
    cursor: str | None,
    timestamp_field: str,
    id_field: str,
    newest_first: bool,
) -> dict[str, Any]:
    ordered = sorted(
        items,
        key=lambda item: (item[timestamp_field], item[id_field]),
        reverse=newest_first,
    )
    current_keys = {(item[timestamp_field], item[id_field]) for item in ordered}
    start = 0
    if cursor is None:
        anchor = (
            (ordered[0][timestamp_field], ordered[0][id_field])
            if newest_first and ordered
            else (ordered[-1][timestamp_field], ordered[-1][id_field])
            if ordered
            else None
        )
        anchored = ordered
    else:
        decoded = _decode_cursor(cursor, resource, newest_first)
        anchor = decoded["anchor"]
        position = decoded["position"]
        if anchor not in current_keys or position not in current_keys:
            raise ContentSyncError("CURSOR_INVALID")
        if newest_first:
            anchored = [
                item
                for item in ordered
                if (item[timestamp_field], item[id_field]) <= anchor
            ]
        else:
            anchored = [
                item
                for item in ordered
                if (item[timestamp_field], item[id_field]) <= anchor
            ]
        try:
            start = next(
                index + 1
                for index, item in enumerate(anchored)
                if (item[timestamp_field], item[id_field]) == position
            )
        except StopIteration:
            raise ContentSyncError("CURSOR_INVALID") from None
    page_items = anchored[start : start + limit]
    has_more = start + len(page_items) < len(anchored)
    next_cursor = None
    if has_more and anchor is not None and page_items:
        last = page_items[-1]
        next_cursor = _encode_cursor(
            resource,
            anchor,
            (last[timestamp_field], last[id_field]),
            newest_first,
        )
    oldest = min((item[timestamp_field] for item in ordered), default=None)
    return {
        "v": 1,
        "items": page_items,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "collectionRevision": _revision("collection", ordered),
        "oldestAvailableAt": oldest,
    }


def _encode_cursor(
    resource: str,
    anchor: tuple[int, str],
    position: tuple[int, str],
    newest_first: bool,
) -> str:
    payload = {
        "v": 1,
        "r": resource,
        "a": [anchor[0], anchor[1]],
        "p": [position[0], position[1]],
        "d": "desc" if newest_first else "asc",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(
    cursor: str, resource: str, newest_first: bool
) -> dict[str, tuple[int, str]]:
    if not isinstance(cursor, str) or not cursor.startswith(_CURSOR_PREFIX) or len(cursor) > 2048:
        raise ContentSyncError("CURSOR_INVALID")
    encoded = cursor[len(_CURSOR_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ContentSyncError("CURSOR_INVALID") from None
    direction = "desc" if newest_first else "asc"
    if (
        not isinstance(value, dict)
        or set(value) != {"v", "r", "a", "p", "d"}
        or value.get("v") != 1
        or value.get("r") != resource
        or value.get("d") != direction
    ):
        raise ContentSyncError("CURSOR_INVALID")
    return {
        "anchor": _cursor_key(value.get("a")),
        "position": _cursor_key(value.get("p")),
    }


def _cursor_key(value: Any) -> tuple[int, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or isinstance(value[0], bool)
        or not isinstance(value[0], int)
        or not isinstance(value[1], str)
        or _PUBLIC_ID_RE.fullmatch(value[1]) is None
    ):
        raise ContentSyncError("CURSOR_INVALID")
    return value[0], value[1]


def _normalized_summary(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = _read_json_object(path)
    if value is None:
        return _failed_summary()
    ok = value.get("ok") is True
    text = _optional_string(value.get("summary", value.get("text")))
    return {
        "ok": ok,
        "text": text,
        "callerIdentity": _optional_string(value.get("caller_identity")),
        "intent": _optional_string(value.get("intent")),
        "urgency": _normalized_optional_enum(value.get("urgency")),
        "callbackNeeded": value.get("callback_needed")
        if isinstance(value.get("callback_needed"), bool)
        else None,
        "errorCode": None if ok else _summary_error_code(value.get("error")),
        "resultSource": _result_source(value.get("result_source")),
        "resultVerification": _normalized_optional_enum(
            value.get("result_verification")
        ),
    }


def _summary_state(
    source: str,
    path: Path,
    summary: dict[str, Any] | None,
    meta: dict[str, Any],
) -> str:
    if source == "REMOTE_HANDSET":
        return "UNAVAILABLE"
    if path.exists() or summary is not None:
        return "READY" if summary is not None and summary["ok"] is True else "FAILED"
    explicit = str(meta.get("summary_state") or "").upper()
    if explicit in {"PENDING", "UNAVAILABLE"}:
        return explicit
    return "UNAVAILABLE"


def _failed_summary() -> dict[str, Any]:
    return {
        "ok": False,
        "text": None,
        "callerIdentity": None,
        "intent": None,
        "urgency": None,
        "callbackNeeded": None,
        "errorCode": "SUMMARY_FAILED",
        "resultSource": None,
        "resultVerification": None,
    }


def _summary_preview(summary: dict[str, Any] | None) -> str | None:
    text = _summary_text(summary)
    if text is None:
        return None
    return text if len(text) <= 240 else text[:239] + "…"


def _summary_text(summary: dict[str, Any] | None) -> str | None:
    if summary is None:
        return None
    return _optional_string(summary.get("text"))


def _summary_error_code(value: Any) -> str:
    error = str(value or "").lower()
    if "timeout" in error or "超时" in error:
        return "SUMMARY_TIMEOUT"
    if "transcript" in error or "转写为空" in error or "无用户发言" in error:
        return "NO_TRANSCRIPT"
    return "SUMMARY_FAILED"


def _result_source(value: Any) -> str | None:
    source = str(value or "").strip().lower()
    if not source:
        return None
    return {"carrier_sms": "CARRIER_MESSAGE", "transcript": "TRANSCRIPT"}.get(
        source, source.upper()
    )


def _triage_outcome(meta: dict[str, Any], events: list[tuple[int, dict[str, Any]]]) -> str | None:
    if meta.get("direction") != "inbound":
        return None
    outcomes = [
        str(event.get("outcome") or "")
        for _, event in events
        if event.get("type") == "inbound_triage_consumed"
    ]
    if "transfer" in outcomes:
        return "TRANSFERRED"
    if "reject" in outcomes:
        return "REJECTED"
    if outcomes:
        return "AI_HANDLED"
    return "UNKNOWN"


def _message_status(event: dict[str, Any]) -> str:
    if event.get("type") == "sms_in":
        return "RECEIVED"
    status = str(event.get("status") or "").strip().lower()
    if status in {"sent", "success"}:
        return "SENT"
    if status in {"failed", "failure"}:
        return "FAILED"
    return "ERROR"


def _call_direction(value: Any) -> str:
    return "INBOUND" if value == "inbound" else "OUTBOUND"


def _call_status(value: Any) -> str:
    return {
        "completed": "COMPLETED",
        "not_connected": "NOT_CONNECTED",
        "failed": "FAILED",
    }.get(str(value or "").lower(), "UNKNOWN")


def _call_source(value: Any) -> str:
    if value == "remote_web_dialer":
        return "REMOTE_HANDSET"
    if value in {None, "", "agent"}:
        return "AGENT"
    return "UNKNOWN"


def _transcript_role(value: Any) -> str | None:
    if value in {"agent", "assistant"}:
        return "AGENT"
    if value in {"user", "caller"}:
        return "CALLER"
    return None


def _duration_ms(meta: dict[str, Any]) -> int | None:
    duration = _finite_number(meta.get("duration"))
    if duration is None:
        started = _finite_number(meta.get("started_at"))
        ended = _finite_number(meta.get("ended_at"))
        if started is None or ended is None:
            return None
        duration = ended - started
    return max(0, round(duration * 1000))


def _sms_epoch_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = _SMS_TIMESTAMP_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        parsed = datetime.strptime(match.group("stamp"), "%y/%m/%d,%H:%M:%S")
        zone = match.group("zone")
        if zone:
            quarters = int(zone)
            parsed = parsed.replace(
                tzinfo=timezone(timedelta(minutes=quarters * 15))
            )
        else:
            parsed = parsed.astimezone()
        return round(parsed.timestamp() * 1000)
    except (OverflowError, ValueError):
        return None


def _epoch_ms(value: Any) -> int:
    number = _finite_number(value)
    return round((number or 0.0) * 1000)


def _optional_epoch_ms(value: Any) -> int | None:
    number = _finite_number(value)
    return None if number is None else round(number * 1000)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _normalized_optional_enum(value: Any) -> str | None:
    text = _optional_string(value)
    return text.upper() if text is not None else None


def _reason_code(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "UNKNOWN")).strip("_")
    return (text or "UNKNOWN")[:64].upper()


def _timeline_item_id(public_id: str, line_number: int, event: dict[str, Any]) -> str:
    canonical = json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return _opaque_digest(
        "item", public_id.encode("ascii") + b"\0" + str(line_number).encode("ascii") + b"\0" + canonical
    )


def _revision(scope: str, value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return _opaque_digest("revision", scope.encode("ascii") + b"\0" + canonical)


def _opaque_digest(prefix: str, value: bytes) -> str:
    digest = hashlib.sha256(value).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{prefix}_{encoded}"


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_events(path: Path) -> list[tuple[int, dict[str, Any]]]:
    events: list[tuple[int, dict[str, Any]]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append((line_number, event))
    except (OSError, UnicodeDecodeError):
        pass
    return events


def _atomic_write_json(path: Path, value: dict[str, Any]) -> bool:
    temp_path = path.with_suffix(path.suffix + ".content-sync.tmp")
    try:
        temp_path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(path)
        return True
    except OSError:
        temp_path.unlink(missing_ok=True)
        return False


def _list_params(params: dict[str, Any]) -> tuple[int, str | None]:
    if not isinstance(params, dict) or set(params) != {"limit", "cursor"}:
        raise ContentSyncError("INVALID_REQUEST")
    limit = params.get("limit")
    cursor = params.get("cursor")
    _validate_limit(limit)
    if cursor is not None and not isinstance(cursor, str):
        raise ContentSyncError("INVALID_REQUEST")
    return cast(int, limit), cursor


def _call_id_params(params: dict[str, Any]) -> str:
    if not isinstance(params, dict) or set(params) != {"callId"}:
        raise ContentSyncError("INVALID_REQUEST")
    call_id = params.get("callId")
    _validate_call_id(call_id)
    return cast(str, call_id)


def _timeline_params(params: dict[str, Any]) -> tuple[str, int, str | None]:
    if not isinstance(params, dict) or set(params) != {"callId", "limit", "cursor"}:
        raise ContentSyncError("INVALID_REQUEST")
    call_id = params.get("callId")
    limit = params.get("limit")
    cursor = params.get("cursor")
    _validate_call_id(call_id)
    _validate_limit(limit)
    if cursor is not None and not isinstance(cursor, str):
        raise ContentSyncError("INVALID_REQUEST")
    return cast(str, call_id), cast(int, limit), cursor


def _validate_limit(limit: Any) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ContentSyncError("INVALID_REQUEST")


def _validate_call_id(call_id: Any) -> None:
    if not isinstance(call_id, str) or _CALL_ID_RE.fullmatch(call_id) is None:
        raise ContentSyncError("INVALID_REQUEST")
