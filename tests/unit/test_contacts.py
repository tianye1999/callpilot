"""contacts 单测:发短信目标限制(只回复已联系过的号码)。"""

from __future__ import annotations

from agentcall.contacts import is_reply_target_allowed, known_contact_numbers


class FakeHub:
    def __init__(self, events: list | None = None) -> None:
        self._events = events or []

    def history(self) -> list:
        return list(self._events)


class FakeCallLogger:
    """来电方由 inbound_numbers() 提供(direction 过滤在真实 CallLogger 里做)。"""

    def __init__(self, inbound: list | None = None) -> None:
        self._inbound = set(inbound or [])

    def inbound_numbers(self) -> set:
        return set(self._inbound)


def test_known_numbers_union_of_sms_senders_and_inbound_callers():
    hub = FakeHub([
        {"type": "sms_in", "sender": "13800000000", "text": "hi"},
        {"type": "sms_out", "number": "999", "text": "x"},   # 发出的不算
        {"type": "sms_in", "sender": "", "text": "空发件方"},   # 空 sender 跳过
    ])
    call_logger = FakeCallLogger(["10086"])
    assert known_contact_numbers(hub, call_logger) == {"13800000000", "10086"}


def test_allowed_for_sms_sender_and_inbound_caller_with_strip():
    hub = FakeHub([{"type": "sms_in", "sender": "13800000000", "text": "hi"}])
    call_logger = FakeCallLogger(["10086"])
    assert is_reply_target_allowed("13800000000", hub, call_logger)
    assert is_reply_target_allowed("10086", hub, call_logger)
    assert is_reply_target_allowed("  10086  ", hub, call_logger)  # strip 后匹配


def test_rejected_for_unknown_and_empty():
    hub = FakeHub([{"type": "sms_in", "sender": "13800000000", "text": "hi"}])
    call_logger = FakeCallLogger([])
    assert not is_reply_target_allowed("18800000000", hub, call_logger)
    assert not is_reply_target_allowed("", hub, call_logger)
    assert not is_reply_target_allowed("   ", hub, call_logger)


def test_number_not_in_either_source_rejected():
    """既没发过短信、也不在来电方集合里的号码,不放行。"""
    call_logger = FakeCallLogger(["10086"])
    assert not is_reply_target_allowed("13900000000", FakeHub([]), call_logger)


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
