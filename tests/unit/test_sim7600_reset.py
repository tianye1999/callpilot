"""Post-call SIM7600 reset: fake AT transport only, never opens a serial port."""

import threading

import pytest

from agentcall.modem import Eg25Modem, Sim7600Modem


@pytest.fixture
def modem(monkeypatch):
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "true")
    monkeypatch.setenv("MODEM_PCM_PIN_8K", "false")
    modem = Sim7600Modem("/dev/fake-at")
    monkeypatch.setattr(modem, "_send", lambda command: "OK")
    return modem


def test_reset_hook_claims_once_after_hangup_and_pcm_disable(modem, monkeypatch):
    calls = []
    monkeypatch.setattr(modem, "_send", lambda command: calls.append(command) or "OK")
    modem.on_post_call_reset(lambda: calls.append(("reset", modem.reset_in_progress)))
    modem.answer()
    modem.initialize_for_voice()
    modem.hangup()
    modem.hangup()  # Remote-party callback and session shutdown can both hang up.
    assert calls == ["ATA", "AT+CPCMREG=1", "AT+CHUP", "AT+CPCMREG=0", ("reset", True)]


@pytest.mark.parametrize("case", ["disabled", "not_enabled", "idle_enable", "failed_enable"])
def test_reset_hook_only_qualifies_a_real_pcm_enabled_call(modem, monkeypatch, case):
    requests = []
    modem.on_post_call_reset(lambda: requests.append(True))
    if case == "disabled":
        monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "false")
    if case != "idle_enable":
        modem.dial("test-number")
    if case == "failed_enable":
        monkeypatch.setattr(modem, "_CPCMREG_ENABLE_ATTEMPTS", 1)
        monkeypatch.setattr(modem, "_send", lambda command: "ERROR")
    if case != "not_enabled":
        modem.initialize_for_voice()
    modem.hangup()
    assert requests == []
    assert not modem.reset_in_progress


def test_quectel_is_not_paused_by_sim7600_config(monkeypatch):
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "true")
    modem = Eg25Modem("/dev/fake-at")
    assert not modem.recovery_in_progress
    assert not modem.transport_paused


def test_paused_transport_rejects_at_and_reconnect_without_opening(modem, monkeypatch):
    modem._reset_in_progress = True
    monkeypatch.setattr(modem, "_send", Sim7600Modem._send.__get__(modem))
    opened = []
    monkeypatch.setattr(modem, "_open_serial", lambda: opened.append(True))
    with pytest.raises(Exception, match="重启"):
        modem.send_command("AT")
    modem._reconnect()
    assert opened == []


def test_pause_drops_stale_clcc_and_ring_processing(modem):
    modem._reset_in_progress = True
    modem._buffer = "\r\nRING\r\n"
    rings = []
    modem.on_ring(lambda caller: rings.append(caller))
    modem._process_buffer()
    modem._process_clcc_response('+CLCC: 1,0,0,0,0,"test-number",129\r\nOK')
    assert rings == []
    assert not modem.is_call_connected()


def test_reset_access_does_not_enable_automatic_reconnect(modem, monkeypatch):
    opened = []
    modem._reset_in_progress = True
    modem._running = True
    monkeypatch.setattr(modem, "_open_serial", lambda: opened.append(True))
    with modem.reset_transport_access():
        assert not modem.transport_paused
        modem._reconnect()
    assert opened == []
    assert modem.transport_paused


@pytest.fixture
def recovery(modem, monkeypatch):
    from agentcall.sim7600_reset import Sim7600CallReset

    states = []
    controller = Sim7600CallReset(modem, "nmea", "/dev/fake-pcm", states.append)
    calls = []

    def raw(command):
        assert not modem.transport_paused
        calls.append(command)
        return {"AT+CPIN?": "+CPIN: READY\r\nOK", "AT+CEREG?": "+CEREG: 0,1\r\nOK"}.get(command, "OK")

    monkeypatch.setattr(modem, "_write_command", raw)
    monkeypatch.setattr(modem, "connect", lambda: calls.append("connect"))
    monkeypatch.setattr(modem, "refresh_sim_identity", lambda: calls.append("refresh_sim_identity"))
    monkeypatch.setattr(modem, "start_listener", lambda: calls.append("start_listener"))
    gone = iter([False, True])
    monkeypatch.setattr(controller, "_ports_gone", lambda: next(gone, True))
    monkeypatch.setattr(controller, "_ports_ready", lambda: True)
    monkeypatch.setattr(controller, "_POLL_SECONDS", 0.001)
    return controller, states, calls


def end_enabled_call(modem):
    modem.answer()
    modem.initialize_for_voice()
    modem.hangup()


def test_reset_observes_port_cycle_and_normal_reconnect_once(modem, recovery, caplog):
    controller, states, calls = recovery
    with caplog.at_level("INFO"):
        end_enabled_call(modem)
        modem.hangup()
        assert controller._thread is not None
        controller._thread.join(timeout=2)
    assert not controller._thread.is_alive()
    assert calls.count("AT+CRESET") == 1
    assert calls.index("AT+CRESET") < calls.index("connect") < calls.index("refresh_sim_identity")
    assert "start_listener" in calls
    assert states == [False, True]
    assert modem.is_connected()
    for phase in ("reset_sent", "ports_gone", "ports_back", "ready"):
        assert f"phase={phase}" in caplog.text
    assert "elapsed_seconds=" in caplog.text


@pytest.mark.parametrize("ports_ready", [True, False])
def test_port_cycle_timeout_releases_to_watchdog(modem, recovery, monkeypatch, caplog, ports_ready):
    controller, states, calls = recovery
    monkeypatch.setattr(controller, "_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(controller, "_ports_ready", lambda: ports_ready)
    monkeypatch.setattr(controller, "_ports_gone", lambda: not ports_ready)
    end_enabled_call(modem)
    controller._thread.join(timeout=2)
    assert not controller._thread.is_alive()
    assert calls == ["AT+CRESET"]
    assert states == [False, False]
    assert not modem.reset_in_progress
    assert "看门狗" in caplog.text


def test_stalled_connect_keeps_exclusive_lease_until_exit(modem, recovery, monkeypatch, caplog):
    controller, states, calls = recovery
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(controller, "_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(controller, "_CONNECT_TIMEOUT_SECONDS", 0.01)

    def blocked_connect():
        calls.append("connect")
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(modem, "connect", blocked_connect)
    end_enabled_call(modem)
    assert entered.wait(timeout=1)
    controller._thread.join(timeout=1)
    assert not controller._thread.is_alive()
    assert modem.reset_in_progress
    assert not modem.is_connected()
    assert states == [False]
    assert calls.count("connect") == 1
    release.set()
    controller._connector.join(timeout=1)
    assert not modem.reset_in_progress
    assert states == [False, False]
    assert "看门狗" in caplog.text


def test_stop_during_connect_cannot_revive_listener(modem, recovery, monkeypatch):
    controller, states, calls = recovery
    entered = threading.Event()
    release = threading.Event()

    def blocked_connect():
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(modem, "connect", blocked_connect)
    end_enabled_call(modem)
    assert entered.wait(timeout=1)
    controller.stop()
    release.set()
    controller._thread.join(timeout=2)
    assert True not in states
    assert "start_listener" not in calls


def test_service_reports_offline_and_blocks_calls_during_reset(modem, monkeypatch):
    from agentcall.call_agent import CallAgentService

    service = CallAgentService("/dev/fake-at", "unused", modem=modem, pcm_port="/dev/fake-pcm")
    controller = service._sim7600_reset
    entered = threading.Event()
    release = threading.Event()

    def wait_for_ports(_present):
        entered.set()
        release.wait(timeout=2)
        raise TimeoutError("fake USB never returned")

    monkeypatch.setattr(controller, "_wait_for_ports", wait_for_ports)
    monkeypatch.setattr(modem, "_write_command", lambda command: "OK")
    end_enabled_call(modem)
    assert entered.wait(timeout=1)
    try:
        assert modem.reset_in_progress
        assert not service.modem_connected
        assert service._dial_guard(None).code == "MODEM_OFFLINE"
        assert not service.dial("123")[0]
        modem._on_ring(None)
        assert not service.session.is_active
        # Delayed callbacks must consult current truth rather than their old payload.
        modem._on_connection_state(True)
        assert not service.modem_connected
    finally:
        release.set()
    controller._thread.join(timeout=2)
    assert not modem.reset_in_progress
    modem._on_connection_state(True)
    assert not service.modem_connected
    modem._on_ring(None)
    assert not service.session.is_active
    modem.close()


def test_watchdog_does_not_compete_with_reset(modem, monkeypatch):
    from agentcall.call_agent import CallAgentService

    service = CallAgentService("/dev/fake-at", "unused", modem=modem)
    modem._reset_in_progress = True
    service._service_running = True
    service._LINK_WATCHDOG_POLL = 0
    service._LINK_WATCHDOG_SECONDS = 0
    reconnects = []
    monkeypatch.setattr(service, "_reconnect_modem_bounded", lambda: reconnects.append(True))
    ticks = iter([True, True, False])

    def tick(_seconds):
        service._service_running = next(ticks, False)

    monkeypatch.setattr("agentcall.call_agent.time.sleep", tick)
    service._watch_modem_link()
    assert reconnects == []
    modem.close()


def test_both_links_must_disappear_before_reconnecting(modem, recovery, monkeypatch):
    controller, states, calls = recovery
    monkeypatch.setattr(controller, "_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(controller, "_ports_gone", lambda: False)
    monkeypatch.setattr(controller, "_ports_ready", lambda: False)
    end_enabled_call(modem)
    controller._thread.join(timeout=2)
    assert calls == ["AT+CRESET"]
    assert True not in states


@pytest.mark.parametrize("reply", ["", OSError("lost response")])
def test_ambiguous_reset_reply_still_waits_for_cycle_without_replay(modem, recovery, monkeypatch, reply):
    controller, states, calls = recovery
    write = modem._write_command

    def raw(command):
        if command == "AT+CRESET":
            calls.append(command)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return write(command)

    monkeypatch.setattr(modem, "_write_command", raw)
    end_enabled_call(modem)
    controller._thread.join(timeout=2)
    assert calls.count("AT+CRESET") == 1
    assert states == [False, True]


def test_cleanup_error_cannot_leave_reset_lease_stuck(modem, recovery, monkeypatch):
    controller, states, _calls = recovery
    monkeypatch.setattr(controller, "_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(controller, "_ports_gone", lambda: False)

    def failed_close():
        modem._emit_connection_state(False)
        raise OSError("close failed")

    monkeypatch.setattr(modem, "close_reset_transport", failed_close)
    end_enabled_call(modem)
    controller._thread.join(timeout=2)
    assert not modem.reset_in_progress
    assert states == [False, False]


def test_service_defers_queue_until_modem_recovery_is_ready(modem, monkeypatch):
    from agentcall.call_agent import CallAgentService

    service = CallAgentService("/dev/fake-at", "unused", modem=modem)
    advances = []
    monkeypatch.setattr(service.dial_queue, "on_session_ended", lambda: advances.append(True))
    modem._reset_in_progress = True
    service._set_modem_connected(False)
    service._handle_session_ended()
    assert advances == []
    modem._reset_in_progress = False
    modem._emit_connection_state(True)
    service._set_modem_connected(True)
    assert advances == [True]
    service._set_modem_connected(True)
    assert advances == [True]
    modem.close()


def test_next_completed_call_can_reset_before_previous_callback_exits(modem, recovery):
    controller, states, calls = recovery
    ready = threading.Event()
    release = threading.Event()

    def state(online):
        states.append(online)
        if online and not ready.is_set():
            ready.set()
            release.wait(timeout=2)

    controller._on_state = state
    end_enabled_call(modem)
    assert ready.wait(timeout=1)
    first = controller._thread
    try:
        end_enabled_call(modem)
    finally:
        release.set()
    first.join(timeout=2)
    controller._thread.join(timeout=2)
    assert calls.count("AT+CRESET") == 2
    assert not modem.reset_in_progress


def test_port_return_is_not_reported_ready_until_network_registration(modem, recovery, monkeypatch):
    controller, states, _calls = recovery
    entered = threading.Event()
    release = threading.Event()
    write = modem._write_command

    def raw(command):
        if command == "AT+CEREG?" and not entered.is_set():
            entered.set()
            release.wait(timeout=2)
            return "+CEREG: 0,2\r\nOK"
        return write(command)

    monkeypatch.setattr(modem, "_write_command", raw)
    end_enabled_call(modem)
    assert entered.wait(timeout=1)
    assert states == [False]
    assert not modem.is_connected()
    release.set()
    controller._thread.join(timeout=2)
    assert states == [False, True]


@pytest.mark.parametrize("response", ["+CEREG: 1,2", "+CEREG: 1,0", "+CEREG: 2,3"])
def test_registration_probe_does_not_confuse_reporting_mode_with_status(modem, recovery, monkeypatch, response):
    controller, _states, _calls = recovery
    monkeypatch.setattr(modem, "_write_command", lambda command: {
        "AT": "OK", "AT+CPIN?": "+CPIN: READY\r\nOK", "AT+CEREG?": response,
        "AT+CREG?": "+CREG: 1,0\r\nOK",
    }[command])
    assert not controller._network_ready()


def test_reset_claim_between_clcc_parse_and_mutation_drops_old_response(modem, monkeypatch):
    monkeypatch.setattr(modem, "_process_response_urcs", lambda response: setattr(modem, "_reset_in_progress", True))
    modem._process_clcc_response('+CLCC: 1,0,0,0,0,"test-number",129\r\nOK')
    assert not modem.is_call_connected()
