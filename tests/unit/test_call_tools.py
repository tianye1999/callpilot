"""CallTools 单测：4 个通话工具的成功/拒绝路径（FakeModem 驱动，无硬件）。

延迟挂断的 Timer/世代号机制在 CallSession（见 test_call_wiring），
这里只验证 hangup 工具通过 ``schedule_hangup`` 回调触发。
"""

from __future__ import annotations

import asyncio

from fakes import FakeModem

from agentcall.call_tools import CallTools
from agentcall.events import EventHub


def make_hub() -> EventHub:
    return EventHub(asyncio.new_event_loop())


class SpyRecord:
    """CallRecord 替身：只记录审计事件（与 CallRecord.log_event 同形）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log_event(self, type: str, **fields) -> None:  # noqa: A002
        self.events.append((type, fields))


def make_tools(
    modem: FakeModem | None = None,
    hub: EventHub | None = None,
    caller: str | None = None,
    record: SpyRecord | None = None,
    on_hangup=None,
    sms_gate=None,
) -> tuple[CallTools, FakeModem, list]:
    modem = modem or FakeModem()
    hangups: list[bool] = []
    tools = CallTools(
        modem,  # type: ignore[arg-type]  # FakeModem 与 Eg25Modem 同形
        hub=hub,
        get_caller=lambda: caller,
        get_record=lambda: record,
        schedule_hangup=on_hangup or (lambda: hangups.append(True)),
        is_sms_target_allowed=sms_gate,
    )
    return tools, modem, hangups


# ---- 注册表：4 个工具全部就位 ----

def test_register_exposes_all_four_tools():
    tools, _, _ = make_tools()
    registry = tools.register()
    names = {spec["function"]["name"] for spec in registry.specs()}
    assert names == {"send_sms", "hangup_call", "query_verification_code", "send_dtmf"}


def test_query_code_tool_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TOOL_QUERY_CODE_ENABLED", "false")
    tools, _, _ = make_tools()

    registry = tools.register()

    names = {spec["function"]["name"] for spec in registry.specs()}
    assert "query_verification_code" not in names


# ---- send_sms ----

def test_send_sms_uses_current_caller_when_to_empty():
    tools, modem, _ = make_tools(caller="13800000000")

    result = tools._send_sms({"content": "你好"})

    assert result["success"] is True
    assert result["to"] == "13800000000"
    assert ("send_sms", ("13800000000", "你好")) in modem.calls


def test_send_sms_publishes_sms_out_event():
    hub = make_hub()
    tools, _, _ = make_tools(hub=hub, caller="13800000000")

    tools._send_sms({"content": "你好"})

    events = [e for e in hub.history() if e.get("type") == "sms_out"]
    assert len(events) == 1
    assert events[0]["number"] == "13800000000"
    assert events[0]["text"] == "你好"
    assert events[0]["status"] == "sent"


def test_send_sms_rejects_missing_number_and_empty_content():
    tools, modem, _ = make_tools(caller=None)
    assert tools._send_sms({"content": "你好"})["success"] is False
    assert tools._send_sms({"to": "13800000000", "content": " "})["success"] is False
    assert modem.calls == []  # 拒绝路径不得触发 AT 指令


def test_send_sms_rejected_when_target_not_allowed():
    """网关拒绝(非已联系号码)时:不发送、不触达 AT。"""
    tools, modem, _ = make_tools(caller="13800000000", sms_gate=lambda n: False)

    result = tools._send_sms({"to": "18800000000", "content": "hi"})

    assert result["success"] is False
    assert "只能" in result["message"]
    assert modem.calls == []  # 拦截路径不得触发 AT 指令


def test_send_sms_allowed_when_target_permitted():
    """网关放行时正常发送。"""
    tools, modem, _ = make_tools(sms_gate=lambda n: n == "10086")

    result = tools._send_sms({"to": "10086", "content": "hi"})

    assert result["success"] is True
    assert ("send_sms", ("10086", "hi")) in modem.calls


def test_send_sms_rate_limited_before_modem(monkeypatch):
    monkeypatch.setenv("SMS_RATE_LIMIT_PER_HOUR", "1")
    from agentcall import rate_limit

    rate_limit.reset_sms_rate_limit_state()
    tools, modem, _ = make_tools(sms_gate=lambda n: True)

    assert tools._send_sms({"to": "10086", "content": "hi"})["success"] is True
    result = tools._send_sms({"to": "10086", "content": "again"})

    assert result["success"] is False
    assert "频控" in result["message"]
    assert modem.calls == [("send_sms", ("10086", "hi"))]
    rate_limit.reset_sms_rate_limit_state()


def test_send_sms_rate_limit_zero_unlimited(monkeypatch):
    monkeypatch.setenv("SMS_RATE_LIMIT_PER_HOUR", "0")
    from agentcall import rate_limit

    rate_limit.reset_sms_rate_limit_state()
    tools, modem, _ = make_tools(sms_gate=lambda n: True)

    for i in range(3):
        assert tools._send_sms({"to": "10086", "content": f"hi {i}"})["success"] is True

    assert len(modem.calls) == 3
    rate_limit.reset_sms_rate_limit_state()


def test_tool_calls_write_sanitized_audit_events():
    hub = make_hub()
    hub.publish({"type": "sms_in", "sender": "95588", "text": "您的验证码是 482913"})
    record = SpyRecord()
    tools, _, hangups = make_tools(hub=hub, caller="10086", record=record)

    tools._send_sms({"content": "secret body"})
    tools._hangup({})
    tools._query_code({})

    audits = [fields for typ, fields in record.events if typ == "tool_call"]
    assert [audit["tool"] for audit in audits] == [
        "send_sms",
        "hangup_call",
        "query_verification_code",
    ]
    assert audits[0]["args"] == {"to": "10086", "content_length": 11}
    assert audits[0]["result"] == {"success": True}
    assert audits[1]["args"] == {}
    assert audits[1]["result"] == {"success": True}
    assert audits[2]["result"] == {"success": True, "hit": True}
    assert "secret body" not in str(audits)
    assert "482913" not in str(audits)
    assert hangups == [True]


def test_send_sms_no_gate_allows_all():
    """未注入网关(默认 None)保持旧行为:不限制。"""
    tools, modem, _ = make_tools(caller="18800000000")
    assert tools._send_sms({"content": "hi"})["success"] is True
    assert ("send_sms", ("18800000000", "hi")) in modem.calls


def test_send_sms_failure_reported():
    modem = FakeModem()
    modem.sms_should_succeed = False
    tools, _, _ = make_tools(modem=modem, caller="13800000000")

    result = tools._send_sms({"content": "hi"})
    assert result["success"] is False


def test_send_sms_exception_reported_as_failure():
    modem = FakeModem()
    modem.send_sms = lambda number, text: (_ for _ in ()).throw(RuntimeError("串口断开"))  # type: ignore[method-assign]
    tools, _, _ = make_tools(modem=modem, caller="13800000000")

    result = tools._send_sms({"content": "hi"})
    assert result["success"] is False
    assert "串口断开" in result["message"]


# ---- hangup_call ----

def test_hangup_triggers_schedule_callback():
    tools, _, hangups = make_tools()

    result = tools._hangup({})

    assert result["success"] is True
    assert "挂断" in result["message"]
    assert hangups == [True]  # 只回调排定，不自己管 Timer


# ---- send_dtmf ----

def test_send_dtmf_dispatches_to_modem_and_logs_record():
    modem = FakeModem()
    sent: list[str] = []
    modem.send_dtmf = lambda digits: sent.append(digits) or True  # type: ignore[attr-defined]
    record = SpyRecord()
    tools, _, _ = make_tools(modem=modem, record=record)

    result = tools._send_dtmf({"digits": "103#"})

    assert result["success"] is True
    assert sent == ["103#"]
    assert record.events == [("dtmf", {"digits": "103#"})]  # 审计日志


def test_send_dtmf_rejects_empty_digits():
    tools, modem, _ = make_tools()
    assert tools._send_dtmf({"digits": ""})["success"] is False
    assert modem.calls == []


def test_send_dtmf_exception_reported_as_failure():
    modem = FakeModem()
    modem.send_dtmf = lambda digits: (_ for _ in ()).throw(RuntimeError("AT 超时"))  # type: ignore[attr-defined]
    tools, _, _ = make_tools(modem=modem)

    result = tools._send_dtmf({"digits": "1"})
    assert result["success"] is False
    assert "AT 超时" in result["message"]


# ---- query_verification_code ----

def test_query_code_finds_keyword_sms():
    hub = make_hub()
    hub.publish({"type": "sms_in", "sender": "10086", "text": "余额 1000 元"})
    hub.publish({"type": "sms_in", "sender": "95588", "text": "您的验证码是 482913，5分钟内有效"})
    tools, _, _ = make_tools(hub=hub)

    result = tools._query_code({})

    assert result["success"] is True
    assert result["code"] == "482913"
    assert result["sender"] == "95588"


def test_query_code_falls_back_to_plain_digits():
    hub = make_hub()
    hub.publish({"type": "sms_in", "sender": "10086", "text": "取件码 8842，请尽快取件"})
    tools, _, _ = make_tools(hub=hub)

    result = tools._query_code({})
    assert result["success"] is True
    assert result["code"] == "8842"


def test_query_code_no_sms():
    tools, _, _ = make_tools(hub=make_hub())
    assert tools._query_code({})["success"] is False


def test_query_code_without_hub():
    tools, _, _ = make_tools(hub=None)
    assert tools._query_code({})["success"] is False
