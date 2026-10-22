"""SMS data still reaches consumers, but neither bodies nor raw replies reach logs."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from agentcall.modem import Eg25Modem, Sim7600Modem

SECRET = "OTP-private-849251\nsecond-private-line"
TIMESTAMP = "26/10/21,12:30:00+32"


def assert_private(caplog):
    for record in caplog.records:
        assert "OTP-private-849251" not in record.getMessage()
        assert "second-private-line" not in record.getMessage()
        assert SECRET not in repr(record.args)
        assert record.exc_info is None


@pytest.mark.parametrize("factory", [Eg25Modem, Sim7600Modem])
@pytest.mark.parametrize("stored", [False, True])
@pytest.mark.parametrize("callback_fails", [False, True])
def test_received_sms_only_logs_metadata(monkeypatch, caplog, factory, stored, callback_fails):
    monkeypatch.setenv("SMS_DELETE_AFTER_INGEST", "true")
    modem = factory("unused-fake-port")
    sent = []
    modem._ser = SimpleNamespace(is_open=True, write=lambda data: sent.append(data.decode("ascii").strip()))
    header = f'+CMGL: 5,"REC READ","10086",,"{TIMESTAMP}"' if stored else (
        f'+CMGR: "REC UNREAD","10086",,"{TIMESTAMP}"'
    )
    raw = f"\r\n{header}\r\n{SECRET}\r\nOK\r\n"
    monkeypatch.setattr(modem, "_read_response", lambda **kwargs: raw if sent[-1].startswith(
        ("AT+CMGR", "AT+CMGL")
    ) else "OK")
    received = []

    def receive(sender, body, timestamp):
        received.append((sender, body, timestamp))
        if callback_fails:
            raise ValueError(raw)

    modem.on_sms(receive)
    with caplog.at_level(logging.DEBUG):
        if stored:
            modem._dump_stored_sms()
        else:
            modem._buffer = '\r\n+CMTI: "SM",5\r\n'
            modem._process_buffer()
    assert received == [("10086", SECRET, TIMESTAMP)]
    assert any(cmd == "AT+CMGD=5" for cmd in sent) is not callback_fails
    assert "10086" in caplog.text and TIMESTAMP in caplog.text
    assert f"字符数={len(SECRET)}" in caplog.text
    assert_private(caplog)


@pytest.mark.parametrize("operation", ["stored", "realtime", "delete"])
def test_sms_read_and_delete_errors_do_not_log_raw_responses(monkeypatch, caplog, operation):
    modem = Eg25Modem("unused-fake-port")

    def fail(_cmd):
        raise RuntimeError(f"+CMGR: response contained {SECRET}")

    monkeypatch.setattr(modem, "_send", fail)
    with caplog.at_level(logging.DEBUG):
        if operation == "stored":
            modem._dump_stored_sms()
        elif operation == "realtime":
            modem._read_sms("SM", "5")
        else:
            modem._delete_stored_sms("5")
    assert caplog.records
    assert_private(caplog)


@pytest.mark.parametrize("text", [SECRET, "验证码 849251"])
def test_sent_sms_does_not_log_body_or_encoded_payload(monkeypatch, caplog, text):
    modem = Eg25Modem("unused-fake-port")
    modem._ser = SimpleNamespace(is_open=True)
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    monkeypatch.setattr(modem, "_send_sms_payload", lambda addr, payload: sent.append((addr, payload)) or True)
    with caplog.at_level(logging.DEBUG):
        assert modem.send_sms("10086", text)
    expected_payload = text if text.isascii() else text.encode("utf-16-be").hex().upper()
    assert sent[0][1] == expected_payload
    assert text not in caplog.text and expected_payload not in caplog.text
    assert "10086" in caplog.text and f"字符数={len(text)}" in caplog.text
    assert_private(caplog)


@pytest.mark.parametrize("name", ["send_sms", "query_verification_code", "wait_for_sms"])
@pytest.mark.parametrize("failure", [False, True])
def test_sms_tool_registry_does_not_log_arguments_results_or_exceptions(caplog, name, failure):
    from agentcall.agents.tools import ToolRegistry

    registry = ToolRegistry()

    def handle(args):
        assert args["content"] == SECRET
        if failure:
            raise RuntimeError(SECRET)
        return {"success": True, "text": SECRET}

    registry.register({"type": "function", "function": {"name": name}}, handle)
    with caplog.at_level(logging.DEBUG):
        result = registry.dispatch(name, {"content": SECRET})
    assert result["success"] is not failure
    assert SECRET in (result["message"] if failure else result["text"])
    assert caplog.records
    assert_private(caplog)


@pytest.mark.parametrize("failure", ["dispatch", "serialize"])
def test_openai_sms_dispatch_errors_do_not_log_body(monkeypatch, caplog, failure):
    from agentcall.agents.openai_agent import OpenAIVoiceAgent
    from agentcall.agents.tools import ToolRegistry

    agent = OpenAIVoiceAgent(api_key="fake-key", model="fake-model", model_display_name="fake-model")
    registry = ToolRegistry()

    def dispatch(*args):
        if failure == "dispatch":
            raise RuntimeError(SECRET)
        return {"success": True, "text": SECRET, "unserializable": object()}

    monkeypatch.setattr(registry, "dispatch", dispatch)
    agent.set_tools(registry)
    sent = []

    async def send(data):
        sent.append(json.loads(data))

    ws = SimpleNamespace(send=send)
    agent._ws = ws
    with caplog.at_level(logging.DEBUG):
        asyncio.run(agent._dispatch_tool_call("send_sms", "fake-call", "{}", ws))
    assert any(event["type"] == "conversation.item.create" for event in sent)
    assert_private(caplog)
