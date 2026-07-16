"""Edge content repository v1: stable identity, pagination and normalization."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from agentcall.call_log import CallLogger
from agentcall.content_sync import ContentSyncError, ContentSyncRepository
from agentcall.events import EventHub


def _repository(tmp_path):
    hub = EventHub(asyncio.new_event_loop(), store_path=tmp_path / "messages.json")
    calls = CallLogger(tmp_path / "recordings")
    return hub, calls, ContentSyncRepository(hub, calls)


def test_messages_use_persisted_ids_and_anchored_newest_first_pagination(tmp_path):
    hub, _calls, repository = _repository(tmp_path)
    for index in range(4):
        hub.publish(
            {
                "type": "sms_out",
                "number": "10086",
                "text": f"fragment {index}",
                "status": "sent",
                "ts": 100.0 + index,
            }
        )

    first = repository.list_messages(limit=2)
    first_ids = [item["messageId"] for item in first["items"]]
    assert [item["text"] for item in first["items"]] == ["fragment 3", "fragment 2"]
    assert first["hasMore"] is True

    hub.publish(
        {
            "type": "sms_in",
            "sender": "10086",
            "text": "arrived after anchor",
            "ts": 999.0,
        }
    )
    second = repository.list_messages(limit=2, cursor=first["nextCursor"])
    second_ids = [item["messageId"] for item in second["items"]]

    assert [item["text"] for item in second["items"]] == ["fragment 1", "fragment 0"]
    assert not set(first_ids) & set(second_ids)
    assert "arrived after anchor" not in {item["text"] for item in second["items"]}
    assert second["hasMore"] is False


def test_message_dto_preserves_exact_text_and_normalizes_status_and_timestamps(tmp_path):
    hub, _calls, repository = _repository(tmp_path)
    text = "verification value 012345; exact fragment"
    hub.publish(
        {
            "type": "sms_in",
            "sender": "+15550100001",
            "text": text,
            "sms_ts": "26/07/16,12:00:00+32",
            "ts": 1784180000.0,
        }
    )
    hub.publish(
        {
            "type": "sms_out",
            "number": "+15550100001",
            "text": "failed",
            "status": "failed",
            "ts": 1784180001.0,
        }
    )

    items = repository.list_messages(limit=25)["items"]
    inbound = next(item for item in items if item["direction"] == "INBOUND")
    outbound = next(item for item in items if item["direction"] == "OUTBOUND")

    assert inbound["text"] == text
    assert inbound["status"] == "RECEIVED"
    assert inbound["occurredAt"] != inbound["recordedAt"]
    assert outbound["status"] == "FAILED"
    assert re.fullmatch(r"revision_[A-Za-z0-9_-]{12,80}", inbound["revision"])


def test_cursor_is_resource_bound_and_rejects_malformed_or_stale_position(tmp_path):
    hub, calls, repository = _repository(tmp_path)
    for index in range(3):
        hub.publish(
            {"type": "sms_out", "number": "10086", "text": str(index), "ts": index}
        )
    calls.begin_call("outbound", "10086").finish("completed")
    cursor = repository.list_messages(limit=1)["nextCursor"]

    with pytest.raises(ContentSyncError, match="CURSOR_INVALID"):
        repository.list_call_records(limit=1, cursor=cursor)
    with pytest.raises(ContentSyncError, match="CURSOR_INVALID"):
        repository.list_messages(limit=1, cursor="cursor_not-base64")


def test_call_public_id_is_stable_non_pii_and_late_summary_advances_revisions(tmp_path):
    _hub, calls, repository = _repository(tmp_path)
    record = calls.begin_call("inbound", "+15550100002")
    record.log_event("answered")
    record.log_event("transcript", role="user", text="Please transfer me.")
    record.finish("completed")
    record.mark_summary_pending()

    pending = repository.list_call_records(limit=25)["items"][0]
    pending_detail = repository.get_call_record(pending["callId"])
    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))

    assert pending["callId"] == meta["public_id"] == record.public_id
    assert record.id not in pending["callId"]
    assert "+15550100002" not in pending["callId"]
    assert pending["summaryState"] == "PENDING"
    assert pending_detail["summary"] is None

    record.set_summary(
        {
            "ok": True,
            "summary": "The caller requested the owner.",
            "caller_identity": "Synthetic caller",
            "intent": "Speak to owner",
            "urgency": "normal",
            "callback_needed": False,
        }
    )
    ready = repository.list_call_records(limit=25)["items"][0]
    ready_detail = repository.get_call_record(ready["callId"])

    assert ready["callId"] == pending["callId"]
    assert ready["revision"] != pending["revision"]
    assert ready["summaryState"] == "READY"
    assert ready_detail["timelineRevision"] != pending_detail["timelineRevision"]
    assert ready_detail["summary"]["text"] == "The caller requested the owner."
    assert "evidence" not in ready_detail["summary"]


def test_legacy_call_metadata_migrates_public_identity_once(tmp_path):
    _hub, calls, repository = _repository(tmp_path)
    local_id = "20260716-120000-inbound-15550100003"
    path = calls.base_dir / local_id
    path.mkdir()
    (path / "meta.json").write_text(
        json.dumps(
            {
                "id": local_id,
                "direction": "inbound",
                "number": "+15550100003",
                "started_at": 100.0,
                "ended_at": 110.0,
                "duration": 10.0,
                "status": "completed",
                "answered": True,
            }
        ),
        encoding="utf-8",
    )
    (path / "events.jsonl").write_text(
        json.dumps({"type": "call_finished", "ts": 110.0, "status": "completed"})
        + "\n",
        encoding="utf-8",
    )

    first = repository.list_call_records(limit=25)["items"][0]
    migrated = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    second = repository.list_call_records(limit=25)["items"][0]

    assert first["callId"] == second["callId"] == migrated["public_id"]
    assert local_id not in first["callId"]
    assert isinstance(migrated["content_updated_at"], (int, float))
    assert first["summaryState"] == "UNAVAILABLE"


def test_timeline_is_chronological_public_union_and_filters_debug_fields(tmp_path):
    _hub, calls, repository = _repository(tmp_path)
    record = calls.begin_call("inbound", "+15550100004")
    record.log_event("latency", stage="secret_internal_stage", ms=123)
    record.log_event("transcript", role="agent", text="Who is calling?")
    record.log_event("transcript", role="user", text="I need the owner.")
    record.log_event(
        "inbound_triage_consumed",
        outcome="transfer",
        category="personal",
        action="transfer",
        confidence=0.94,
        reason_code="owner_requested",
        reason="threshold_met",
    )
    record.log_event("takeover_requested", trigger="triage_judge")
    record.log_event("takeover_committed", generation=2)
    record.log_event("dtmf", digits="1", result="success")
    record.finish("completed")

    call_id = repository.list_call_records(limit=25)["items"][0]["callId"]
    page = repository.list_call_timeline(call_id, limit=50)

    assert [item["occurredAt"] for item in page["items"]] == sorted(
        item["occurredAt"] for item in page["items"]
    )
    assert {item["type"] for item in page["items"]} == {
        "TRANSCRIPT",
        "TRIAGE",
        "TAKEOVER",
        "RESULT",
    }
    serialized = json.dumps(page)
    assert "secret_internal_stage" not in serialized
    assert '"digits"' not in serialized
    triage = next(item for item in page["items"] if item["type"] == "TRIAGE")
    assert triage == {
        "timelineItemId": triage["timelineItemId"],
        "occurredAt": triage["occurredAt"],
        "type": "TRIAGE",
        "category": "PERSONAL",
        "action": "TRANSFER",
        "confidence": 0.94,
        "reasonCode": "OWNER_REQUESTED",
    }


def test_remote_handset_call_has_normal_empty_content_state(tmp_path):
    _hub, calls, repository = _repository(tmp_path)
    record = calls.begin_call(
        "outbound", "+15550100005", source="remote_web_dialer"
    )
    record.log_event("answered")
    record.finish("completed")

    item = repository.list_call_records(limit=25)["items"][0]
    detail = repository.get_call_record(item["callId"])
    timeline = repository.list_call_timeline(item["callId"], limit=50)

    assert item["source"] == "REMOTE_HANDSET"
    assert item["summaryState"] == "UNAVAILABLE"
    assert item["hasTranscript"] is False
    assert detail["summary"] is None
    assert timeline["items"] == []


def test_corrupt_summary_is_a_structured_failure_not_ambiguous_null(tmp_path):
    _hub, calls, repository = _repository(tmp_path)
    record = calls.begin_call("outbound", "+15550100006")
    record.finish("completed")
    record.mark_summary_pending()
    (record.path / "summary.json").write_text("{broken", encoding="utf-8")

    item = repository.list_call_records(limit=25)["items"][0]
    detail = repository.get_call_record(item["callId"])

    assert item["summaryState"] == "FAILED"
    assert detail["summary"] == {
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


@pytest.mark.parametrize(
    "resource,params",
    [
        ("messages.list", {"limit": True, "cursor": None}),
        ("messages.list", {"limit": 25, "cursor": None, "extra": 1}),
        ("call_records.get", {"callId": "../meta.json"}),
        ("run.shell", {}),
    ],
)
def test_read_boundary_rejects_unknown_or_malformed_requests(tmp_path, resource, params):
    _hub, _calls, repository = _repository(tmp_path)

    with pytest.raises(ContentSyncError, match="INVALID_REQUEST"):
        repository.read(resource, params)
