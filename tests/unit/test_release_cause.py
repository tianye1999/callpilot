"""释放原因只走假串口，不操作真实设备。"""

import asyncio
import json
import threading
import time

import pytest
from test_modem_parsing import FakeSerial

from agentcall import config
from agentcall.call_agent import CallSession
from agentcall.call_log import CallLogger
from agentcall.modem import Eg25Modem, Sim7600Modem


class ReleaseSerial(FakeSerial):
    def __init__(self, response):
        super().__init__()
        self.response = response
        self.query_started = threading.Event()
        self.timeout = 0.2
        self.write_timeout = 2

    def write(self, data):
        result = super().write(data)
        if data == b"AT+CEER\r":
            self._pending = self.response
            self.query_started.set()
        return result


def make_modem(monkeypatch, response=b'\r\n+CEER: "User busy"\r\nOK\r\n'):
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "true")
    modem = Eg25Modem("unused")
    modem._ser = ReleaseSerial(response)
    modem.dial("+12025550100")
    return modem


@pytest.mark.parametrize("response", [
    b'\r\n+CEER: "User busy"\r\nOK\r\n',
    b"\r\n+CEER: 0,17\r\nOK\r\n",
    b"\r\nERROR\r\n",
    b"\r\n+CME ERROR: 3\r\n",
])
def test_ceer_preserves_raw_response_once(monkeypatch, response):
    modem = make_modem(monkeypatch, response)
    modem.hangup()
    causes = modem.wait_release_causes()
    assert causes == [{"raw": response.decode(), "source": "ceer"}]
    modem._buffer = "\r\nNO CARRIER\r\n"
    modem._process_buffer()
    modem.hangup()
    assert modem._ser.writes.count("AT+CEER") == 1


def test_ceer_timeout_does_not_block_hangup(monkeypatch):
    modem = make_modem(monkeypatch, b"")
    started = time.monotonic()
    modem.hangup()
    assert time.monotonic() - started < 0.2
    assert modem._ser.query_started.wait(1)
    started = time.monotonic()
    modem.hangup()
    assert time.monotonic() - started < 0.2
    assert modem.wait_release_causes() == []
    assert time.monotonic() - started < 2.1


def test_cend_cause_fragmented_and_no_carrier_share_query(monkeypatch):
    modem = make_modem(monkeypatch)
    modem.on_hangup(modem.hangup)
    modem._buffer = "\r\n+CEND: 1,30,0,"
    modem._process_buffer()
    assert "AT+CEER" not in modem._ser.writes
    modem._buffer += "17\r\nNO CARRIER\r\n"
    modem._process_buffer()
    causes = modem.wait_release_causes()
    assert {"raw": "+CEND: 1,30,0,17", "source": "cend", "cause": "17"} in causes
    assert modem._ser.writes.count("AT+CEER") == 1


def test_release_cause_disabled_keeps_commands_unchanged(monkeypatch):
    modem = make_modem(monkeypatch)
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "false")
    modem._buffer = "\r\n+CEND: 1,30,0,17\r\n"
    modem._process_buffer()
    modem.hangup()
    assert modem._ser.writes == ["ATD+12025550100;", "ATH", "AT+QPCMV=0"]
    assert modem.wait_release_causes() == []


def test_release_cause_defaults_on(monkeypatch):
    monkeypatch.delenv("MODEM_LOG_RELEASE_CAUSE", raising=False)
    assert config.get_bool("MODEM_LOG_RELEASE_CAUSE")


def test_managed_no_carrier_defers_ceer_until_physical_cleanup(monkeypatch):
    modem = make_modem(monkeypatch, b"")
    report = modem.begin_release_capture()
    modem.dial("+12025550100")
    modem._buffer = "\r\nNO CARRIER\r\n"
    modem._process_buffer()
    assert "AT+CEER" not in modem._ser.writes
    started = time.monotonic()
    modem.hangup()
    assert time.monotonic() - started < 0.2
    assert modem._ser.writes[-2:] == ["ATH", "AT+QPCMV=0"]
    assert modem.complete_release_capture(report) == []
    assert modem._ser.writes[-1] == "AT+CEER"


def test_failed_hangup_does_not_disable_retry(monkeypatch):
    modem = make_modem(monkeypatch)
    report = modem.begin_release_capture()
    original_write = modem._ser.write

    def write(data):
        result = original_write(data)
        if data == b"ATH\r" and modem._ser.writes.count("ATH") == 1:
            modem._ser._pending = b"\r\nERROR\r\n"
        return result

    monkeypatch.setattr(modem._ser, "write", write)
    modem.hangup()
    assert not report.hung_up
    modem.hangup()
    assert report.hung_up
    assert modem._ser.writes.count("ATH") == 2
    modem.complete_release_capture(report)


def test_no_carrier_does_not_wait_for_partial_cend(monkeypatch):
    modem = make_modem(monkeypatch)
    report = modem.begin_release_capture()
    ended = threading.Event()
    modem.on_hangup(ended.set)
    modem._buffer = "\r\nNO CARRIER\r\n+CEND: 1,30,0,"
    modem._process_buffer()
    assert ended.is_set()
    modem._buffer += "17\r\n"
    modem._process_buffer()
    modem.hangup()
    assert modem.complete_release_capture(report)[0]["cause"] == "17"


def test_ceer_lock_wait_is_bounded_and_does_not_send_late(monkeypatch):
    modem = make_modem(monkeypatch)
    with modem._serial_lock:
        modem._request_release_cause()
        assert modem._release_report.done.wait(2.1)
        assert "AT+CEER" not in modem._ser.writes
    assert modem.wait_release_causes() == []


def test_previous_call_report_cannot_query_or_contaminate_next_call(monkeypatch):
    modem = make_modem(monkeypatch)
    previous = modem.begin_release_capture()
    modem._buffer = "\r\n+CEND: 1,30,0,17\r\n"
    modem._process_buffer()
    current = modem.begin_release_capture()
    modem.dial("+12025550101")
    assert modem.complete_release_capture(previous) == [
        {"raw": "+CEND: 1,30,0,17", "source": "cend", "cause": "17"},
    ]
    assert "AT+CEER" not in modem._ser.writes
    assert modem.wait_release_causes(current) == []


def test_ceer_preserves_sms_and_pcm_urcs(monkeypatch):
    modem = make_modem(monkeypatch, b'\r\n+CMTI: "SM",1\r\n+QPCMV: 0,0\r\n+CEER: 0,17\r\nOK\r\n')
    sms = threading.Event()
    monkeypatch.setattr(modem, "_read_sms", lambda *args: sms.set())
    modem.hangup()
    modem.wait_release_causes()
    assert sms.wait(1)
    assert not modem.pcm_ready()


def test_sim7600_reset_waits_for_ceer_without_blocking_hangup(monkeypatch):
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "true")
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "true")
    modem = Sim7600Modem("unused")
    modem._ser = ReleaseSerial(b"\r\n+CEER: 0,17\r\nOK\r\n")
    allow_response = threading.Event()
    original_write = modem._ser.write

    def write(data):
        if data == b"AT+CEER\r":
            assert allow_response.wait(1)
        return original_write(data)

    monkeypatch.setattr(modem._ser, "write", write)
    report = modem.begin_release_capture()
    modem._voice_call_armed = True
    modem._call_pcm_enabled = True
    reset = threading.Event()
    modem.on_post_call_reset(reset.set)
    started = time.monotonic()
    modem.hangup()
    assert time.monotonic() - started < 0.2
    assert not reset.is_set()
    # 即使上层尚未 complete，诊断完成也必须放行重启，不能留下永久重启锁。
    allow_response.set()
    assert reset.wait(1)
    assert modem.complete_release_capture(report)[0]["source"] == "ceer"
    assert modem._ser.writes[-1] == "AT+CEER"


def test_disabling_ceer_during_reset_still_resets_once(monkeypatch):
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "true")
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "true")
    modem = Sim7600Modem("unused")
    modem._ser = ReleaseSerial(b"\r\nOK\r\n")
    report = modem.begin_release_capture()
    modem._voice_call_armed = True
    modem._call_pcm_enabled = True
    resets = []
    modem.on_post_call_reset(lambda: resets.append(True))
    request = modem._request_release_cause

    def disable_before_query(report=None):
        if report is not None and report.before_reset:
            monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "false")
        request(report)

    monkeypatch.setattr(modem, "_request_release_cause", disable_before_query)
    modem.hangup()
    assert resets == [True]
    assert modem.complete_release_capture(report) == []
    assert resets == [True]
    assert "AT+CEER" not in modem._ser.writes


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("ending", ["timeout", "NO CARRIER", "+CEND: 1,30,0,17"])
def test_not_connected_records_release_event_and_metrics(monkeypatch, tmp_path, caplog, enabled, ending):
    modem = make_modem(monkeypatch)
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", str(enabled).lower())
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "false")
    session = CallSession(modem, "unused", "qwen", "nmea", "unused", 115200, 1.0,
                          call_logger=CallLogger(tmp_path))
    session._outbound_number = "+12025550100"

    async def not_connected(**kwargs):
        if ending != "timeout":
            modem._buffer = f"\r\n{ending}\r\n"
            modem._process_buffer()
        return False

    monkeypatch.setattr(session, "_wait_connected", not_connected)
    with caplog.at_level("INFO"):
        asyncio.run(session._handle_call())
    path = next(tmp_path.iterdir())
    events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    metrics = json.loads((path / "metrics.json").read_text())
    assert json.loads((path / "meta.json").read_text())["status"] == "not_connected"
    releases = [event for event in events if event["type"] == "release_cause"]
    if enabled:
        assert releases[-1]["raw"] == '\r\n+CEER: "User busy"\r\nOK\r\n'
        assert metrics["release_causes"] == [
            {key: value for key, value in event.items() if key not in {"type", "ts"}}
            for event in releases
        ]
        if ending.startswith("+CEND"):
            assert releases[0]["source"] == "cend"
            assert releases[0]["cause"] == "17"
        assert "外呼未接通，通话释放原因:" in caplog.text
    else:
        assert not releases
        assert "release_causes" not in metrics
        assert "AT+CEER" not in modem._ser.writes
