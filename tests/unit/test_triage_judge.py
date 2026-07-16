import json
import threading
import time

import pytest

from agentcall.triage_judge import (
    InboundTriageJudge,
    TriageJudgeError,
    TriageVerdictConsumer,
    judge_transcript,
    parse_triage_verdict,
)


def _response(**overrides):
    payload = {
        "category": "personal",
        "action": "transfer",
        "confidence": 0.93,
        "reason_code": "owner_requested",
        "turn_id": 1,
        "call_generation": 7,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_verdict_is_strict_and_rejects_duplicate_fields():
    verdict = parse_triage_verdict(_response())
    assert verdict.action == "transfer"
    assert verdict.call_generation == 7
    assert verdict.public_fields()["category"] == "personal"

    with pytest.raises(TriageJudgeError, match="invalid_schema"):
        parse_triage_verdict(_response(extra="leak"))
    with pytest.raises(TriageJudgeError, match="duplicate_fields"):
        parse_triage_verdict(
            '{"category":"personal","action":"transfer","action":"reject",'
            '"confidence":0.9,"reason_code":"owner_requested","turn_id":1,'
            '"call_generation":7}'
        )


def test_judge_transcript_fences_turn_and_generation():
    def stale(_messages, _timeout):
        return _response(turn_id=2), None

    with pytest.raises(TriageJudgeError, match="turn_mismatch"):
        judge_transcript(
            [("user", "找本人")],
            "找本人的都转接",
            turn_id=1,
            call_generation=7,
            model_call=stale,
        )


def test_event_worker_debounces_and_uses_a_per_call_lane():
    seen = []
    ready = threading.Event()

    def model(messages, _timeout):
        payload = json.loads(messages[1]["content"])
        return _response(
            turn_id=payload["turn_id"],
            call_generation=payload["call_generation"],
        ), None

    judge = InboundTriageJudge(
        call_generation=7,
        preference="找本人的都转接",
        on_verdict=lambda verdict, _latency: (seen.append(verdict), ready.set()),
        model_call=model,
        debounce_seconds=0.3,
    )
    judge.start()
    judge.submit_turn("user", "我是老王")
    judge.submit_turn("agent", "请问什么事")
    judge.submit_turn("user", "找本人有急事")
    assert ready.wait(1.0)
    judge.stop()

    assert len(seen) == 1
    assert seen[0].turn_id == 2
    assert seen[0].call_generation == 7


def test_timeout_reports_error_and_never_emits_verdict():
    errors = []
    ready = threading.Event()

    def blocked(_messages, _timeout):
        time.sleep(0.3)
        return _response(call_generation=3), None

    judge = InboundTriageJudge(
        call_generation=3,
        preference="",
        on_verdict=lambda *_args: pytest.fail("timeout must not emit a verdict"),
        on_error=lambda *args: (errors.append(args), ready.set()),
        model_call=blocked,
        debounce_seconds=0.3,
        timeout_seconds=0.05,
    )
    judge.start()
    judge.submit_turn("user", "你好")
    assert ready.wait(1.0)
    judge.stop()
    assert errors[0][0] == "timeout"


def test_new_caller_turn_fences_inflight_older_verdict():
    seen = []
    ready = threading.Event()
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0

    def model(messages, _timeout):
        nonlocal calls
        calls += 1
        payload = json.loads(messages[1]["content"])
        if calls == 1:
            first_started.set()
            release_first.wait(1.0)
        return _response(
            turn_id=payload["turn_id"],
            call_generation=payload["call_generation"],
        ), None

    judge = InboundTriageJudge(
        call_generation=7,
        preference="找本人的都转接",
        on_verdict=lambda verdict, _latency: (seen.append(verdict), ready.set()),
        model_call=model,
        debounce_seconds=0.3,
        timeout_seconds=1.0,
    )
    judge.start()
    judge.submit_turn("user", "我是老王")
    assert first_started.wait(0.8)
    judge.submit_turn("user", "找本人有急事")
    release_first.set()
    assert ready.wait(1.2)
    judge.stop()

    assert calls == 2
    assert [verdict.turn_id for verdict in seen] == [2]


def test_consumer_fences_and_requires_second_reject_confirmation():
    consumer = TriageVerdictConsumer()
    first = parse_triage_verdict(
        _response(
            category="marketing",
            action="reject",
            confidence=0.91,
            turn_id=1,
        )
    )
    stale = parse_triage_verdict(_response(call_generation=6))
    second = parse_triage_verdict(
        _response(
            category="marketing",
            action="reject",
            confidence=0.9,
            turn_id=2,
        )
    )

    assert consumer.consume(stale, current_generation=7).outcome == "ignored"
    assert consumer.consume(first, current_generation=7).outcome == "clarify"
    assert consumer.consume(first, current_generation=7).outcome == "ignored"
    assert consumer.consume(second, current_generation=7).outcome == "reject"


def test_consumer_transfers_at_threshold_without_realtime_discretion():
    consumer = TriageVerdictConsumer()
    verdict = parse_triage_verdict(_response(confidence=0.7))
    result = consumer.consume(verdict, current_generation=7)
    assert result.outcome == "transfer"
    assert result.reason == "threshold_met"
