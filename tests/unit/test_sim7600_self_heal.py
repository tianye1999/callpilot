"""SIM7600 服务内自愈：假模组/假端口探测，不访问真实串口。"""

from __future__ import annotations

import contextlib
import threading

from agentcall import config, modem_hardware
from agentcall.sim7600_self_heal import Sim7600SelfHeal


class FakeHealModem:
    """只实现自愈序列用到的最小接口；AT 响应全部可注入。"""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self._closed = False
        self._reset_in_progress = False
        self._maintenance_generation = 0
        self._sim_refresh_lock = threading.Lock()
        self._sim_refresh_generation = 0
        self._sim_refresh_requested_generation = None
        self.port = None
        self.run_idle_hardware = None
        self.connect_called = 0
        self.close_transport_calls = 0
        self.refresh_sim_identity_called = 0
        self.transport_open = False
        self.emitted = []

    def reset_transport_access(self):
        return contextlib.nullcontext()

    def _write_command(self, command: str) -> str:
        return self.responses.get(command, "OK")

    def close_reset_transport(self) -> None:
        self.close_transport_calls += 1
        self.transport_open = False
        self._emit_connection_state(False)

    def connect(self) -> None:
        self.connect_called += 1
        self.transport_open = True

    def initialize_for_voice(self, audio_mode: str) -> None:
        pass

    def refresh_sim_identity(self) -> None:
        self.refresh_sim_identity_called += 1

    def start_listener(self) -> None:
        pass

    def _emit_connection_state(self, online: bool) -> None:
        self.emitted.append(online)


class FakeStop:
    def __init__(self):
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def wait(self, seconds: float) -> bool:
        return False

    def set(self) -> None:
        self._set = True


def _registered_responses():
    return {
        "AT": "OK",
        "AT+CPIN?": "+CPIN: READY",
        "AT+CEREG?": "+CEREG: 1",
        "AT+CREG?": "+CREG: 1",
    }


def _detect_sequence(*states):
    queue = iter(states)
    return lambda: next(queue)


def _join(heal: Sim7600SelfHeal) -> None:
    thread = heal._thread
    if thread is not None:
        thread.join(timeout=5)


def test_self_heal_runs_when_idle_and_reports_ok(monkeypatch):
    modem = FakeHealModem(_registered_responses())
    modem.run_idle_hardware = lambda action: (action(), True)[1]
    detect = _detect_sequence((None, None), ("COM5", "COM4"), ("COM5", "COM4"))
    closed_pcm = []
    results = []
    heal = Sim7600SelfHeal(
        modem, "nmea", "COM4", detect, lambda: closed_pcm.append(True), results.append,
    )
    monkeypatch.setattr(heal, "_POLL_SECONDS", 0.001)
    monkeypatch.setattr(heal, "_PORT_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(heal, "_NETWORK_WAIT_SECONDS", 1.0)

    assert heal.request("pcm_degraded") is True
    _join(heal)

    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["reason"] == "pcm_degraded"
    assert closed_pcm == [True]
    assert modem.port == "COM5"
    assert heal.pcm_port == "COM4"
    assert modem.connect_called >= 1
    assert modem.emitted[-1] is True
    assert modem._reset_in_progress is False


def test_self_heal_skips_when_line_busy(monkeypatch):
    modem = FakeHealModem()
    modem.run_idle_hardware = lambda action: False
    results = []
    heal = Sim7600SelfHeal(
        modem, "nmea", "COM4", lambda: (None, None), lambda: None, results.append,
    )
    heal._stop = FakeStop()
    monkeypatch.setattr(heal, "_IDLE_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(heal, "_POLL_SECONDS", 0.001)

    assert heal.request("pcm_degraded") is True
    _join(heal)

    assert len(results) == 1
    assert results[0]["ok"] is False
    assert results[0]["error"] == "line_busy"
    assert modem.close_transport_calls == 0
    assert modem.connect_called == 0


def test_self_heal_throttles_repeat_requests_within_cooldown(monkeypatch):
    modem = FakeHealModem(_registered_responses())
    modem.run_idle_hardware = lambda action: (action(), True)[1]
    detect = _detect_sequence((None, None), ("COM5", "COM4"), ("COM5", "COM4"))
    results = []
    heal = Sim7600SelfHeal(
        modem, "nmea", "COM4", detect, lambda: None, results.append,
    )
    monkeypatch.setattr(heal, "_POLL_SECONDS", 0.001)
    monkeypatch.setattr(heal, "_PORT_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(heal, "_NETWORK_WAIT_SECONDS", 1.0)

    assert heal.request("pcm_degraded") is True
    _join(heal)
    assert heal.request("pcm_degraded") is False


def test_self_heal_port_reappearance_timeout_fails(monkeypatch):
    modem = FakeHealModem()
    modem.run_idle_hardware = lambda action: (action(), True)[1]
    results = []
    heal = Sim7600SelfHeal(
        modem, "nmea", "COM4", lambda: (None, None), lambda: None, results.append,
    )
    heal._stop = FakeStop()
    monkeypatch.setattr(heal, "_POLL_SECONDS", 0.001)
    monkeypatch.setattr(heal, "_PORT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(heal, "_NETWORK_WAIT_SECONDS", 0.01)

    assert heal.request("pcm_degraded") is True
    _join(heal)

    assert results[0]["ok"] is False
    assert results[0]["error"] == "TimeoutError"
    assert modem.close_transport_calls >= 1
    assert modem.emitted[-1] is False


def test_recovery_request_path_is_cross_platform_and_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCALL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(modem_hardware, "LEGACY_RECOVERY_REQUEST_PATH", str(tmp_path / "legacy-request"))
    assert modem_hardware.recovery_request_path() == config.data_dir() / "sim7600-recover-request"
    assert modem_hardware.LEGACY_RECOVERY_REQUEST_PATH == str(tmp_path / "legacy-request")

    written = modem_hardware.write_recovery_request("degraded")
    assert written == modem_hardware.recovery_request_path()
    assert written.exists()
    assert "degraded" in written.read_text(encoding="utf-8")

    modem_hardware.clear_recovery_request()
    assert not written.exists()

def test_self_heal_keeps_transport_open_when_registration_is_slow(monkeypatch):
    modem = FakeHealModem(_registered_responses())
    modem.run_idle_hardware = lambda action: (action(), True)[1]
    detect = _detect_sequence((None, None), ("COM5", "COM4"), ("COM5", "COM4"))
    results = []
    heal = Sim7600SelfHeal(
        modem, "nmea", "COM4", detect, lambda: None, results.append,
    )
    monkeypatch.setattr(heal, "_POLL_SECONDS", 0.001)
    monkeypatch.setattr(heal, "_PORT_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(heal, "_NETWORK_WAIT_SECONDS", 5.0)

    polls = {"n": 0}

    def slow_ready() -> bool:
        polls["n"] += 1
        return polls["n"] >= 3

    monkeypatch.setattr(heal, "_network_ready", slow_ready)

    assert heal.request("pcm_degraded") is True
    _join(heal)

    assert results[0]["ok"] is True
    assert results[0]["transport_ok"] is True
    assert results[0]["registered"] is True
    assert results[0]["transport_elapsed_seconds"] >= 0
    assert results[0]["registration_elapsed_seconds"] >= 0
    assert modem.transport_open is True
    assert modem.close_transport_calls == 1
    assert modem.refresh_sim_identity_called == 1
    assert modem.emitted[-1] is True


def test_self_heal_keeps_transport_open_when_registration_never_completes(monkeypatch):
    modem = FakeHealModem()
    modem.run_idle_hardware = lambda action: (action(), True)[1]
    detect = _detect_sequence((None, None), ("COM5", "COM4"), ("COM5", "COM4"))
    results = []
    heal = Sim7600SelfHeal(
        modem, "nmea", "COM4", detect, lambda: None, results.append,
    )
    monkeypatch.setattr(heal, "_POLL_SECONDS", 0.001)
    monkeypatch.setattr(heal, "_PORT_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(heal, "_NETWORK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(heal, "_network_ready", lambda: False)

    assert heal.request("pcm_degraded") is True
    _join(heal)

    assert results[0]["ok"] is True
    assert results[0]["transport_ok"] is True
    assert results[0]["registered"] is False
    assert modem.transport_open is True
    assert modem.close_transport_calls == 1
    assert modem.refresh_sim_identity_called == 0
    assert modem.emitted[-1] is True



def test_linux_port_detection_keeps_stable_udev_links(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agentcall import call_agent, platforms

    monkeypatch.setattr(platforms, "IS_WINDOWS", False)
    monkeypatch.setattr(call_agent.port_detect, "detect_serial_ports",
                        lambda *_: (_ for _ in ()).throw(AssertionError("Windows-only probe used on Linux")))
    at, pcm = tmp_path / "callpilot-sim7600-at", tmp_path / "callpilot-sim7600-pcm"
    service = SimpleNamespace(modem=SimpleNamespace(port=str(at)), pcm_port=str(pcm))
    detect = call_agent.CallAgentService._detect_sim7600_ports

    assert detect(service) == (None, None)  # links gone during re-enumeration
    at.touch()
    assert detect(service) == (str(at), None)  # PCM link not back yet
    pcm.touch()
    assert detect(service) == (str(at), str(pcm))  # stable paths, never /dev/ttyUSB*
