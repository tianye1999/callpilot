"""Trusted-result verification for carrier account enquiries (#86)."""

from agentcall.result_verification import (
    apply_carrier_sms_verification,
    carrier_sms_evidence,
    is_carrier_service_call,
)


def test_carrier_sms_evidence_requires_sender_and_call_window_match():
    events = [
        {"type": "sms_in", "sender": "10086", "text": "old", "ts": 99.0},
        {"type": "sms_in", "sender": "10010", "text": "wrong carrier", "ts": 101.0},
        {"type": "sms_out", "sender": "10086", "text": "wrong direction", "ts": 102.0},
        {"type": "sms_in", "sender": " 10086 ", "text": "账单金额 29.00 元", "ts": 103.0},
        {"type": "sms_in", "sender": "carrier10086", "text": "spoof", "ts": 104.0},
        {"type": "sms_in", "sender": "10086", "text": "blank ignored", "ts": 106.0},
    ]
    events[-1]["text"] = ""

    evidence = carrier_sms_evidence(
        events,
        service_number="10086",
        started_at=100.0,
        ended_at=105.0,
    )

    assert evidence == [
        {"sender": "10086", "text": "账单金额 29.00 元", "ts": 103.0}
    ]


def test_carrier_service_call_requires_exact_dialed_public_service_number():
    assert is_carrier_service_call(" 10086 ", "10086") is True
    assert is_carrier_service_call("+8610086", "10086") is False
    assert is_carrier_service_call("*10086#", "10086") is False
    assert is_carrier_service_call("carrier10086", "10086") is False
    assert is_carrier_service_call("13900000000", "10086") is False
    assert is_carrier_service_call("10086", "") is False


def test_verified_summary_uses_only_official_sms_as_result():
    transcript_result = {
        "ok": True,
        "caller_identity": "中国移动客服",
        "intent": "查询话费",
        "urgency": "低",
        "callback_needed": False,
        "summary": "听写结果是 19.00 元，余额 11.50 元。",
        "error": None,
    }
    evidence = [
        {"sender": "10086", "text": "当月累计话费为29.00元，余额41.40元。", "ts": 103.0}
    ]

    result = apply_carrier_sms_verification(transcript_result, evidence, lang="zh")

    assert result["ok"] is True
    assert result["result_verification"] == "verified"
    assert result["result_source"] == "carrier_sms"
    assert "29.00" in result["summary"] and "41.40" in result["summary"]
    assert "19.00" not in result["summary"] and "11.50" not in result["summary"]
    assert result["evidence"] == evidence


def test_missing_sms_marks_transcript_result_unverified_not_certain():
    transcript_result = {
        "ok": True,
        "caller_identity": "中国移动客服",
        "intent": "查询话费",
        "urgency": "低",
        "callback_needed": False,
        "summary": "听写结果是 19.00 元。",
        "error": None,
    }

    result = apply_carrier_sms_verification(transcript_result, [], lang="zh")

    assert result["ok"] is True
    assert result["result_verification"] == "unverified"
    assert result["result_source"] == "transcript"
    assert result["summary"].startswith("待核实：")
    assert "仅供参考" in result["summary"]
    assert result["evidence"] == []


def test_official_sms_still_produces_verified_result_when_model_failed():
    failed = {"ok": False, "summary": "", "error": "provider unavailable"}
    evidence = [{"sender": "10086", "text": "余额41.40元。", "ts": 103.0}]

    result = apply_carrier_sms_verification(failed, evidence, lang="zh")

    assert result["ok"] is True
    assert result["error"] is None
    assert result["result_verification"] == "verified"
    assert result["summary"] == "已由官方运营商短信核实：余额41.40元。"
