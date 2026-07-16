"""Machine-readable guards for the shared content-sync v1 fixtures (#99)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "content-sync" / "v1"
)
OPAQUE_ID = re.compile(r"^[a-z]+_[A-Za-z0-9_-]{12,80}$")
MAX_WIRE_BYTES = 16 * 1024


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _wire_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _assert_page(page: dict[str, Any]) -> None:
    assert page["v"] == 1
    assert isinstance(page["items"], list)
    assert page["hasMore"] is (page["nextCursor"] is not None)
    assert OPAQUE_ID.fullmatch(page["collectionRevision"])
    assert page["oldestAvailableAt"] is None or isinstance(
        page["oldestAvailableAt"], int
    )


def test_all_json_fixtures_parse_and_fit_v1_wire_limit() -> None:
    paths = sorted(FIXTURE_DIR.glob("*.json"))
    assert paths
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert _wire_size(value) <= MAX_WIRE_BYTES, path.name


def test_messages_fixture_keeps_carrier_fragments_as_separate_items() -> None:
    page = _load("messages-page.json")
    _assert_page(page)
    fragments = [item for item in page["items"] if "fragment" in item["messageId"]]

    assert len(fragments) == 2
    assert len({item["messageId"] for item in fragments}) == 2
    assert len({item["occurredAt"] for item in fragments}) == 1
    assert all("threadId" not in item and "multipart" not in item for item in fragments)
    assert all(OPAQUE_ID.fullmatch(item["messageId"]) for item in page["items"])
    assert {item["direction"] for item in page["items"]} == {"INBOUND", "OUTBOUND"}


def test_late_summary_preserves_call_id_and_advances_revisions() -> None:
    pending = _load("call-record-detail-pending.json")
    ready = _load("call-record-detail-ready.json")

    assert pending["record"]["callId"] == ready["record"]["callId"]
    assert pending["record"]["revision"] != ready["record"]["revision"]
    assert pending["timelineRevision"] != ready["timelineRevision"]
    assert pending["record"]["summaryState"] == "PENDING"
    assert pending["summary"] is None
    assert ready["record"]["summaryState"] == "READY"
    assert ready["summary"]["ok"] is True
    assert ready["summary"]["text"] == ready["record"]["summaryPreview"]


def test_call_record_page_uses_public_ids_and_normalized_states() -> None:
    page = _load("call-records-page.json")
    _assert_page(page)

    assert all(OPAQUE_ID.fullmatch(item["callId"]) for item in page["items"])
    assert {item["status"] for item in page["items"]} == {"COMPLETED"}
    assert {item["source"] for item in page["items"]} == {
        "AGENT",
        "REMOTE_HANDSET",
    }
    assert not any("outbound" in item["callId"] for item in page["items"])


def test_remote_handset_without_ai_content_is_a_valid_empty_state() -> None:
    detail = _load("call-record-detail-no-transcript.json")
    timeline = _load("call-timeline-empty.json")

    assert detail["record"]["source"] == "REMOTE_HANDSET"
    assert detail["record"]["summaryState"] == "UNAVAILABLE"
    assert detail["record"]["hasTranscript"] is False
    assert detail["summary"] is None
    _assert_page(timeline)
    assert timeline["items"] == []


def test_timeline_fixture_is_chronological_and_only_uses_public_union() -> None:
    page = _load("call-timeline-page.json")
    _assert_page(page)
    items = page["items"]

    assert [item["occurredAt"] for item in items] == sorted(
        item["occurredAt"] for item in items
    )
    assert {item["type"] for item in items} == {
        "TRANSCRIPT",
        "TRIAGE",
        "TAKEOVER",
        "RESULT",
    }
    assert all(OPAQUE_ID.fullmatch(item["timelineItemId"]) for item in items)
    assert not any("reasoning" in item or "prompt" in item for item in items)


def test_edge_relay_fixture_correlates_request_and_response() -> None:
    request = _load("edge-data-request.json")
    response = _load("edge-data-response.json")

    assert request["v"] == response["v"] == 1
    assert request["type"] == "data.request"
    assert response["type"] == "data.response"
    assert request["requestId"] == response["requestId"]
    assert request["resource"] == response["resource"] == "messages.list"
    assert 0 < request["expiresAtUnixMs"] - request["issuedAtUnixMs"] <= 10_000
    assert response["status"] == "ok"
    _assert_page(response["body"])


def test_error_fixtures_cover_required_stable_codes_without_content() -> None:
    errors = _load("errors.json")
    codes = {item["body"]["error"]["code"] for item in errors}
    serialized = json.dumps(errors)

    assert codes == {
        "INVALID_REQUEST",
        "CURSOR_INVALID",
        "UNAUTHORIZED",
        "FORBIDDEN",
        "FEATURE_DISABLED",
        "NOT_FOUND",
        "RATE_LIMITED",
        "EDGE_OFFLINE",
        "TIMEOUT",
        "PAYLOAD_TOO_LARGE",
        "INTERNAL_ERROR",
    }
    assert "transcript" not in serialized.lower()
    assert "token" not in serialized.lower()
    assert not re.search(r'"address":\s*"(?!\+15550100\d{3}")', serialized)
