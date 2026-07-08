"""contacts 单测:发短信目标限制(只回复已联系过的号码)。"""

from __future__ import annotations

from agentcall.contacts import is_reply_target_allowed, known_contact_numbers


class FakeHub:
    def __init__(self, events: list | None = None) -> None:
        self._events = events or []

    def history(self) -> list:
        return list(self._events)


class FakeCallLogger:
    def __init__(self, calls: list | None = None) -> None:
        self._calls = calls or []

    def list_calls(self, limit: int = 50) -> list:
        return list(self._calls)


def test_known_numbers_union_of_sms_senders_and_inbound_callers():
    hub = FakeHub([
        {"type": "sms_in", "sender": "13800000000", "text": "hi"},
        {"type": "sms_out", "number": "999", "text": "x"},   # 发出的不算
        {"type": "sms_in", "sender": "", "text": "空发件方"},   # 空 sender 跳过
    ])
    call_logger = FakeCallLogger([
        {"direction": "inbound", "number": "10086"},
        {"direction": "outbound", "number": "13900000000"},   # 外呼不算
        {"direction": "inbound", "number": None},              # 空号码跳过
    ])
    assert known_contact_numbers(hub, call_logger) == {"13800000000", "10086"}


def test_allowed_for_sms_sender_and_inbound_caller_with_strip():
    hub = FakeHub([{"type": "sms_in", "sender": "13800000000", "text": "hi"}])
    call_logger = FakeCallLogger([{"direction": "inbound", "number": "10086"}])
    assert is_reply_target_allowed("13800000000", hub, call_logger)
    assert is_reply_target_allowed("10086", hub, call_logger)
    assert is_reply_target_allowed("  10086  ", hub, call_logger)  # strip 后匹配


def test_rejected_for_unknown_and_empty():
    hub = FakeHub([{"type": "sms_in", "sender": "13800000000", "text": "hi"}])
    call_logger = FakeCallLogger([])
    assert not is_reply_target_allowed("18800000000", hub, call_logger)
    assert not is_reply_target_allowed("", hub, call_logger)
    assert not is_reply_target_allowed("   ", hub, call_logger)


def test_outbound_only_number_not_allowed():
    """只外呼过、从未来电/来信的号码,不自动放行。"""
    call_logger = FakeCallLogger([{"direction": "outbound", "number": "10086"}])
    assert not is_reply_target_allowed("10086", FakeHub([]), call_logger)


def test_extra_allowed_current_caller_bypasses_history():
    """当前通话对端即使不在落盘历史里也放行(通话中可回短信)。"""
    assert is_reply_target_allowed(
        "18800000000", FakeHub([]), FakeCallLogger([]), extra_allowed="18800000000"
    )
    assert not is_reply_target_allowed(
        "18800000000", FakeHub([]), FakeCallLogger([]), extra_allowed="19900000000"
    )


def test_none_sources_reject_but_extra_allowed_still_works():
    assert not is_reply_target_allowed("10086", None, None)
    assert is_reply_target_allowed("10086", None, None, extra_allowed="10086")
