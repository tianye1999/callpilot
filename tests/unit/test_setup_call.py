"""只用内存模组测试向导拨号，不启动服务、不接触串口。"""

import asyncio
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp.test_utils import make_mocked_request
from fakes import FakeModem

from agentcall import config, setup_call
from agentcall.call_agent import CallAgentService, CallSession
from agentcall.call_log import CallLogger
from agentcall.modem import SerialModem, Sim7600Modem
from agentcall.web import server


@pytest.mark.parametrize("token,status,count", [("wrong", 403, 0), ("once", 202, 1)])
def test_setup_call_token_consumed_once(monkeypatch, token, status, count):
    calls = []
    app = {"setup_call_token": ["once"], "service": SimpleNamespace(
        start_setup_test=lambda: (calls.append(True) is None, None))}
    async def body(_):
        return {"token": token, "number": "+12025550123"}
    monkeypatch.setattr(server, "read_json", body)
    request = make_mocked_request("POST", "/api/setup/test_call", app=app)
    assert asyncio.run(server._setup_test_call(request)).status == status
    assert asyncio.run(server._setup_test_call(request)).status == 403
    assert len(calls) == count


@pytest.mark.parametrize("modem_class", [SerialModem, Sim7600Modem])
def test_selftest_modem_dial_never_reconnects_or_retries(monkeypatch, modem_class):
    modem = modem_class("unused")
    writes = []
    def failed_write(command):
        writes.append(command)
        raise OSError("断线")
    monkeypatch.setattr(modem, "_write_command", failed_write)
    monkeypatch.setattr(modem, "_reconnect", lambda: pytest.fail("自测不得重连重拨"))
    with pytest.raises(OSError):
        modem.dial("611", retry=False)
    assert writes == ["ATD611;"]
    with pytest.raises(TimeoutError):
        modem.dial("611", retry=False, deadline=0)
    assert writes == ["ATD611;"]


def admission_service(monkeypatch):
    service = object.__new__(CallAgentService)
    service._remote_setup_lock = threading.Lock()
    service._ring_lock = threading.Lock()
    service._remote_worker = None
    service._remote_call_owner = None
    service._hardware_maintenance = False
    service.call_logger = object()
    service.modem = SimpleNamespace(sim_identity=SimpleNamespace(present=True, service_number="611"))
    service.dial_queue_status = lambda: {}
    service._dial_guard = lambda number: None
    starts = []
    service.session = SimpleNamespace(is_busy=False, start=lambda **kwargs: starts.append(kwargs))
    monkeypatch.setattr(config, "validate_provider_credentials", lambda provider: [])
    return service, starts


def test_setup_call_uses_sim_number_not_dial_queue(monkeypatch):
    service, starts = admission_service(monkeypatch)
    assert service.start_setup_test() == (True, None)
    assert starts == [{"outbound_number": "611", "setup_test": True}]
    assert service.session.setup_test_result == {"state": "running", "number": "611"}


@pytest.mark.parametrize("reason", ["busy", "queue", "remote", "maintenance", "unknown", "offline", "credentials"])
def test_setup_call_rejects_without_start(monkeypatch, reason):
    service, starts = admission_service(monkeypatch)
    if reason == "busy":
        service.session.is_busy = True
    elif reason == "queue":
        service.dial_queue_status = lambda: {"pending": ["611"]}
    elif reason == "remote":
        service._remote_worker = SimpleNamespace(is_running=True)
    elif reason == "maintenance":
        service._hardware_maintenance = True
    elif reason == "unknown":
        service.modem.sim_identity.service_number = ""
    elif reason == "offline":
        service._dial_guard = lambda number: object()
    elif reason == "credentials":
        monkeypatch.setattr(config, "validate_provider_credentials", lambda provider: ["missing"])
    assert service.start_setup_test()[0] is False
    assert starts == []


@pytest.mark.parametrize("asr,energy,written,errors,uplink,received,sent", [
    (False, 2400, 320, 0, 4800, True, True),
    (True, 0, 320, 0, 320, True, True),
    (False, 100, 0, 0, 320, False, False),
    (False, 2400, 320, 1, 4800, True, False),
    (False, 0, 320, 0, 0, False, False),
])
def test_setup_call_result_evidence(asr, energy, written, errors, uplink, received, sent):
    evidence = setup_call.TestEvidence(uplink, energy, written, errors, asr)
    assert evidence.result()["received"]["ok"] is received
    assert evidence.result()["sent"]["ok"] is sent


def session_for_test(tmp_path):
    modem = FakeModem()
    session = CallSession(modem, "unused", None, "nmea", "unused", 115200, 1,
                          call_logger=CallLogger(tmp_path))
    session._outbound_number = "611"
    session._active = True
    return session, modem


def test_setup_call_hard_deadline_forces_hangup_without_retry(monkeypatch, tmp_path):
    session, modem = session_for_test(tmp_path)
    monkeypatch.setattr(setup_call, "MAX_SECONDS", 0.02)
    asyncio.run(setup_call.run_setup_call(session))
    # 20ms 硬截止在慢机器上可能赶在拨号前触发（Windows CI 实测 0 次）；要守的是「截止后不重试」。
    assert modem.call_names().count("dial") <= 1
    assert modem.call_names().count("hangup") >= 1
    assert session.setup_test_result["status"] == "timeout"
    assert session.setup_test_result["sent"]["ok"] is False
    assert session._record.recording_enabled is True


def test_setup_call_watchdog_hangs_up_while_modem_operation_blocks(monkeypatch, tmp_path):
    session, modem = session_for_test(tmp_path)
    hung_up = threading.Event()
    original_hangup = modem.hangup
    def blocked_dial(number, **kwargs):
        assert kwargs["retry"] is False
        modem.calls.append(("dial", (number,)))
        assert hung_up.wait(1), "硬截止必须独立于被取消协程的线程回收"
    def hangup():
        original_hangup()
        hung_up.set()
    monkeypatch.setattr(modem, "dial", blocked_dial)
    monkeypatch.setattr(modem, "hangup", hangup)
    monkeypatch.setattr(setup_call, "MAX_SECONDS", 0.03)
    asyncio.run(setup_call.run_setup_call(session))
    assert hung_up.is_set()
    assert session.setup_test_result["status"] == "timeout"


def test_setup_call_reports_recording_failure_without_staying_running(monkeypatch, tmp_path):
    session, modem = session_for_test(tmp_path)
    monkeypatch.setattr(setup_call, "MAX_SECONDS", 0.02)
    from agentcall.call_log import CallRecord
    monkeypatch.setattr(CallRecord, "finish", lambda self, status: None)
    asyncio.run(setup_call.run_setup_call(session))
    assert "hangup" in modem.call_names()
    assert session.setup_test_result["state"] == "done"
    assert session.setup_test_result["recording_error"] is True
    assert "call_id" not in session.setup_test_result


def test_setup_call_never_dials_after_watchdog_expired(monkeypatch, tmp_path):
    session, modem = session_for_test(tmp_path)
    class ExpiredTimer:
        ident = None
        def __init__(self, seconds, callback):
            self.callback = callback
        def start(self):
            self.callback()
        def cancel(self):
            pass
    monkeypatch.setattr(setup_call.threading, "Timer", ExpiredTimer)
    asyncio.run(setup_call.run_setup_call(session))
    assert "dial" not in modem.call_names()
    assert session.setup_test_result["status"] == "timeout"


@pytest.mark.parametrize("streaming_gap", [False, True])
def test_setup_call_listens_then_speaks_once_and_records(monkeypatch, tmp_path, streaming_gap):
    session, modem = session_for_test(tmp_path)
    monkeypatch.setenv("RECORDING_ENABLED", "false")
    async def connected(timeout):
        return True
    session._wait_connected = connected
    events = []

    class Bridge:
        output_write_errors = 0
        heard = False
        def set_output_observer(self, callback):
            self.observe = callback
        def start(self):
            events.append("bridge")
        def stop(self):
            events.append("stop")
        def read_modem_chunk(self):
            if not self.heard:
                self.heard = True
                events.append("menu")
                return np.full(2400, 100, dtype="<i2").tobytes()
            return b""
        def agent_to_modem(self, pcm, rate):
            return pcm
        def write_modem_chunks(self, chunks):
            for chunk in chunks:
                self.observe(chunk)
        def pending_output_bytes(self):
            return 0

    class Agent:
        output_rate = 8000
        fatal = False
        output_failed = False
        def set_session_instructions(self, task):
            assert task.startswith(setup_call.TEST_TASK)
        def set_transcript_handler(self, callback):
            self.transcript = callback
        def set_output_complete_handler(self, callback):
            self.complete = callback
        async def start(self, callback):
            self.output = callback
        async def say(self, task):
            assert "menu" in events
            events.append("say")
            self.output(b"\x64\x00" * 800)
            async def finish():
                if streaming_gap:
                    await asyncio.sleep(1.1)
                    assert "hangup" not in modem.call_names()
                    self.output(b"\x64\x00" * 800)
                self.complete()
            self.pending = asyncio.create_task(finish())
        async def stop(self):
            await self.pending

    monkeypatch.setattr(setup_call, "create_audio_bridge", lambda **kw: Bridge())
    monkeypatch.setattr(setup_call, "create_agent", lambda provider: Agent())
    asyncio.run(setup_call.run_setup_call(session))
    result = session.setup_test_result
    assert events == ["bridge", "menu", "say", "stop"]
    assert modem.call_names().count("dial") == 1
    assert modem.call_names().count("hangup") == 1
    assert result["received"]["ok"] and result["sent"]["ok"]
    assert (tmp_path / result["call_id"] / "mixed.wav").is_file()
    assert config.get_bool("RECORDING_ENABLED") is False
    meta = json.loads((tmp_path / result["call_id"] / "meta.json").read_text(encoding="utf-8"))
    assert meta["recording_enabled"] is True
    assert meta["answered"] is True
