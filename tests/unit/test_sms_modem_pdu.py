"""短信串口与通话排队验收，全部使用假串口。"""

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_sms_pdu import as_deliver

from agentcall import config
from agentcall.modem import Eg25Modem, Sim7600Modem
from agentcall.sms_concat import MISSING_PART, SmsConcat
from agentcall.sms_pdu import decode_deliver, encode_submit


class SmsSerial:
    def __init__(self, responses=None, fail_part=0):
        self.responses = responses or {}
        self.fail_part = fail_part
        self.parts = 0
        self.writes = []
        self.buffer = b""
        self.is_open = True

    @property
    def in_waiting(self):
        return len(self.buffer)

    def reset_input_buffer(self):
        self.buffer = b""

    def write(self, data):
        self.writes.append(data)
        if data.startswith(b"AT+CMGS="):
            self.buffer = b"\r\n> "
        elif data.endswith(b"\x1a"):
            self.parts += 1
            self.buffer = b"+CMS ERROR: 500\r\n" if self.parts == self.fail_part else b"+CMGS: 1\r\nOK\r\n"
        else:
            self.buffer = self.responses.get(data.decode().strip(), "OK\r\n").encode()

    def read(self, size):
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    def close(self):
        self.is_open = False


@pytest.fixture(params=[Eg25Modem, Sim7600Modem])
def modem(request):
    instance = request.param("unused-fake-port")
    instance._ser = SmsSerial()
    yield instance
    instance.close()


@pytest.mark.parametrize("stored", [False, True])
def test_sms_pdu_receive_reassembles_once_and_deletes(modem, stored, monkeypatch):
    monkeypatch.setenv("SMS_DELETE_AFTER_INGEST", "true")
    text = "前半段 \n" * 25 + "尾部  "
    parts = encode_submit("10086", text, reference=23)
    responses = {}
    rows = []
    for index, part in enumerate(reversed(parts), 1):
        pdu = as_deliver(part)
        responses[f"AT+CMGR={index}"] = f'+CMGR: 0,"",{len(pdu) // 2 - 1}\r\n{pdu}\r\nOK\r\n'
        rows.append(f'+CMGL: {index},0,,{len(pdu) // 2 - 1}\r\n{pdu}\r\n')
    responses["AT+CMGL=4"] = "".join(rows) + "OK\r\n"
    serial = SmsSerial(responses)
    modem._ser = serial
    received = []
    modem.on_sms(lambda sender, body, timestamp: received.append((sender, body, timestamp)))
    if stored:
        modem._dump_stored_sms()
    else:
        for index in range(1, len(parts) + 1):
            modem._read_sms("SM", str(index))
    assert len(received) == 1
    assert received[0][:2] == ("10086", text)
    assert sum(data.startswith(b"AT+CMGD=") for data in serial.writes) == len(parts)


def test_sms_pdu_cmgs_lengths_and_failure(modem):
    serial = modem._ser
    assert modem.send_sms("10086", "中" * 140)
    payloads = [data[:-1] for data in serial.writes if data.endswith(b"\x1a")]
    commands = [data for data in serial.writes if data.startswith(b"AT+CMGS=")]
    assert len(payloads) == 3
    assert commands == [f"AT+CMGS={len(data) // 2 - 1}\r".encode() for data in payloads]
    assert b"AT+CMGF=1\r" not in serial.writes
    assert not any(b"CSCS" in data for data in serial.writes)
    serial = SmsSerial(fail_part=2)
    modem._ser = serial
    assert modem.send_sms("10086", "中" * 140) is False
    assert serial.parts == 2


def wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.005)
    assert predicate()


def test_sms_queue_call_settle_new_call_and_fifo(modem, monkeypatch):
    monkeypatch.setenv("SMS_CALL_SETTLE_SECONDS", "0.06")
    serial = modem._ser
    modem._hardware_call_present = True
    assert modem.send_sms("10086", "single")
    assert serial.parts == 1
    start = time.monotonic()
    assert modem.send_sms("10086", "a" * 161)
    assert modem.send_sms("10086", "中" * 71)
    assert time.monotonic() - start < 0.2
    assert len(modem.sms_queue_status()) == 2
    modem._hardware_call_present = False
    modem._sms_maintenance()
    assert serial.parts == 1
    modem._call_connected_event.set()
    modem._sms_maintenance()
    threading.Event().wait(0.08)
    assert serial.parts == 1
    modem._call_connected_event.clear()
    start = time.monotonic()
    modem._sms_maintenance()
    wait_until(lambda: not modem.sms_queue_status())
    assert time.monotonic() - start >= 0.045
    assert serial.parts == 5
    payloads = [data[:-1].decode() for data in serial.writes if data.endswith(b"\x1a")]
    assert payloads[1:3] == [part.pdu for part in encode_submit("10086", "a" * 161, reference=0)]
    assert payloads[3:] == [part.pdu for part in encode_submit("10086", "中" * 71, reference=1)]


@pytest.mark.parametrize("succeeds", [True, False])
def test_sms_queue_retries_failed_part_once(modem, monkeypatch, succeeds, caplog):
    monkeypatch.setenv("SMS_CALL_SETTLE_SECONDS", "0")
    monkeypatch.setattr(modem, "_SMS_RETRY_SECONDS", 0.02)
    calls = []

    def send(length, payload):
        calls.append(payload)
        return len(calls) == 1 or (succeeds and len(calls) > 2)

    monkeypatch.setattr(modem, "_send_sms_payload", send)
    modem._call_connected_event.set()
    assert modem.send_sms("10086", "中" * 71)
    modem._call_connected_event.clear()
    with caplog.at_level(logging.WARNING):
        wait_until(lambda: not modem.sms_queue_status())
    assert len(calls) == 3
    assert calls[0] != calls[1] == calls[2]
    if not succeeds:
        assert "重试后仍失败" in caplog.text


def test_sms_queue_close_warns_without_body(modem, caplog):
    modem._call_connected_event.set()
    assert modem.send_sms("10086", "PRIVATE" * 30)
    with caplog.at_level(logging.WARNING):
        modem.close()
    assert "未发送短信 1 条" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert modem._sms_thread is not None and not modem._sms_thread.is_alive()


def test_sms_settle_config_matches_env_example(monkeypatch):
    monkeypatch.delenv("SMS_CALL_SETTLE_SECONDS", raising=False)
    spec = config.get_spec("SMS_CALL_SETTLE_SECONDS")
    assert spec.default == "30"
    assert config.get_float(spec.key) == 30
    root = Path(__file__).resolve().parents[2]
    assert f"{spec.key}={spec.default}" in (root / ".env.example").read_text(encoding="utf-8")


def test_sms_expiry_is_delivered_by_poll_maintenance(modem):
    modem._sms_concat = SmsConcat(timeout=0)
    received = []
    modem.on_sms(lambda sender, body, timestamp: received.append(body))
    message = decode_deliver(as_deliver(encode_submit("10086", "中" * 71)[1]))
    assert modem._ingest_sms(message)
    assert received == []
    modem._sms_maintenance()
    assert received == [MISSING_PART + "中" * 4]
    modem._sms_maintenance()
    assert len(received) == 1


def test_sms_callback_failure_keeps_completed_multipart_in_memory(modem):
    def fail(*args):
        raise ValueError("不能丢失已收分段")

    modem.on_sms(fail)
    for part in encode_submit("10086", "中" * 71):
        assert modem._ingest_sms(decode_deliver(as_deliver(part)))
    modem._sms_maintenance()
    assert len(modem._sms_delivery_queue) == 1
    received = []
    modem.on_sms(lambda sender, body, timestamp: received.append(body))
    modem._sms_maintenance()
    assert received == ["中" * 71]
    assert modem._sms_delivery_queue == []


def test_sms_reference_wrap_and_new_call_between_segments(modem, monkeypatch):
    monkeypatch.setenv("SMS_CALL_SETTLE_SECONDS", "0")
    modem._sms_reference = 255
    original = modem._send_sms_payload

    def send(length, payload):
        ok = original(length, payload)
        modem._call_connected_event.set()
        return ok

    monkeypatch.setattr(modem, "_send_sms_payload", send)
    assert modem.send_sms("10086", "中" * 71)
    assert modem._ser.parts == 1
    assert modem.sms_queue_status()[0]["sent_segments"] == 1
    assert modem.send_sms("10086", "a" * 161)
    assert modem._sms_reference == 1
    monkeypatch.setattr(modem, "_send_sms_payload", original)
    modem._call_connected_event.clear()
    wait_until(lambda: not modem.sms_queue_status())
    assert modem._ser.parts == 4
    pdus = [data[:-1].decode() for data in modem._ser.writes if data.endswith(b"\x1a")]
    refs = [decode_deliver(as_deliver(SimpleNamespace(pdu=pdu))).concat.ref for pdu in pdus]
    assert refs == [255, 255, 0, 0]


def test_sms_queue_does_not_wait_for_serial_lock(modem):
    modem._call_connected_event.set()
    completed = threading.Event()
    result = []

    def enqueue():
        result.append(modem.send_sms("10086", "中" * 71))
        completed.set()

    with modem._serial_lock:
        thread = threading.Thread(target=enqueue)
        thread.start()
        try:
            assert completed.wait(0.5)
        finally:
            thread.join(0.5)
    assert result == [True]
    assert modem._ser.writes == []


def test_sms_releases_serial_lock_between_segments(modem, monkeypatch):
    first = threading.Event()
    competitor_done = threading.Event()
    original = modem._send_sms_payload
    segment_calls = []

    def send(length, payload):
        if segment_calls:
            assert competitor_done.is_set()
        segment_calls.append(payload)
        first.set()
        return original(length, payload)

    def competitor():
        assert first.wait(1)
        with modem._serial_lock:
            competitor_done.set()

    monkeypatch.setattr(modem, "_send_sms_payload", send)
    # 直接在段间让出点等竞争者完成，不依赖操作系统调度精度。
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda seconds: competitor_done.wait(0.5))
    thread = threading.Thread(target=competitor)
    thread.start()
    try:
        assert modem.send_sms("10086", "中" * 71)
    finally:
        thread.join(1)
    assert len(segment_calls) == 2


def test_sms_prompt_urc_preserved_after_submission(modem):
    pdu = as_deliver(encode_submit("10086", "received")[0])

    class PromptUrcSerial(SmsSerial):
        def write(self, data):
            super().write(data)
            if data.startswith(b"AT+CMGS="):
                self.buffer = b'\r\n+CMTI: "SM",7\r\n> '

    modem._ser = PromptUrcSerial({"AT+CMGR=7": f"+CMGR: 0,,{len(pdu) // 2 - 1}\r\n{pdu}\r\nOK\r\n"})
    received = []
    modem.on_sms(lambda sender, body, timestamp: received.append(body))
    assert modem.send_sms("10086", "sent")
    assert received == ["received"]
