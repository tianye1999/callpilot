"""Network regression tests use fake transports only."""

import pytest

from agentcall import config
from agentcall.call_agent import CallAgentService
from agentcall.modem import Sim7600Modem
from agentcall.sim7600_reset import Sim7600CallReset
from agentcall.sim_identity import identify, parse_creg


class Clock:
    now = 1000.0

    def monotonic(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


class Serial:
    is_open = True
    timeout = 0.2
    write_timeout = 2.0
    in_waiting = 1

    def __init__(self, clock, responses):
        self.clock = clock
        self.responses = responses
        self.pending = b""
        self.writes = []

    def write(self, data):
        command = data.decode().strip()
        self.writes.append(command)
        self.pending = self.responses.get(command, "").encode()

    def read(self, size):
        self.clock.wait(0.01)
        result, self.pending = self.pending, b""
        return result


def service_with_modem(modem):
    service = CallAgentService.__new__(CallAgentService)
    service.modem = modem
    service.modem_connected = True
    service._hardware_maintenance = False
    service._sim7600_reset = None
    return service


def test_guard_refreshes_stale_cache_and_keeps_healthy_path_fast(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("agentcall.modem.time.monotonic", clock.monotonic)
    modem = Sim7600Modem("fake")
    modem._emit_connection_state(True)
    modem._sim_identity = identify("460001234567890", "+CREG: 1,2", "+CEREG: 1,4")
    modem._ser = Serial(clock, {"AT+CREG?": "+CREG: 1,3\r\nOK\r\n",
                                "AT+CEREG?": "+CEREG: 1,1\r\nOK\r\n"})
    service = service_with_modem(modem)
    assert service._dial_guard("10086") is None
    assert modem.sim_identity.eps_registered
    assert modem._ser.writes == ["AT+CREG?", "AT+CEREG?"]
    assert service._dial_guard("10086") is None
    assert len(modem._ser.writes) == 2
    assert service._dial_guard("10010").code == "SERVICE_NUMBER_MISMATCH"


def test_refresh_preserves_reader_buffer_and_updates_registration(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("agentcall.modem.time.monotonic", clock.monotonic)
    modem = Sim7600Modem("fake")
    modem._emit_connection_state(True)
    modem._sim_identity = identify("460001234567890", "+CREG: 1,2", "+CEREG: 1,4")
    modem._ser = Serial(clock, {
        "AT+CREG?": '+CREG: 1,3\r\nRING\r\n+CLIP: "123456",129\r\nOK\r\n',
        "AT+CEREG?": "+CEREG: 1,1\r\nOK\r\n",
    })
    callers = []
    modem.on_ring(callers.append)
    # Only the reader may mutate this buffer, including when a command consumes URCs.
    modem._buffer = "+CEREG: 1,"
    modem.refresh_registration()
    assert modem._buffer == "+CEREG: 1,"
    assert modem.sim_identity.reg_status == "注册被拒"
    assert modem.sim_identity.eps_registered
    assert callers == []
    # Command responses retain the existing policy of discarding RING/CLIP.
    modem._process_buffer()
    assert modem._buffer == "+CEREG: 1,"
    assert callers == []


def test_registration_timeout_is_shared_and_restores_serial_limits(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("agentcall.modem.time.monotonic", clock.monotonic)
    modem = Sim7600Modem("fake")
    modem._emit_connection_state(True)
    modem._sim_identity = identify("460001234567890", "+CREG: 1,2", "+CEREG: 1,4")
    modem._ser = Serial(clock, {})
    failure = service_with_modem(modem)._dial_guard("10086")
    assert failure.code == "SIM_NOT_REGISTERED"
    assert clock.now <= 1001.02
    assert (modem._ser.timeout, modem._ser.write_timeout) == (0.2, 2.0)
    assert modem._ser.writes == ["AT+CREG?"]


def test_registration_lock_timeout_never_writes(monkeypatch):
    modem = Sim7600Modem("fake")
    timeouts = []

    class BusyLock:
        def acquire(self, *, timeout):
            timeouts.append(timeout)
            return False

    monkeypatch.setattr(modem, "_serial_lock", BusyLock())
    modem.refresh_registration(timeout=0.5)
    assert len(timeouts) == 1 and 0 <= timeouts[0] <= 0.5


def test_partial_query_cannot_mistake_mode_for_registered_status(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("agentcall.modem.time.monotonic", clock.monotonic)
    modem = Sim7600Modem("fake")
    modem._emit_connection_state(True)
    modem._sim_identity = identify("460001234567890", "+CREG: 1,2", "+CEREG: 1,4")
    modem._ser = Serial(clock, {"AT+CREG?": "+CREG: 1,"})
    failure = service_with_modem(modem)._dial_guard("10086")
    assert failure.code == "SIM_NOT_REGISTERED"


@pytest.fixture
def recovery(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("agentcall.sim7600_reset.time.monotonic", clock.monotonic)
    modem = Sim7600Modem("/dev/fake-at")
    modem._emit_connection_state(True)
    controller = Sim7600CallReset(modem, "nmea", "/dev/fake-pcm", lambda online: None)
    controller._deadline = clock.now + 90
    controller._network_mode = True
    monkeypatch.setattr(controller._stop, "wait", clock.wait)
    return modem, controller, clock


@pytest.mark.parametrize("eps,csq,ready", [("1", "25", True), ("5", "25", True),
                                         ("1", "99", False), ("2", "25", False)])
def test_soft_recovery_network_check(recovery, monkeypatch, eps, csq, ready):
    modem, controller, clock = recovery
    commands = []

    def write(command):
        commands.append(command)
        return {"AT+CEREG?": f"+CEREG: 1,{eps}\r\nOK",
                "AT+CSQ": f"+CSQ: {csq},99\r\nOK", "AT+CPIN?": "+CPIN: READY\r\nOK"}.get(command, "OK")

    monkeypatch.setattr(modem, "_write_command", write)
    monkeypatch.setattr(modem, "refresh_sim_identity", lambda: None)
    assert controller._soft_network_reset() is ready
    assert commands[:2] == ["AT+CFUN=0", "AT+CFUN=1"]
    assert clock.now == (1000 if ready else 1030)


def test_claim_checks_hardware_idle_and_cooldown(recovery, monkeypatch):
    modem, controller, clock = recovery
    monkeypatch.setenv("MODEM_NETWORK_RECOVERY", "true")
    requests = []
    monkeypatch.setattr(controller, "request", lambda **kwargs: requests.append(kwargs))
    monkeypatch.setattr(modem, "_write_command", lambda command: '+CLCC: 1,0,0,0,0,"123",129\r\nOK')
    controller._claim_network_reset()
    assert not modem.recovery_in_progress and not requests
    monkeypatch.setattr(modem, "_write_command", lambda command: "OK")
    controller._claim_network_reset()
    assert modem.recovery_in_progress and requests == [{"network": True}]
    clock.now += 599
    controller.request_network_recovery()
    assert controller._pending_network is None


def test_network_recovery_defaults_off(recovery, monkeypatch):
    _modem, controller, _clock = recovery
    monkeypatch.delenv("MODEM_NETWORK_RECOVERY", raising=False)
    assert not config.get_bool("MODEM_NETWORK_RECOVERY")
    controller.request_network_recovery()
    assert controller._pending_network is None


@pytest.mark.parametrize("uncertain", [False, True])
def test_escalation_uses_existing_reconnect_and_never_retries_reboot(recovery, monkeypatch, uncertain):
    modem, controller, _clock = recovery
    modem._reset_in_progress = True
    controller._started = 1000
    commands, states, ports = [], [], []
    controller._on_state = states.append
    monkeypatch.setattr(controller, "_soft_network_reset", lambda: False)
    monkeypatch.setattr(controller, "_wait_for_ports", ports.append)
    monkeypatch.setattr(controller, "_connect_bounded", lambda: True)
    monkeypatch.setattr(controller, "_close_transport", lambda: None)

    def write(command):
        commands.append(command)
        if uncertain:
            raise OSError("USB disappeared before OK")
        return "OK"

    monkeypatch.setattr(modem, "_write_command", write)
    controller._run()
    assert commands == ["AT+CFUN=1,1"]
    assert ports == [False, True]
    assert states == [True]
    assert not modem.recovery_in_progress


def test_stopping_never_escalates_to_reboot(recovery, monkeypatch):
    modem, controller, _clock = recovery
    commands = []
    controller.stop()
    monkeypatch.setattr(modem, "_write_command", commands.append)
    with pytest.raises(RuntimeError, match="stopping"):
        controller._soft_network_reset()
    assert commands == []


def test_pending_wait_uses_idle_owner_and_expires_with_fake_clock(recovery, monkeypatch, caplog):
    modem, controller, clock = recovery
    modem.run_idle_hardware = lambda action: False
    with caplog.at_level("INFO"):
        controller._wait_network_idle()
    assert clock.now == 1030
    assert "network_idle_timeout" in caplog.text
    assert not modem.recovery_in_progress


@pytest.mark.parametrize("operation", ["dial", "answer"])
@pytest.mark.parametrize("cause,expected", [("No service available", True), ('"No service available"', True),
                                           ("User busy", False)])
def test_shared_ceer_hook_for_outbound_and_answer(recovery, monkeypatch, operation, cause, expected):
    modem, _controller, clock = recovery
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "true")
    modem._ser = Serial(clock, {"AT+CEER": f"+CEER: {cause}\r\nOK\r\n"})
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    requests = []
    modem._on_network_failure = lambda: requests.append(True)
    if operation == "dial":
        modem.dial("123")
    else:
        modem.answer()
    modem._query_release_cause(modem._release_report, clock.now + 2)
    assert requests == ([True] if expected else [])


@pytest.mark.parametrize("raw", ['+CREG: 1,"1234","5678",7', '+CREG: 2,5,"1234","5678",7'])
def test_extended_creg(raw):
    assert parse_creg(raw)[0]


def test_registration_urcs_in_command_response_update_cache():
    modem = Sim7600Modem("fake")
    modem._sim_identity = identify("460001234567890", "+CREG: 1,2", "+CEREG: 1,4")
    modem._process_response_urcs('\r\n+CREG: 3\r\n+CEREG: 1\r\nOK\r\n')
    assert modem.sim_identity.eps_registered


def test_fragmented_creg_waits_for_whole_line():
    modem = Sim7600Modem("fake")
    modem._sim_identity = identify("460001234567890", "+CREG: 1,2")
    modem._buffer = '+CREG: 2,'
    modem._process_sim_urcs()
    modem._buffer += '5,"1234","5678",7\r\n'
    modem._process_sim_urcs()
    assert modem.sim_identity.registered
