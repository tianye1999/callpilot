"""Low-level D-Bus contracts without importing optional dbus-fast or opening a bus."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentcall.ofono_dbus import OfonoDBus


class FakeMessage:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    @staticmethod
    def new_error(message, name, text):
        return ("error", name)

    @staticmethod
    def new_method_return(message):
        return ("return",)


def adapter():
    transport = OfonoDBus("/agent")
    transport._Message = FakeMessage
    transport._MessageType = SimpleNamespace(SIGNAL="signal", METHOD_CALL="call", ERROR="error")
    transport._owner = ":1.42"
    return transport


def test_fd_index_not_fd_value_and_all_extra_fds_close(monkeypatch):
    transport = adapter()
    accepted, closed = [], []
    transport._audio = lambda *args: accepted.append(args) or True
    monkeypatch.setattr("agentcall.ofono_dbus.os.close", closed.append)
    message = FakeMessage(message_type="call", path="/agent", interface="org.ofono.HandsfreeAudioAgent",
                          member="NewConnection", signature="ohy", sender=":1.42",
                          body=["/card", 1, 1], unix_fds=[50, 51, 52])
    assert transport._handle_message(message) == ("return",)
    assert accepted == [("/card", 51, 1)]
    assert closed == [50, 52]
    transport._loop.close()


@pytest.mark.parametrize("sender,signature,index", [(":1.99", "ohy", 0), (":1.42", "oh", 0),
                                                   (":1.42", "ohy", 9)])
def test_invalid_or_spoofed_agent_method_rejects_and_closes(sender, signature, index, monkeypatch):
    transport = adapter()
    closed = []
    transport._audio = lambda *_: pytest.fail("untrusted fd accepted")
    monkeypatch.setattr("agentcall.ofono_dbus.os.close", closed.append)
    message = FakeMessage(message_type="call", path="/agent", interface="org.ofono.HandsfreeAudioAgent",
                          member="NewConnection", signature=signature, sender=sender,
                          body=["/card", index, 1], unix_fds=[50])
    assert transport._handle_message(message)[0] == "error"
    assert closed == [50]
    transport._loop.close()


def test_signal_sender_and_service_restart_are_verified():
    transport = adapter()
    events = []
    transport._signal = lambda *args: events.append(args)
    signal = FakeMessage(message_type="signal", path="/hfp/phone", interface="org.ofono.Modem",
                         member="PropertyChanged", body=["Online", False], sender=":1.99")
    transport._handle_message(signal)
    assert events == []
    signal.sender = ":1.42"
    transport._handle_message(signal)
    assert events[-1][-1] == ["Online", False]
    signal.sender = "org.freedesktop.DBus"
    signal.member = "NameOwnerChanged"
    signal.body = ["org.ofono", ":1.42", ":1.43"]
    transport._handle_message(signal)
    assert transport._owner == ":1.43" and events[-1][2] == "ServiceChanged"
    transport._loop.close()


def test_async_call_signature_variant_unwrap_and_daemon_error():
    transport = adapter()
    sent = []
    class Bus:
        async def call(self, message):
            sent.append(message)
            return SimpleNamespace(message_type="return", body=[{"Strength": SimpleNamespace(signature="y", value=42)}])
    transport._bus = Bus()
    result = asyncio.run(transport._call("org.ofono", "/", "org.ofono.HandsfreeAudioManager", "Register",
                                        "oay", ["/agent", b"\x01"]))
    assert sent[0].signature == "oay" and sent[0].body == ["/agent", b"\x01"]
    assert result == [{"Strength": 42}]
    async def failed(message):
        return SimpleNamespace(message_type="error", error_name="org.ofono.Error.InUse")
    transport._bus.call = failed
    with pytest.raises(RuntimeError, match="org.ofono.Error.InUse"):
        asyncio.run(transport._call("org.ofono", "/", "org.ofono.HandsfreeAudioManager", "Register"))
    transport._loop.close()


def test_unused_adapter_close_does_not_leak_event_loop():
    transport = OfonoDBus("/agent")
    transport.close()
    transport.close()
    assert transport._loop.is_closed()
