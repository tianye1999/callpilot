"""CallTools 单测：4 个通话工具的成功/拒绝路径（FakeModem 驱动，无硬件）。

延迟挂断的 Timer/世代号机制在 CallSession（见 test_call_wiring），
这里只验证 hangup 工具通过 ``schedule_hangup`` 回调触发。
"""

from __future__ import annotations

import asyncio
import logging
import time

from fakes import FakeModem

from agentcall.agents.tools import SEND_DTMF_SPEC
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
    send_dtmf=None,
    effect_guard=None,
    direction=None,
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
        send_dtmf=send_dtmf,
        effect_guard=effect_guard,
        direction=direction,
    )
    return tools, modem, hangups


# ---- 注册表：4 个工具全部就位 ----

def test_register_exposes_base_tools():
    tools, _, _ = make_tools()
    registry = tools.register()
    names = {spec["function"]["name"] for spec in registry.specs()}
    # wait_for_sms 与 query_verification_code 同门（WIL-120 三期，同一开关）。
    assert names == {
        "send_sms", "hangup_call", "query_verification_code",
        "wait_for_sms", "send_dtmf",
    }


def test_send_dtmf_tool_description_requires_silent_execution():
    description = SEND_DTMF_SPEC["function"]["description"]

    assert "不要口头宣布按键动作" in description
    assert "发送后保持沉默" in description


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


# ---- to="owner"：系统解析机主号码（WIL-116） ----

def test_send_sms_owner_token_resolves_to_configured_number(monkeypatch):
    """模型只写 owner，真实号码由系统从 OWNER_PHONE 解析；工具结果也只回传
    owner 标记——结果回流进模型上下文，回号码等于送到模型嘴边念给来电者。"""
    monkeypatch.setenv("OWNER_PHONE", "+16505550100")
    tools, modem, _ = make_tools(
        caller="13800000000", direction="inbound", sms_gate=lambda n: True
    )

    result = tools._send_sms({"to": "Owner", "content": "张伟找您，说合同的事"})

    assert result["success"] is True
    number, text = modem.calls[0][1]
    assert number == "+16505550100"
    assert "13800000000" in text  # 转告机主仍要带来电号码
    assert result["to"] == "owner"
    assert "+16505550100" not in repr(result)  # 机主号码不回流给模型


def test_send_sms_owner_token_tolerates_quotes(monkeypatch):
    """提示词里 owner 带引号展示，模型照抄引号也必须认。"""
    monkeypatch.setenv("OWNER_PHONE", "18800000000")
    tools, modem, _ = make_tools(sms_gate=lambda n: True)

    assert tools._send_sms({"to": '"owner"', "content": "hi"})["success"] is True
    assert modem.calls[0][1][0] == "18800000000"


def test_send_sms_owner_token_fails_clearly_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OWNER_PHONE", raising=False)
    record = SpyRecord()
    tools, modem, _ = make_tools(caller="13800000000", record=record)

    result = tools._send_sms({"to": "owner", "content": "hi"})

    assert result["success"] is False
    assert "OWNER_PHONE" in result["message"]
    assert modem.calls == []
    audits = [fields for typ, fields in record.events if typ == "tool_call"]
    assert audits[0]["args"]["to"] == "owner"


def test_send_sms_owner_phone_human_format_sanitized(monkeypatch):
    """OWNER_PHONE 填人类格式（空格/括号/连字符）也能拨——清洗后再校验。"""
    monkeypatch.setenv("OWNER_PHONE", "+1 (650) 555-0100")
    tools, modem, _ = make_tools(sms_gate=lambda n: True)

    assert tools._send_sms({"to": "owner", "content": "hi"})["success"] is True
    assert modem.calls[0][1][0] == "+16505550100"


def test_send_sms_owner_phone_invalid_format_rejected(monkeypatch):
    """清洗后仍不成号码形状的 OWNER_PHONE 要报格式错误，而不是发到 AT 层才失败。"""
    monkeypatch.setenv("OWNER_PHONE", "not-a-phone")
    tools, modem, _ = make_tools(sms_gate=lambda n: True)

    result = tools._send_sms({"to": "owner", "content": "hi"})

    assert result["success"] is False
    assert "格式无效" in result["message"]
    assert modem.calls == []


def test_send_sms_non_numeric_to_error_teaches_owner_token():
    """to 既不是号码也不是 owner 标记（如「机主」）时，错误信息要教会模型正确写法。"""
    tools, modem, _ = make_tools(caller="13800000000")

    result = tools._send_sms({"to": "机主", "content": "hi"})

    assert result["success"] is False
    assert "owner" in result["message"]
    assert modem.calls == []


# ---- 转给机主的短信必须带来电号码（且只有转给机主的才带） ----

def make_relay_tools(monkeypatch, caller="13800000000", owner="18800000000"):
    monkeypatch.setenv("OWNER_PHONE", owner)
    return make_tools(caller=caller, direction="inbound", sms_gate=lambda n: True)


def test_relay_sms_appends_caller_number(monkeypatch):
    """转给机主的短信要带来电号码，否则机主不知道该回给谁（真机 2026-08-14）。"""
    tools, modem, _ = make_relay_tools(monkeypatch)

    result = tools._send_sms({"to": "owner", "content": "张伟找您，说合同的事"})

    assert result["success"] is True
    text = modem.calls[0][1][1]
    assert text.startswith("张伟找您，说合同的事")
    assert "13800000000" in text
    assert result["content"] == text  # 回给模型的也是真正发出去的正文


def test_explicit_owner_number_also_counts_as_relay(monkeypatch):
    """模型直接填机主号码（而非 owner 标记）同样算中继，国家码形变也认。"""
    tools, modem, _ = make_relay_tools(monkeypatch, owner="+8618800000000")

    result = tools._send_sms({"to": "18800000000", "content": "张伟找您"})

    assert "13800000000" in modem.calls[0][1][1]
    assert result["to"] == "18800000000"  # 模型自己给的号码不用打码


def test_third_party_sms_never_carries_caller_number(monkeypatch):
    """发给第三方的短信绝不附来电号码——那是把来电者的号码泄露给无关的人。"""
    tools, modem, _ = make_relay_tools(monkeypatch)

    tools._send_sms({"to": "19900000000", "content": "地址是人民路 1 号"})

    assert modem.calls[0][1][1] == "地址是人民路 1 号"


def test_caller_line_language_follows_body_script(monkeypatch):
    """追加行跟正文字符集走：给 ASCII 正文追中文会把整条短信翻成 UCS2（70 字上限）。"""
    tools, modem, _ = make_relay_tools(monkeypatch, caller="+16505550100")

    tools._send_sms({"to": "owner", "content": "Wei from Acme called."})
    tools._send_sms({"to": "owner", "content": "张伟来电找您"})

    en_text = modem.calls[0][1][1]
    assert en_text.endswith("(Caller: +16505550100)")
    assert en_text.isascii()  # 不因追加行翻成 UCS2
    assert modem.calls[1][1][1].endswith("（来电号码：+16505550100）")


def test_reply_to_caller_keeps_content_untouched(monkeypatch):
    """回给来电者本人时不标注来源——对方当然知道自己是谁。"""
    tools, modem, _ = make_relay_tools(monkeypatch)

    tools._send_sms({"content": "地址是人民路 1 号"})

    assert modem.calls[0][1][1] == "地址是人民路 1 号"


def test_outbound_call_sms_keeps_content_untouched(monkeypatch):
    """外呼时对端不是「来电」，这行措辞不成立。"""
    monkeypatch.setenv("OWNER_PHONE", "18800000000")
    tools, modem, _ = make_tools(
        caller="10086", direction="outbound", sms_gate=lambda n: True
    )

    tools._send_sms({"to": "owner", "content": "客服说下月生效"})

    assert modem.calls[0][1][1] == "客服说下月生效"


def test_relay_dedup_tolerates_number_format_variants(monkeypatch):
    """CLIP 是 +86 全格式、模型写裸号码：算已含，不重复追加。"""
    tools, modem, _ = make_relay_tools(monkeypatch, caller="+8613800000000")

    tools._send_sms({"to": "owner", "content": "张伟找您，回拨 13800000000"})

    assert modem.calls[0][1][1] == "张伟找您，回拨 13800000000"


def test_relay_dedup_respects_digit_boundaries(monkeypatch):
    """短号是长数字的子串时不算已含：10086 不能被「100863 元」骗过。"""
    tools, modem, _ = make_relay_tools(monkeypatch, caller="10086")

    tools._send_sms({"to": "owner", "content": "话费余额 100863 元，来电想聊套餐"})

    assert modem.calls[0][1][1].endswith("（来电号码：10086）")


def test_relay_without_caller_id_sends_without_line(monkeypatch):
    """隐藏号码来电（CLIP 为空）：正常发送，不编造号码行。"""
    monkeypatch.setenv("OWNER_PHONE", "18800000000")
    tools, modem, _ = make_tools(
        caller=None, direction="inbound", sms_gate=lambda n: True
    )

    result = tools._send_sms({"to": "owner", "content": "有人来电找您，没留姓名"})

    assert result["success"] is True
    assert modem.calls[0][1][1] == "有人来电找您，没留姓名"


def test_empty_content_still_rejected_on_owner_relay(monkeypatch):
    """补号码不能把空正文补成非空，绕过空内容校验。"""
    tools, modem, _ = make_relay_tools(monkeypatch)

    assert tools._send_sms({"to": "owner", "content": "  "})["success"] is False
    assert modem.calls == []


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

def test_send_dtmf_dispatch_redacts_logs_result_and_audit(caplog):
    modem = FakeModem()
    sent: list[str] = []
    modem.send_dtmf = lambda digits: sent.append(digits) or True  # type: ignore[attr-defined]
    record = SpyRecord()
    tools, _, _ = make_tools(modem=modem, record=record)

    with caplog.at_level(logging.INFO):
        result = tools.register().dispatch("send_dtmf", {"digits": "103#"})

    assert result["success"] is True
    assert result["count"] == 4
    assert result["mode"] == "qvts"
    assert "digits" not in result
    assert "103#" not in result["message"]
    assert "103#" not in caplog.text
    assert "count=4" in caplog.text
    assert "mode=qvts" in caplog.text
    assert "result=success" in caplog.text
    assert sent == ["103#"]
    assert record.events == [
        ("dtmf", {"count": 4, "mode": "qvts", "result": "success"})
    ]  # 审计日志


def test_send_dtmf_audit_does_not_persist_plaintext_digits():
    record = SpyRecord()
    tools, _, _ = make_tools(record=record)

    tools._send_dtmf({"digits": "103#"})

    _event_type, fields = record.events[0]
    assert fields["count"] == 4
    assert "digits" not in fields
    assert "103#" not in repr(fields)


def test_send_dtmf_uses_injected_session_sender_and_logs_mode():
    record = SpyRecord()
    sent: list[str] = []
    tools, modem, _ = make_tools(
        record=record,
        send_dtmf=lambda digits: sent.append(digits) or (True, "inband"),
    )

    result = tools._send_dtmf({"digits": "9"})

    assert result["success"] is True
    assert sent == ["9"]
    assert modem.calls == []
    assert record.events == [
        ("dtmf", {"count": 1, "mode": "inband", "result": "success"})
    ]


def test_send_dtmf_rejects_empty_digits():
    tools, modem, _ = make_tools()
    assert tools._send_dtmf({"digits": ""})["success"] is False
    assert modem.calls == []


def test_send_dtmf_failure_dispatch_redacts_logs_result_and_audit(caplog):
    modem = FakeModem()
    modem.send_dtmf = lambda digits: (_ for _ in ()).throw(RuntimeError("AT 超时"))  # type: ignore[attr-defined]
    record = SpyRecord()
    tools, _, _ = make_tools(modem=modem, record=record)

    with caplog.at_level(logging.INFO):
        result = tools.register().dispatch("send_dtmf", {"digits": "73#"})

    assert result["success"] is False
    assert result["count"] == 3
    assert result["mode"] == "qvts"
    assert "digits" not in result
    assert "73#" not in result["message"]
    assert "73#" not in caplog.text
    assert "count=3" in caplog.text
    assert "mode=qvts" in caplog.text
    assert "result=failure" in caplog.text
    assert record.events == [
        ("dtmf", {"count": 3, "mode": "qvts", "result": "failure"})
    ]


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


def test_stale_agent_generation_rejects_every_tool_before_side_effect() -> None:
    hub = make_hub()
    hub.publish({"type": "sms_in", "sender": "95588", "text": "验证码 482913"})
    record = SpyRecord()
    tools, modem, hangups = make_tools(
        hub=hub,
        caller="13800000000",
        record=record,
        effect_guard=lambda: False,
    )
    registry = tools.register()

    results = [
        registry.dispatch("send_sms", {"content": "late"}),
        registry.dispatch("hangup_call", {}),
        registry.dispatch("send_dtmf", {"digits": "1"}),
        registry.dispatch("query_verification_code", {}),
    ]

    assert all(result["success"] is False for result in results)
    assert {result["code"] for result in results} == {"STALE_AGENT_GENERATION"}
    assert results[2]["count"] == 1
    assert results[2]["mode"] == "unknown"
    assert modem.calls == []
    assert hangups == []
    assert record.events == []


# ---- WIL-112：工具描述随 AGENT_LANGUAGE 本地化 ----

def test_tool_specs_localized_to_english(monkeypatch):
    """规格里的中文描述会随 session 发给模型，英文场景下会把模型带偏说中文
    （真机 2026-08-12：机主配英文、对端说英文，AI 却用中文开口）。"""
    monkeypatch.setenv("AGENT_LANGUAGE", "en")
    from agentcall.agents.tools import SEND_SMS_SPEC, ToolRegistry

    registry = ToolRegistry()
    registry.register(SEND_SMS_SPEC, lambda args: {})
    spec = registry.specs()[0]["function"]
    assert "Send an SMS" in spec["description"]
    assert "收件" not in str(spec)
    # 原规格常量不得被就地改动（多处共享）
    assert "收件手机号码" in str(SEND_SMS_SPEC)


def test_tool_specs_keep_chinese_by_default(monkeypatch):
    monkeypatch.setenv("AGENT_LANGUAGE", "zh")
    from agentcall.agents.tools import SEND_SMS_SPEC, ToolRegistry

    registry = ToolRegistry()
    registry.register(SEND_SMS_SPEC, lambda args: {})
    assert "收件手机号码" in str(registry.specs()[0])


# ---- WIL-120 二期：ask_owner 机主确认环 ----


def test_ask_owner_registered_only_for_outbound():
    outbound, _, _ = make_tools(direction="outbound")
    assert "ask_owner" in outbound.register()._tools
    inbound, _, _ = make_tools(direction="inbound")
    assert "ask_owner" not in inbound.register()._tools


def test_ask_owner_approved_and_declined(monkeypatch):
    from agentcall import notification_events

    feed = notification_events.NotificationFeed()
    monkeypatch.setattr(notification_events, "feed", feed)
    monkeypatch.setenv("OWNER_CONFIRM_TIMEOUT_SECONDS", "5")
    hub = make_hub()
    record = SpyRecord()

    # 先把答复灌进 history：wait_for_event 先扫历史，立即命中不真等。
    import threading

    def answer(choice):
        def worker():
            deadline = time.time() + 2.0
            while time.time() < deadline:
                history = hub.history()
                answered = {
                    e.get("id") for e in history
                    if e.get("type") == "owner_confirm_response"
                }
                pending = [
                    e for e in history
                    if e.get("type") == "owner_confirm_request"
                    and e.get("id") not in answered
                ]
                if pending:
                    hub.publish({
                        "type": "owner_confirm_response",
                        "id": pending[-1]["id"],
                        "choice": choice,
                    })
                    return
                time.sleep(0.02)
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t

    tools, _, _ = make_tools(hub=hub, record=record, direction="outbound")
    registry = tools.register()

    t = answer("approve")
    result = registry.dispatch("ask_owner", {"question": "月费 55 刀方案，接受吗？"})
    t.join()
    assert result["success"] is True and result["decision"] == "approved"

    t = answer("decline")
    result = registry.dispatch("ask_owner", {"question": "月费 70 刀方案，接受吗？"})
    t.join()
    assert result["decision"] == "declined"

    # 关闭事件总会广播（UI 据此收卡）。
    closed = [e for e in hub.history() if e.get("type") == "owner_confirm_closed"]
    assert len(closed) == 2
    # 审计不含 question 原文（同 WIL-95 §7 口径）。
    audits = [f for (etype, f) in record.events if etype == "tool_call"
              and f.get("tool") == "ask_owner"]
    assert audits and all("55" not in str(f) for f in audits)
    assert [e["kind"] for e in feed.read(0)["events"]] == ["confirm", "confirm_closed"] * 2
    assert feed.read(0)["confirmations"] == []


def test_ask_owner_timeout_is_declined_fail_closed(monkeypatch):
    from agentcall import notification_events

    feed = notification_events.NotificationFeed()
    monkeypatch.setattr(notification_events, "feed", feed)
    monkeypatch.setenv("OWNER_CONFIRM_TIMEOUT_SECONDS", "1")
    hub = make_hub()
    tools, _, _ = make_tools(hub=hub, direction="outbound")
    started = time.monotonic()
    result = tools.register().dispatch("ask_owner", {"question": "x"})
    assert time.monotonic() - started >= 0.9
    assert result["success"] is True and result["decision"] == "timeout"
    assert [e["kind"] for e in feed.read(0)["events"]] == ["confirm", "confirm_closed"]
    assert feed.read(0)["confirmations"] == []


def test_ask_owner_rejects_empty_question():
    tools, _, _ = make_tools(hub=make_hub(), direction="outbound")
    result = tools.register().dispatch("ask_owner", {"question": "  "})
    assert result["success"] is False


# ---- WIL-120 三期：wait_for_sms ----


def test_wait_for_sms_returns_new_message_and_ignores_old(monkeypatch):
    monkeypatch.setenv("WAIT_SMS_TIMEOUT_SECONDS", "2")
    hub = make_hub()
    # 通话前的旧短信：绝不能被当成「刚到的」返回。
    hub.publish({"type": "sms_in", "ts": time.time() - 300,
                 "sender": "10086", "text": "旧验证码 111111"})
    tools, _, _ = make_tools(hub=hub, direction="outbound")
    registry = tools.register()

    import threading

    def deliver():
        time.sleep(0.3)
        hub.publish({"type": "sms_in", "ts": time.time(),
                     "sender": "10086", "text": "您的验证码是 654321"})

    t = threading.Thread(target=deliver, daemon=True)
    t.start()
    result = registry.dispatch("wait_for_sms", {})
    t.join()
    assert result["success"] is True
    assert result["code"] == "654321"
    assert result["sender"] == "10086"


def test_wait_for_sms_times_out_without_new_message(monkeypatch):
    monkeypatch.setenv("WAIT_SMS_TIMEOUT_SECONDS", "1")
    hub = make_hub()
    hub.publish({"type": "sms_in", "ts": time.time() - 300,
                 "sender": "10086", "text": "历史短信 999999"})
    tools, _, _ = make_tools(hub=hub, direction="outbound")
    result = tools.register().dispatch("wait_for_sms", {})
    assert result["success"] is False
