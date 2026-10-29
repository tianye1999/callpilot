"""SIM7600 native greeting tests: all serial operations are replaced with fakes."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import serial

from agentcall.modem import Eg25Modem, Sim7600Modem


@pytest.fixture
def modem(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "喂")
    monkeypatch.setenv("MODEM_TTS_PARAMS", "2,2,0,1,1,0")
    return Sim7600Modem("unused-fake-port")


def fake_open(monkeypatch, modem, send):
    monkeypatch.setattr(
        "agentcall.modem.serial.Serial", lambda **_kw: SimpleNamespace(is_open=True, close=lambda: None),
    )
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(modem, "_drain", lambda: None)
    monkeypatch.setattr(modem, "_init_sms", lambda: None)
    monkeypatch.setattr(modem, "refresh_sim_identity", lambda **_kw: None)
    monkeypatch.setattr(modem, "_send", send)


@pytest.mark.parametrize("failure", [None, "ERROR", serial.SerialException("private-content")])
def test_greeting_initialized_on_connect_and_reconnect(modem, monkeypatch, caplog, failure):
    sent = []

    def send(cmd):
        sent.append(cmd)
        if cmd.startswith(("AT+CDTAM", "AT+CTTSPARAM")) and failure is not None:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return "OK"

    fake_open(monkeypatch, modem, send)
    with caplog.at_level(logging.WARNING):
        modem.connect()
        modem._running = True
        modem._reconnect()
    assert sent.count("AT+CDTAM=1") == 2
    assert sent.count("AT+CTTSPARAM=2,2,0,1,1,0") == 2
    assert modem.is_connected()
    assert "private-content" not in caplog.text
    if failure is not None:
        assert "CDTAM" in caplog.text and "CTTSPARAM" in caplog.text


@pytest.mark.parametrize("disabled", ["serial", "empty", "quectel"])
def test_greeting_init_disabled_preserves_commands(modem, monkeypatch, disabled):
    if disabled == "serial":
        monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "false")
    elif disabled == "empty":
        monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    else:
        modem = Eg25Modem("unused-fake-port")
    sent = []
    fake_open(monkeypatch, modem, lambda cmd: sent.append(cmd) or "OK")
    modem.connect()
    assert sent == ["AT", "ATE0", "AT+CLIP=1", "AT+QSIMSTAT=1", "AT+CREG=1", "AT+CEREG=1"]


@pytest.mark.parametrize("params", ["", '2,2,0,1,1,0\rATD123;', "invalid"])
def test_empty_or_invalid_tts_params_not_sent(modem, monkeypatch, params):
    monkeypatch.setenv("MODEM_TTS_PARAMS", params)
    sent = []
    fake_open(monkeypatch, modem, lambda cmd: sent.append(cmd) or "OK")
    modem.connect()
    assert "AT+CDTAM=1" in sent
    assert not any(cmd.startswith("AT+CTTSPARAM") for cmd in sent)


@pytest.mark.parametrize("response", ["\r\nOK\r\n", "ERROR", "", serial.SerialException("private-content")])
def test_play_answer_greeting_ucs2_and_nonfatal_failure(modem, monkeypatch, caplog, response):
    monkeypatch.setattr(modem, "_send", lambda _cmd: "OK")
    modem._init_answer_greeting()
    sent = []

    def write(cmd):
        sent.append(cmd)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(modem, "_write_command", write)
    with caplog.at_level(logging.INFO):
        assert modem.play_answer_greeting("喂你好") is (response == "\r\nOK\r\n")
    assert sent == ['AT+CTTS=1,"55824F60597D"']
    assert "耗时" in caplog.text
    assert "5582" not in caplog.text and "喂你好" not in caplog.text
    assert "private-content" not in caplog.text


@pytest.mark.parametrize("text", ["", "\U0001f600", "\ud800"])
def test_non_ucs2_or_empty_greeting_is_nonfatal_without_at(modem, monkeypatch, text):
    monkeypatch.setattr(modem, "_send", lambda _cmd: "OK")
    modem._init_answer_greeting()
    sent = []
    monkeypatch.setattr(modem, "_write_command", lambda cmd: sent.append(cmd) or "OK")
    assert modem.play_answer_greeting(text) is False
    assert sent == []


def test_failed_route_setup_does_not_claim_greeting_success(modem, monkeypatch):
    monkeypatch.setattr(modem, "_send", lambda cmd: "ERROR" if cmd == "AT+CDTAM=1" else "OK")
    modem._init_answer_greeting()
    sent = []
    monkeypatch.setattr(modem, "_write_command", lambda cmd: sent.append(cmd) or "ERROR")
    assert modem.play_answer_greeting("喂") is False
    assert sent == ["AT+CDTAM=1", "AT+CTTSPARAM=2,2,0,1,1,0"]


def test_greeting_lazily_retries_failed_reconnect_configuration_once(modem, monkeypatch):
    monkeypatch.setattr(modem, "_send", lambda _cmd: "ERROR")
    modem._init_answer_greeting()
    sent = []
    monkeypatch.setattr(modem, "_write_command", lambda cmd: sent.append(cmd) or "OK")
    assert modem.play_answer_greeting("喂") is True
    assert sent == ["AT+CDTAM=1", "AT+CTTSPARAM=2,2,0,1,1,0", 'AT+CTTS=1,"5582"']
