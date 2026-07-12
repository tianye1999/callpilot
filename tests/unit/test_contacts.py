"""contacts 单测:发短信目标限制(只回复已联系过的号码)。"""

from __future__ import annotations

from agentcall.call_log import CallLogger
from agentcall.contacts import is_reply_target_allowed, known_contact_numbers


class FakeHub:
    def __init__(self, events: list | None = None) -> None:
        self._events = events or []

    def history(self) -> list:
        return list(self._events)


class FakeCallLogger:
    """通话白名单来源由真实 CallLogger 负责按方向/接通状态过滤。"""

    def __init__(self, inbound: list | None = None, answered_outbound: list | None = None) -> None:
        self._inbound = set(inbound or [])
        self._answered_outbound = set(answered_outbound or [])

    def inbound_numbers(self) -> set:
        return set(self._inbound)

    def answered_outbound_numbers(self) -> set:
        return set(self._answered_outbound)


def test_known_numbers_union_of_sms_senders_and_inbound_callers():
    hub = FakeHub([
        {"type": "sms_in", "sender": "13800000000", "text": "hi"},
        {"type": "sms_out", "number": "999", "text": "x"},   # 发出的不算
        {"type": "sms_in", "sender": "", "text": "空发件方"},   # 空 sender 跳过
    ])
    call_logger = FakeCallLogger(["10086"], answered_outbound=["10000"])
    assert known_contact_numbers(hub, call_logger) == {"13800000000", "10086", "10000"}


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


def test_allowed_for_answered_outbound_but_not_merely_dialed():
    call_logger = FakeCallLogger(answered_outbound=["10000"])

    assert is_reply_target_allowed("10000", FakeHub([]), call_logger)
    assert not is_reply_target_allowed("10086", FakeHub([]), call_logger)


def test_allowed_for_real_answered_outbound_call_log(tmp_path):
    call_logger = CallLogger(tmp_path / "calls")
    answered = call_logger.begin_call("outbound", "10000")
    answered.log_event("answered")
    answered.finish("completed")
    not_connected = call_logger.begin_call("outbound", "10086")
    not_connected.finish("not_connected")

    assert is_reply_target_allowed("10000", FakeHub([]), call_logger)
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


def test_allow_any_bypasses_contact_check_for_nonempty_number():
    """开发期总开关:allow_any 放行任意非空号码,无需已联系过。"""
    assert is_reply_target_allowed(
        "18800000000", FakeHub([]), FakeCallLogger([]), allow_any=True
    )
    assert is_reply_target_allowed("13900000000", None, None, allow_any=True)


def test_allow_any_still_rejects_empty_number():
    """allow_any 也不放行空号码(空号码一律拒绝)。"""
    assert not is_reply_target_allowed("", None, None, allow_any=True)
    assert not is_reply_target_allowed("   ", None, None, allow_any=True)
