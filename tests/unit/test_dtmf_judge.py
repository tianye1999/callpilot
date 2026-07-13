"""DTMF text judge: strict contract, shadow privacy, batching, and lifecycle."""

from __future__ import annotations

import json
import math
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from agentcall.dtmf_judge import (
    DtmfActionLedger,
    DtmfJudge,
    JudgeValidationError,
    build_judge_messages,
    parse_judge_decision,
)


class SpyRecord:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True)
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def log_event(self, event_type: str, **fields) -> None:
        with self._lock:
            self.events.append((event_type, fields))


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def valid_response(*, digits: str = "1") -> str:
    return json.dumps(
        {
            "action": "press",
            "digits": digits,
            "confidence": 0.97,
            "reason_code": "menu_matched",
            "reason": "该选项通向本通任务",
        },
        ensure_ascii=False,
    )


def test_parse_judge_decision_accepts_strict_contract():
    decision = parse_judge_decision(valid_response(digits="103#"))

    assert decision.action == "press"
    assert decision.digits == "103#"
    assert decision.confidence == pytest.approx(0.97)
    assert decision.reason_code == "menu_matched"


@pytest.mark.parametrize(
    "payload",
    [
        "```json\n{}\n```",
        "not json",
        json.dumps({"action": "press"}),
        json.dumps(
            {
                "action": "press",
                "digits": "12345",
                "confidence": 0.8,
                "reason_code": "menu_matched",
                "reason": "x",
            }
        ),
        json.dumps(
            {
                "action": "wait",
                "digits": "1",
                "confidence": 0.8,
                "reason_code": "menu_incomplete",
                "reason": "x",
            }
        ),
        json.dumps(
            {
                "action": "wait",
                "confidence": True,
                "reason_code": "menu_incomplete",
                "reason": "x",
            }
        ),
        json.dumps(
            {
                "action": "wait",
                "confidence": math.nan,
                "reason_code": "menu_incomplete",
                "reason": "x",
            }
        ),
        json.dumps(
            {
                "action": "wait",
                "confidence": 0.8,
                "reason_code": "made_up",
                "reason": "x",
            }
        ),
        json.dumps(
            {
                "action": "wait",
                "confidence": 0.8,
                "reason_code": "menu_incomplete",
                "reason": "x" * 51,
            }
        ),
    ],
)
def test_parse_judge_decision_rejects_malformed_or_unsafe_payload(payload):
    with pytest.raises(JudgeValidationError):
        parse_judge_decision(payload)


def test_build_messages_keeps_latest_eight_segments_and_three_actions():
    ledger = DtmfActionLedger(id_factory=lambda: "action-id")
    for index in range(4):
        ledger.record(str(index), "realtime", timestamp=float(index))
    transcripts = [(float(index * 100), f"segment-{index}") for index in range(10)]

    messages = build_judge_messages(
        transcripts,
        ledger.recent(3),
        "查询套餐余量",
    )
    payload = json.loads(messages[1]["content"])

    assert [item["text"] for item in payload["remote_transcripts"]] == [
        f"segment-{index}" for index in range(2, 10)
    ]
    assert [item["digits"] for item in payload["recent_dtmf"]] == ["1", "2", "3"]
    assert [item["t_ms"] for item in payload["recent_dtmf"]] == [1000.0, 2000.0, 3000.0]
    assert payload["task_goal"] == "查询套餐余量"


def test_ledger_records_three_sources_but_public_fields_have_no_digit_fingerprint():
    ids = iter(("r-id", "g-id", "j-id"))
    ledger = DtmfActionLedger(id_factory=lambda: next(ids))

    entries = [
        ledger.record("1", "realtime", timestamp=1.0),
        ledger.record("103#", "guard", timestamp=2.0),
        ledger.record("*", "judge", timestamp=3.0),
    ]

    assert [entry.source for entry in entries] == ["realtime", "guard", "judge"]
    assert [entry.digits for entry in ledger.recent(3)] == ["1", "103#", "*"]
    public = [entry.public_fields() for entry in entries]
    assert public == [
        {"action_id": "r-id", "source": "realtime", "digits_len": 1},
        {"action_id": "g-id", "source": "guard", "digits_len": 4},
        {"action_id": "j-id", "source": "judge", "digits_len": 1},
    ]
    assert "103#" not in repr(public)
    assert "hash" not in repr(public).lower()


def test_private_shadow_file_correlates_cleartext_action_by_opaque_id(tmp_path):
    record = SpyRecord(tmp_path / "recording")
    ledger = DtmfActionLedger(id_factory=lambda: "action-opaque")
    judge = DtmfJudge(
        record=record,
        task_goal="查询套餐余量",
        ledger=ledger,
        model="qwen-plus",
        window_mode="fragmented",
        model_call=lambda messages, model, timeout: (None, "timeout"),
    )
    judge.start()

    judge.record_action(ledger.record("103#", "realtime", timestamp=1.25))
    judge.stop()

    private = json.loads(
        (record.path / "judge_shadow.jsonl").read_text(encoding="utf-8").strip()
    )
    assert private == {
        "kind": "action",
        "action_id": "action-opaque",
        "ts": private["ts"],
        "t_ms": 1250.0,
        "source": "realtime",
        "digits": "103#",
        "digits_len": 4,
    }
    assert isinstance(private["ts"], float)


def test_shadow_batches_segments_logs_redacted_event_and_private_digits(tmp_path):
    record = SpyRecord(tmp_path / "recording")
    ledger = DtmfActionLedger()
    calls: list[list[dict[str, str]]] = []

    def model_call(messages, model, timeout):
        calls.append(messages)
        return valid_response(digits="103#"), None

    judge = DtmfJudge(
        record=record,
        task_goal="查询套餐余量",
        ledger=ledger,
        model="qwen-plus",
        window_mode="merged",
        model_call=model_call,
        throttle_seconds=0.03,
        timeout_seconds=0.2,
    )
    judge.start()
    for index in range(3):
        judge.submit_remote_transcript(f"菜单片段{index}", t_ms=float(index * 100))

    wait_until(lambda: any(kind == "dtmf_judge" for kind, _ in record.events))
    judge.stop()

    assert len(calls) == 1
    model_payload = json.loads(calls[0][1]["content"])
    assert [item["text"] for item in model_payload["remote_transcripts"]] == [
        "菜单片段0",
        "菜单片段1",
        "菜单片段2",
    ]
    events = [fields for kind, fields in record.events if kind == "dtmf_judge"]
    assert len(events) == 1
    event = events[0]
    assert set(event) == {
        "action",
        "confidence",
        "reason_code",
        "latency_ms",
        "window_mode",
        "digits_len",
        "decision_id",
    }
    assert event["digits_len"] == 4
    assert event["window_mode"] == "merged"
    assert "103#" not in repr(event)
    assert "hash" not in repr(event).lower()

    private_path = record.path / "judge_shadow.jsonl"
    private = json.loads(private_path.read_text(encoding="utf-8").strip())
    assert private["kind"] == "decision"
    assert private["decision_id"] == event["decision_id"]
    assert private["digits"] == "103#"
    assert private["reason"] == "该选项通向本通任务"
    if os.name == "posix":
        assert stat.S_IMODE(private_path.stat().st_mode) == 0o600


def test_malformed_model_output_emits_only_sanitized_error(tmp_path):
    record = SpyRecord(tmp_path / "recording")
    secret_output = "bad payload containing 103#"
    judge = DtmfJudge(
        record=record,
        task_goal="查询套餐余量",
        ledger=DtmfActionLedger(),
        model="qwen-plus",
        window_mode="fragmented",
        model_call=lambda messages, model, timeout: (secret_output, None),
        throttle_seconds=0.01,
    )
    judge.start()
    judge.submit_remote_transcript("请按一", t_ms=10.0)

    wait_until(lambda: any(kind == "judge_error" for kind, _ in record.events))
    judge.stop()

    errors = [fields for kind, fields in record.events if kind == "judge_error"]
    assert len(errors) == 1
    assert set(errors[0]) == {"code", "latency_ms", "window_mode"}
    assert errors[0]["code"] == "invalid_json"
    assert secret_output not in repr(record.events)
    assert not (record.path / "judge_shadow.jsonl").exists()


def test_timeout_error_is_sanitized_and_does_not_end_worker(tmp_path):
    record = SpyRecord(tmp_path / "recording")
    responses = iter(((None, "timeout"), (valid_response(), None)))
    judge = DtmfJudge(
        record=record,
        task_goal="查询套餐余量",
        ledger=DtmfActionLedger(),
        model="qwen-plus",
        window_mode="merged",
        model_call=lambda messages, model, timeout: next(responses),
        throttle_seconds=0.01,
    )
    judge.start()
    judge.submit_remote_transcript("第一段", t_ms=10.0)
    wait_until(lambda: any(kind == "judge_error" for kind, _ in record.events))
    judge.submit_remote_transcript("第二段", t_ms=20.0)
    wait_until(lambda: any(kind == "dtmf_judge" for kind, _ in record.events))
    judge.stop()

    assert next(fields for kind, fields in record.events if kind == "judge_error")[
        "code"
    ] == "timeout"


def test_model_call_exceeding_timeout_is_abandoned_without_blocking_worker(tmp_path):
    record = SpyRecord(tmp_path / "recording")
    release = threading.Event()

    def slow_call(messages, model, timeout):
        release.wait(timeout=1)
        return valid_response(), None

    judge = DtmfJudge(
        record=record,
        task_goal="查询套餐余量",
        ledger=DtmfActionLedger(),
        model="qwen-plus",
        window_mode="fragmented",
        model_call=slow_call,
        throttle_seconds=0.01,
        timeout_seconds=0.03,
    )
    judge.start()
    judge.submit_remote_transcript("请按一", t_ms=10.0)

    wait_until(lambda: any(kind == "judge_error" for kind, _ in record.events))
    judge.stop()
    release.set()

    error = next(fields for kind, fields in record.events if kind == "judge_error")
    assert error["code"] == "timeout"


def test_blocked_judge_does_not_block_submit_or_stop_and_stale_result_is_dropped(
    tmp_path,
):
    record = SpyRecord(tmp_path / "recording")
    entered = threading.Event()
    release = threading.Event()

    def blocked_call(messages, model, timeout):
        entered.set()
        release.wait(timeout=10)
        return valid_response(), None

    judge = DtmfJudge(
        record=record,
        task_goal="查询套餐余量",
        ledger=DtmfActionLedger(),
        model="qwen-plus",
        window_mode="fragmented",
        model_call=blocked_call,
        throttle_seconds=0.01,
    )
    judge.start()
    started = time.monotonic()
    judge.submit_remote_transcript("请按一", t_ms=10.0)
    assert time.monotonic() - started < 0.05
    assert entered.wait(timeout=1)

    stopped_at = time.monotonic()
    judge.stop(join_timeout=0.05)
    assert time.monotonic() - stopped_at < 0.2
    release.set()
    time.sleep(0.05)

    assert record.events == []
    assert not (record.path / "judge_shadow.jsonl").exists()


def test_gitignore_explicitly_covers_private_judge_file():
    root = Path(__file__).resolve().parents[2]
    text = (root / ".gitignore").read_text(encoding="utf-8")
    assert "judge_shadow.jsonl" in text
