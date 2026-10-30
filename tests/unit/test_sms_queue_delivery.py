"""卡 2 返工：真实状态、SIM 保留以及锁外交付。"""

import threading

import pytest
from test_call_tools import SpyRecord, make_tools
from test_sms_modem_pdu import SmsSerial, wait_until
from test_sms_pdu import as_deliver
from test_web_api import FakeHub, FakeService, api

from agentcall.modem import Eg25Modem
from agentcall.sms_pdu import encode_submit
from agentcall.web.server import build_app


@pytest.fixture
def modem():
    instance = Eg25Modem("unused-fake-port")
    instance._ser = SmsSerial()
    yield instance
    instance.close()


@pytest.mark.parametrize("active,settling,expected", [(True, False, "queued"), (False, True, "queued"),
                                                     (False, False, "failed")])
def test_sms_single_failure_status(modem, active, settling, expected, monkeypatch):
    monkeypatch.setattr(modem, "_send_sms_payload", lambda *args: False)
    modem._hardware_call_present = active
    if settling:
        modem._sms_settle_until = float("inf")
    assert modem.send_sms_status("10086", "one") == expected
    assert len(modem.sms_queue_status()) == (expected == "queued")


@pytest.mark.parametrize("success", [True, False])
def test_sms_tool_queued_and_terminal_events(modem, monkeypatch, success):
    monkeypatch.setenv("SMS_CALL_SETTLE_SECONDS", "0")
    monkeypatch.setattr(modem, "_SMS_RETRY_SECONDS", 0.01)
    modem._call_connected_event.set()
    hub = FakeHub()
    record = SpyRecord()
    tools, _, _ = make_tools(modem=modem, hub=hub, caller="10086", record=record)
    result = tools._send_sms({"content": "中" * 71})
    assert result["success"] and result["queued"]
    assert "已排队" in result["message"] and "0 秒" in result["message"]
    assert hub.events[0]["status"] == "queued"
    assert record.events[-1][1]["result"]["queued"] is True
    if not success:
        monkeypatch.setattr(modem, "_send_sms_payload", lambda *args: False)
    modem._call_connected_event.clear()
    wait_until(lambda: len(hub.events) == 2)
    assert hub.events[1]["status"] == ("sent" if success else "failed")
    assert hub.events[0]["message_id"] == hub.events[1]["message_id"] == result["message_id"]
    assert set(hub.events[1]) <= {"type", "status", "message_id", "number", "text"}


def test_sms_web_queued_status_and_completion(modem, monkeypatch):
    monkeypatch.setenv("SMS_CALL_SETTLE_SECONDS", "0")
    modem._call_connected_event.set()
    hub = FakeHub(history=[{"type": "sms_in", "sender": "10086"}])
    app = build_app(hub=hub, modem=modem, service=FakeService())

    async def request(client):
        response = await client.post("/api/sms/send", json={"number": "10086", "text": "中" * 71})
        assert response.status == 200
        body = await response.json()
        assert body["ok"] and body["queued"] and body["status"] == "queued"
        assert body["message_id"] == hub.events[0]["message_id"]

    api(app, request)
    modem._call_connected_event.clear()
    wait_until(lambda: len(hub.events) == 2)
    assert [event["status"] for event in hub.events] == ["queued", "sent"]
    assert hub.events[0]["message_id"] == hub.events[1]["message_id"]


class StoredSmsSerial(SmsSerial):
    def __init__(self, records):
        super().__init__()
        self.records = records
        self.memory = "SM"
        self.deleted = []

    def write(self, data):
        super().write(data)
        command = data.decode().strip()
        if command.startswith("AT+CPMS="):
            self.memory = command.split('"')[1]
        elif command.startswith("AT+CMGR="):
            pdu = self.records[(self.memory, command.split("=")[1])]
            self.buffer = f"+CMGR: 0,,{len(pdu) // 2 - 1}\r\n{pdu}\r\nOK\r\n".encode()
        elif command == "AT+CMGL=4":
            self.buffer = ("".join(f"+CMGL: {index},0,,{len(pdu) // 2 - 1}\r\n{pdu}\r\n"
                                   for (mem, index), pdu in self.records.items() if mem == self.memory)
                           + "OK\r\n").encode()
        elif command.startswith("AT+CMGD="):
            key = (self.memory, command.split("=")[1])
            self.deleted.append(key)
            self.records.pop(key, None)


def check_callback_lock(modem):
    acquired = threading.Event()

    def competitor():
        with modem._serial_lock:
            acquired.set()

    thread = threading.Thread(target=competitor)
    thread.start()
    try:
        assert acquired.wait(0.3), "短信回调仍持有串口锁"
    finally:
        thread.join(0.3)


def test_sms_delete_all_parts_only_after_success_outside_lock(modem):
    parts = encode_submit("10086", "中" * 71)
    serial = StoredSmsSerial({("SM", "1"): as_deliver(parts[0]), ("ME", "2"): as_deliver(parts[1])})
    modem._ser = serial
    received = []

    def receive(sender, body, timestamp):
        check_callback_lock(modem)
        assert serial.deleted == []
        received.append(body)

    modem.on_sms(receive)
    modem._read_sms("SM", "1")
    assert serial.deleted == [] and received == []
    modem._read_sms("ME", "2")
    assert received == ["中" * 71]
    assert set(serial.deleted) == {("SM", "1"), ("ME", "2")}


def test_sms_restart_backfills_retained_parts(modem):
    parts = encode_submit("10086", "中" * 71)
    records = {("SM", "1"): as_deliver(parts[0])}
    modem._ser = StoredSmsSerial(records)
    modem.on_sms(lambda *args: None)
    modem._read_sms("SM", "1")
    assert records
    modem.close()
    records[("SM", "2")] = as_deliver(parts[1])
    restarted = Eg25Modem("unused-fake-port")
    restarted._ser = StoredSmsSerial(records)
    received = []

    def receive(sender, body, timestamp):
        check_callback_lock(restarted)
        received.append(body)

    restarted.on_sms(receive)
    try:
        restarted._dump_stored_sms()
        assert received == ["中" * 71]
        assert records == {}
    finally:
        restarted.close()


@pytest.mark.parametrize("expire", [False, True])
def test_sms_callback_retry_keeps_sim_until_delivered(modem, expire):
    parts = encode_submit("10086", "中" * 71)
    records = {("SM", str(index)): as_deliver(part) for index, part in enumerate(parts, 1)}
    serial = StoredSmsSerial(records)
    modem._ser = serial

    def fail(*args):
        raise ValueError("模拟交付失败")

    modem.on_sms(fail)
    modem._read_sms("SM", "1")
    if expire:
        modem._sms_concat.timeout = 0
        modem._sms_maintenance()
    else:
        modem._read_sms("SM", "2")
    assert serial.deleted == []
    received = []
    modem.on_sms(lambda sender, body, timestamp: received.append(body))
    modem._sms_maintenance()
    assert len(received) == 1
    assert set(serial.deleted) == ({("SM", "1")} if expire else {("SM", "1"), ("SM", "2")})


def test_sms_nested_serial_transaction_defers_callback(modem):
    pdu = as_deliver(encode_submit("10086", "hello")[0])
    modem._ser = StoredSmsSerial({("SM", "1"): pdu})
    received = []

    def receive(*args):
        check_callback_lock(modem)
        received.append(args)

    modem.on_sms(receive)
    with modem._serial_lock:
        modem._read_sms("SM", "1")
        assert received == []
    modem._sms_maintenance()
    assert len(received) == 1


def test_sms_unsupported_backfill_warns_once_per_storage_slot(modem, caplog):
    # 压缩 DCS、保留编码、奇数长度 UCS2 和截断 PDU 都不可解码。
    prefix = "000005810180F600"
    timestamp = "62010321000000"
    records = {("SM", "1"): prefix + "20" + timestamp + "0141",
               ("SM", "2"): prefix + "0C" + timestamp + "0141",
               ("SM", "3"): prefix + "08" + timestamp + "0141", ("SM", "4"): "00"}
    modem._ser = StoredSmsSerial(records)
    modem._dump_stored_sms()
    modem._dump_stored_sms()
    warnings = [record for record in caplog.records if "PDU" in record.message]
    assert len(warnings) == 4
    assert len(modem._ser.records) == 4
    assert not any(pdu in caplog.text for pdu in records.values() if len(pdu) > 2)


def test_sms_empty_worker_waits_without_timeout(modem, monkeypatch):
    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        modem._closed = True

    monkeypatch.setattr(modem._sms_condition, "wait", wait)
    modem._sms_send_loop()
    assert waits == [None]


def test_sms_dispatch_conservatively_defers_with_lock_wrapper(modem):
    original_lock = modem._serial_lock
    modem._serial_lock = threading.Lock()
    assert modem.send_command("AT+CSQ") == "OK\r\n"
    received = []
    modem.on_sms(lambda sender, body, timestamp: received.append(body))
    from agentcall.sms_pdu import SmsDeliver

    modem._ingest_sms(SmsDeliver("10086", "", "hello"))
    modem._drain_sms_work()
    assert received == []
    modem._serial_lock = original_lock
    modem._drain_sms_work()
    assert received == ["hello"]


def test_sms_single_rejected_in_call_is_sent_after_hangup(modem, monkeypatch):
    monkeypatch.setenv("SMS_CALL_SETTLE_SECONDS", "0")
    modem._ser = SmsSerial(fail_part=1)
    modem._hardware_call_present = True
    events = []
    assert modem.send_sms_status("10086", "single", on_queue_result=events.append) == "queued"
    assert modem._ser.parts == 1
    modem._hardware_call_present = False
    modem._sms_maintenance()
    wait_until(lambda: len(events) == 2)
    assert modem._ser.parts == 2
    assert [event["status"] for event in events] == ["queued", "sent"]


def test_sms_queued_callbacks_do_not_overwrite_other_requests(modem, monkeypatch):
    monkeypatch.setenv("SMS_CALL_SETTLE_SECONDS", "0")
    modem._hardware_call_present = True
    hubs = [FakeHub(), FakeHub()]
    for hub in hubs:
        tools, _, _ = make_tools(modem=modem, hub=hub, caller="10086")
        assert tools._send_sms({"content": "中" * 71})["queued"]
    modem._hardware_call_present = False
    modem._sms_maintenance()
    wait_until(lambda: all(len(hub.events) == 2 for hub in hubs))
    for hub in hubs:
        assert [event["status"] for event in hub.events] == ["queued", "sent"]
        assert hub.events[0]["message_id"] == hub.events[1]["message_id"]
    assert hubs[0].events[0]["message_id"] != hubs[1].events[0]["message_id"]


def test_sms_tool_queued_message_uses_configured_settle(modem, monkeypatch):
    monkeypatch.setenv("SMS_CALL_SETTLE_SECONDS", "12")
    modem._hardware_call_present = True
    tools, _, _ = make_tools(modem=modem, caller="10086")
    result = tools._send_sms({"content": "中" * 71})
    assert "12 秒" in result["message"] and "已发送" not in result["message"]


def test_sms_delete_retry_does_not_repeat_callback(modem):
    pdu = as_deliver(encode_submit("10086", "hello")[0])

    class RejectDeleteSerial(StoredSmsSerial):
        reject = True

        def write(self, data):
            if self.reject and data.startswith(b"AT+CMGD="):
                self.buffer = b"ERROR\r\n"
            else:
                super().write(data)

    serial = RejectDeleteSerial({("ME", "1"): pdu})
    modem._ser = serial
    received = []
    modem.on_sms(lambda sender, body, timestamp: received.append(body))
    modem._read_sms("ME", "1")
    assert received == ["hello"] and serial.records
    serial.reject = False
    modem._sms_maintenance()
    assert received == ["hello"] and serial.records == {}
