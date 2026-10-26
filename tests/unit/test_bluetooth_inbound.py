"""Phone incoming policy and ownership races, with fake oFono and time."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_bluetooth_line import ADDRESS, MODEM, FakeOfono, make_service, methods

from agentcall.bluetooth_modem import BluetoothModem

CALL = MODEM + "/incoming01"
NUMBER = "+15550000101"


class PhoneBus(FakeOfono):
    def __init__(self):
        super().__init__()
        self.answer_error = False
        self.answer_disconnects = False
        self.on_register = lambda: None
        self.after_answer = False
        self.answer_states = iter(())

    def state(self, state):
        self.calls = [[CALL, {"State": state, "LineIdentification": NUMBER}]] if state else []

    def call(self, destination, path, interface, member, signature="", body=None):
        if interface == "org.ofono.VoiceCall" and member == "GetProperties":
            self.operations.append((member, path, body))
            if self.after_answer:
                state = next(self.answer_states, None)
                if state is not None:
                    self.state(state)
            return [next((props.copy() for p, props in self.calls if p == path), {"State": "disconnected"})]
        result = super().call(destination, path, interface, member, signature, body)
        if member == "Register":
            self.on_register()
        elif member == "Answer":
            if self.answer_error:
                raise RuntimeError("fake answer failure")
            self.state("disconnected" if self.answer_disconnects else "active")
            self.after_answer = True
        return result


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "off")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", "")
    monkeypatch.setenv("INBOUND_AI_ANSWER_AFTER_SECONDS", "15")
    now = [0.0]
    bus = PhoneBus()

    def wait(seconds):
        now[0] += seconds

    modem = BluetoothModem(ADDRESS, transport=bus, clock=lambda: now[0], wait=wait)
    modem.connect()
    modem.poll_once(now[0])
    events, rings = [], []
    modem.on_call_event(events.append)
    modem.on_ring(rings.append)
    yield modem, bus, now, events, rings
    modem.close()


def incoming(phone, state="incoming", number=NUMBER):
    modem, bus, _, _, _ = phone
    bus.calls = [[CALL, {"State": state, "LineIdentification": number}]]
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallAdded", bus.calls[0])


def statuses(events):
    return [event["status"] for event in events]


def test_off_records_skip_owner_answer_and_end_without_agent(phone, caplog):
    modem, bus, _, events, _ = phone
    with caplog.at_level(logging.INFO):
        incoming(phone)
        assert not modem.inbound_ready
        modem.handle_signal(CALL, "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
        modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [CALL])
    assert statuses(events) == ["incoming_ringing", "incoming_skipped", "incoming_owner_answered", "incoming_finished"]
    assert events[-1]["outcome"] == "owner_answered"
    assert not {"Register", "Answer", "Hangup", "HangupAll"}.intersection(methods(bus))
    assert NUMBER not in caplog.text and "0101" in caplog.text


@pytest.mark.parametrize("number,allowlist,allowed", [
    (NUMBER, " +1555-000-0101 , +15550000102", True),
    ("0086 1555-000-0101", "15550000101", True),
    ("15550000101", "+86 1555-000-0101", True),
    (NUMBER, "+15550000102", False),
    (None, NUMBER, False), ("", NUMBER, False), ("anonymous", NUMBER, False),
    ("hidden15550000101", NUMBER, False),
    ("+3315550000101", "15550000101", False),
])
def test_allowlist_normalizes_but_never_suffix_matches(phone, monkeypatch, number, allowlist, allowed):
    modem, _, _, _, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", allowlist)
    incoming(phone, number=number)
    assert modem.inbound_ready is allowed


@pytest.mark.parametrize("mode", ["off", "allowlist", "unanswered"])
def test_waiting_never_offers_ai(phone, monkeypatch, mode):
    modem, bus, now, events, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", mode)
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    incoming(phone, "waiting")
    now[0] = 30
    modem.poll_once(now[0])
    assert not modem.inbound_ready
    assert "incoming_skipped" in statuses(events)
    assert "Register" not in methods(bus)


@pytest.mark.parametrize("owner_action", ["active", "disconnected", "removed", None])
def test_unanswered_deadline_never_steals_owner_answer_or_reject(phone, monkeypatch, owner_action):
    modem, bus, now, events, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "unanswered")
    incoming(phone)
    now[0] = 14
    modem.poll_once(now[0])
    assert not modem.inbound_ready
    if owner_action == "removed":
        bus.state(None)
        modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [CALL])
    elif owner_action:
        bus.state(owner_action)
        modem.handle_signal(CALL, "org.ofono.VoiceCall", "PropertyChanged", ["State", owner_action])
    now[0] = 15
    modem.poll_once(now[0])
    assert modem.inbound_ready is (owner_action is None)
    assert "Register" not in methods(bus)  # Scheduling alone never registers audio.


def test_answer_registers_cvsd_before_answering_specific_call(phone, monkeypatch):
    modem, bus, _, events, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    incoming(phone)
    assert modem.claim_inbound()
    modem.answer()
    assert modem.ai_owned and modem.is_call_connected()
    assert methods(bus).index("Register") < methods(bus).index("Answer")
    assert next(op for op in bus.operations if op[0] == "Answer")[1] == CALL
    assert "incoming_ai_answered" in statuses(events)
    modem.hangup()
    assert methods(bus).count("Unregister") == 1


@pytest.mark.parametrize("ending", ["active", "disconnected", "timeout"])
def test_answer_waits_for_delayed_ag_state_with_virtual_clock(phone, monkeypatch, ending):
    modem, bus, now, events, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(modem, "_wait", wait, raising=False)
    bus.answer_states = iter(["incoming", "incoming", ending] if ending != "timeout" else ["incoming"])
    incoming(phone)
    assert modem.claim_inbound()
    if ending == "active":
        modem.answer()
        assert modem.ai_owned and modem.is_call_connected()
        assert "Unregister" not in methods(bus)
        assert "incoming_ai_answered" in statuses(events)
    else:
        with pytest.raises(RuntimeError):
            modem.answer()
        assert not modem.ai_owned
        assert methods(bus).count("Unregister") == 1
        assert "incoming_answer_failed" in statuses(events)
        assert not {"Hangup", "HangupAll"}.intersection(methods(bus))
    assert len(waits) >= 2
    if ending == "timeout":
        assert 3 <= now[0] < 3.01
    else:
        assert now[0] < 3


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_hangup_targets_owned_path_without_touching_waiting_call(phone, monkeypatch, direction):
    modem, bus, _, _, _ = phone
    if direction == "inbound":
        monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
        monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
        incoming(phone)
        modem.claim_inbound()
        modem.answer()
    else:
        modem.dial(NUMBER)
    owned = modem._owned_path
    waiting = MODEM + "/waiting02"
    bus.calls.append([waiting, {"State": "waiting", "LineIdentification": "+15550000102"}])
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallAdded", bus.calls[-1])
    modem.hangup()
    assert [op for op in bus.operations if op[0] in {"Hangup", "HangupAll"}] == [("Hangup", owned, None)]
    assert waiting in modem._calls and not modem.ai_owned
    assert methods(bus).count("Unregister") == 1


def test_hangup_missing_owned_path_never_guesses(phone, caplog):
    modem, bus, _, _, _ = phone
    modem.dial(NUMBER)
    modem._owned_path = ""
    with caplog.at_level(logging.WARNING):
        modem.hangup()
    assert not {"Hangup", "HangupAll"}.intersection(methods(bus))
    assert "owned path missing" in caplog.text
    assert methods(bus).count("Unregister") == 1


@pytest.mark.parametrize("race", ["before_register", "during_register", "answer_error", "answer_disconnected"])
def test_answer_races_cleanup_without_hanging_up_owner(phone, monkeypatch, race):
    modem, bus, _, events, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    incoming(phone)
    assert modem.claim_inbound()
    if race == "before_register":
        bus.state("active")
    elif race == "during_register":
        bus.on_register = lambda: bus.state("active")
    bus.answer_error = race == "answer_error"
    bus.answer_disconnects = race == "answer_disconnected"
    with pytest.raises(RuntimeError):
        modem.answer()
    assert not modem.ai_owned
    assert methods(bus).count("Unregister") == (0 if race == "before_register" else 1)
    assert not {"Hangup", "HangupAll"}.intersection(methods(bus))
    assert "incoming_answer_failed" in statuses(events)


def test_non_ai_calls_have_one_durable_record_and_masked_logs(phone, tmp_path, caplog):
    modem, bus, _, _, _ = phone
    service = make_service(modem, tmp_path)
    with caplog.at_level(logging.INFO):
        incoming(phone)
        bus.state(None)
        modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [CALL])
    records = list(tmp_path.glob("*/meta.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["status"] == "missed"
    assert NUMBER not in caplog.text and NUMBER.lstrip("+") not in caplog.text
    assert not service.session.is_busy


def test_claim_cannot_answer_a_replacement_call(phone, monkeypatch):
    modem, bus, _, _, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    incoming(phone)
    assert modem.claim_inbound() == CALL
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [CALL])
    replacement = CALL + "replacement"
    bus.calls = [[replacement, {"State": "incoming", "LineIdentification": NUMBER}]]
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallAdded", bus.calls[0])
    with pytest.raises(RuntimeError):
        modem.answer()
    assert "Answer" not in methods(bus) and "Register" not in methods(bus)
    modem.poll_once(1)
    assert modem.inbound_ready  # The stale claim must not block future calls.


def test_offline_cancels_delayed_answer_and_finishes_record(phone, monkeypatch, tmp_path):
    modem, bus, now, _, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "unanswered")
    service = make_service(modem, tmp_path)
    incoming(phone)
    bus.online = False
    now[0] = 20
    modem.poll_once(now[0])
    assert not modem.inbound_ready and not service.session.is_busy
    assert "Register" not in methods(bus)
    assert json.loads(next(tmp_path.glob("*/meta.json")).read_text(encoding="utf-8"))["status"] == "missed"


@pytest.mark.parametrize("parallel", [False, True])
def test_real_service_inbound_session_records_audio_transcript_and_cleanup(phone, monkeypatch, tmp_path, parallel):
    from fakes import FakeAgent

    from agentcall import call_agent

    modem, bus, _, _, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel).lower())
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    monkeypatch.setenv("RECORDING_ENABLED", "true")
    service = make_service(modem, tmp_path)
    monkeypatch.setattr(service, "_reject_if_credentials_missing", lambda: (False, None))
    monkeypatch.setattr(call_agent, "create_agent", lambda _: FakeAgent())

    async def run_loop(agent, bridge, record, transcripts):
        assert bridge is modem.audio_bridge
        record.write_uplink(b"\0\x01" * 160)
        record.write_downlink(b"\0\x01" * 160)
        service.session._make_transcript_handler(record, transcripts, agent)("user", "A fictional message")
        service.session._set_active(False)
        return bridge

    monkeypatch.setattr(service.session, "_run_agent_loop", run_loop)
    incoming(phone)
    service.session._thread.join(timeout=2)
    assert not service.session._thread.is_alive()
    assert methods(bus).index("Register") < methods(bus).index("Answer")
    assert methods(bus).count("Unregister") == 1
    records = list(tmp_path.glob("*/meta.json"))
    assert len(records) == 1
    meta = json.loads(records[0].read_text(encoding="utf-8"))
    assert meta["status"] == "completed" and meta["answered"] is True
    assert meta["direction"] == "inbound" and meta["uplink_bytes"] == 320
    assert records[0].with_name("uplink.wav").exists()
    assert "A fictional message" in records[0].with_name("events.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("parallel", [False, True])
def test_remote_hangup_during_provider_start_cancels_and_unregisters(phone, monkeypatch, tmp_path, parallel):
    from fakes import FakeAgent

    from agentcall import call_agent

    modem, bus, _, _, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel).lower())
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    service = make_service(modem, tmp_path)
    session, agent = service.session, FakeAgent()
    monkeypatch.setattr(service, "_reject_if_credentials_missing", lambda: (False, None))
    monkeypatch.setattr(session, "start", lambda **kwargs: None)
    incoming(phone)
    session._inbound_record = service._bluetooth_incoming_records[CALL]
    session._active = True

    async def start(callback):
        bus.state(None)
        modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [CALL])
        await asyncio.Future()

    async def scenario():
        session._loop = asyncio.get_running_loop()
        async with asyncio.timeout(0.5) as deadline:
            await session._handle_call()
        assert not deadline.expired()

    monkeypatch.setattr(call_agent, "create_agent", lambda _: agent)
    monkeypatch.setattr(agent, "start", start)
    asyncio.run(scenario())
    assert agent.stopped and not modem.ai_owned
    assert methods(bus).count("Unregister") == 1
    assert not {"Hangup", "HangupAll"}.intersection(methods(bus))


def test_owner_answer_record_is_answered_without_recording(phone, tmp_path):
    modem, bus, _, _, _ = phone
    make_service(modem, tmp_path)
    incoming(phone)
    bus.state("active")
    modem.handle_signal(CALL, "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
    bus.state(None)
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [CALL])
    meta = json.loads(next(tmp_path.glob("*/meta.json")).read_text(encoding="utf-8"))
    assert meta["status"] == "owner_answered" and meta["answered"]
    assert not meta["recording_enabled"] and meta["uplink_bytes"] == 0


@pytest.mark.parametrize("delay", ["0", "-1", "601"])
def test_invalid_unanswered_delay_fails_closed(phone, monkeypatch, delay):
    modem, bus, now, _, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "unanswered")
    monkeypatch.setenv("INBOUND_AI_ANSWER_AFTER_SECONDS", delay)
    incoming(phone)
    now[0] = 1000
    modem.poll_once(now[0])
    assert not modem.inbound_ready and "Register" not in methods(bus)


def test_repeated_incoming_does_not_reset_deadline_or_repeat_offer(phone, monkeypatch):
    modem, _, now, events, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "unanswered")
    incoming(phone)
    now[0] = 14
    incoming(phone)
    now[0] = 15
    modem.poll_once(now[0])
    modem.poll_once(16)
    assert modem.inbound_ready
    assert statuses(events).count("incoming_ringing") == 1
    assert statuses(events).count("incoming_ai_requested") == 1


@pytest.mark.parametrize("mode", ["off", "allowlist", "unanswered"])
def test_late_caller_id_updates_record_without_restarting_timer(phone, monkeypatch, tmp_path, mode):
    modem, bus, now, _, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", mode)
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    service = make_service(modem, tmp_path)
    monkeypatch.setattr(service, "_reject_if_credentials_missing", lambda: (False, None))
    monkeypatch.setattr(service.session, "start", lambda **kwargs: None)
    incoming(phone, number=None)
    assert not modem.inbound_ready
    now[0] = 14
    bus.state("incoming")
    modem.handle_signal(CALL, "org.ofono.VoiceCall", "PropertyChanged", ["LineIdentification", NUMBER])
    assert bool(modem._claimed_inbound) is (mode == "allowlist")
    assert service._bluetooth_incoming_records[CALL].number == NUMBER
    now[0] = 15
    modem.poll_once(now[0])
    assert bool(modem._claimed_inbound) is (mode != "off")


def test_late_caller_id_cannot_promote_waiting_or_newly_enabled_policy(phone, monkeypatch):
    modem, _, _, _, _ = phone
    incoming(phone, number=None)
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    modem.handle_signal(CALL, "org.ofono.VoiceCall", "PropertyChanged", ["LineIdentification", NUMBER])
    assert not modem.inbound_ready
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [CALL])
    incoming(phone, state="waiting", number=None)
    modem.handle_signal(CALL, "org.ofono.VoiceCall", "PropertyChanged", ["State", "incoming"])
    modem.handle_signal(CALL, "org.ofono.VoiceCall", "PropertyChanged", ["LineIdentification", NUMBER])
    assert not modem.inbound_ready


def test_stop_before_session_start_never_answers_and_clears_claim(phone, monkeypatch, tmp_path):
    modem, bus, _, _, _ = phone
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    service = make_service(modem, tmp_path)
    monkeypatch.setattr(service, "_reject_if_credentials_missing", lambda: (False, None))
    monkeypatch.setattr(service.session, "start", lambda **kwargs: None)
    incoming(phone)
    session = service.session
    session._inbound_record = service._bluetooth_incoming_records[CALL]
    asyncio.run(session._handle_call())
    assert not {"Answer", "Register", "Hangup", "HangupAll"}.intersection(methods(bus))
    assert not modem._claimed_inbound


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("during_startup", [False, True])
@pytest.mark.parametrize("remote_ended", [False, True])
def test_sco_close_fences_ai_then_classifies_and_finalizes_record(
    phone, monkeypatch, tmp_path, parallel, direction, during_startup, remote_ended,
):
    from fakes import FakeAgent

    from agentcall import call_agent

    modem, bus, now, _, _ = phone
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel).lower())
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    monkeypatch.setenv("INBOUND_AI_ANSWER_MODE", "allowlist")
    monkeypatch.setenv("INBOUND_AI_ALLOWLIST", NUMBER)
    service = make_service(modem, tmp_path)
    session, agent = service.session, FakeAgent()
    events = []
    monkeypatch.setattr(service, "_publish", events.append)
    monkeypatch.setattr(service, "_reject_if_credentials_missing", lambda: (False, None))

    def start_session(**kwargs):
        session._inbound_record = kwargs.get("inbound_record")
        session._active = True

    monkeypatch.setattr(session, "start", start_session)
    if direction == "inbound":
        incoming(phone)
    else:
        session._outbound_number = NUMBER
        session._active = True

    def handoff():
        bridge = modem.audio_bridge
        bridge.packet_count = 1
        bridge.closed.set()
        modem._sco_closed(bridge)
        assert not session.is_active
        assert not session._agent_effect_allowed(session._session_generation)
        assert not session._phone_owner_took_over
        modem.hangup()  # Cleanup cannot hang up or drop ownership during confirmation.
        now[0] = 0.9
        modem.poll_once(now[0])
        assert "owner_took_over" not in [event["status"] for event in events]
        assert not {"Hangup", "HangupAll", "Connect", "Unregister"}.intersection(methods(bus))
        if remote_ended:
            bus.calls = []
        now[0] = 1
        modem.poll_once(now[0])

    async def agent_start(callback):
        agent.started = True
        agent._on_audio_out = callback
        if during_startup:
            handoff()
            await asyncio.Future()  # Cancellation must interrupt blocked startup.

    async def wait_connected(**kwargs):
        path = modem._owned_path
        bus.calls = [[path, {"State": "active"}]]
        modem.handle_signal(path, "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
        return True

    async def run_loop(agent, bridge, record, transcripts):
        handoff()
        await asyncio.Future()

    async def scenario():
        session._loop = asyncio.get_running_loop()
        async with asyncio.timeout(2) as deadline:
            await session._handle_call()
        assert not deadline.expired()

    monkeypatch.setattr(call_agent, "create_agent", lambda _: agent)
    monkeypatch.setattr(agent, "start", agent_start)
    monkeypatch.setattr(session, "_wait_connected", wait_connected)
    monkeypatch.setattr(session, "_run_agent_loop", run_loop)
    asyncio.run(scenario())
    assert agent.stopped
    assert not modem.ai_owned
    assert modem.phone_call_busy is not remote_ended
    assert methods(bus).count("Unregister") == 1
    assert not {"Hangup", "HangupAll"}.intersection(methods(bus))
    now[0] = 40
    modem.poll_once(now[0])
    assert "Connect" not in methods(bus)
    assert [event["status"] for event in events].count("owner_took_over") == int(not remote_ended)
    records = list(tmp_path.glob("*/meta.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["status"] == (
        "completed" if remote_ended else "owner_took_over"
    )
    events_text = records[0].with_name("events.jsonl").read_text(encoding="utf-8")
    assert ('"type": "owner_took_over"' in events_text) is not remote_ended


@pytest.mark.parametrize("final_state", ["active", "disconnected", "removed"])
def test_record_waits_for_live_sco_classification(phone, monkeypatch, tmp_path, final_state):
    from fakes import FakeAgent

    from agentcall import call_agent

    modem, bus, now, events, _ = phone
    service = make_service(modem, tmp_path)
    session, agent = service.session, FakeAgent()
    session._outbound_number = NUMBER
    session._active = True
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "false")
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("SUMMARY_ENABLED", "false")

    async def scenario():
        session._loop = asyncio.get_running_loop()
        stopped = asyncio.Event()

        async def wait_connected(**kwargs):
            bus.calls = [[modem._owned_path, {"State": "active"}]]
            modem.handle_signal(modem._owned_path, "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
            return True

        async def run_loop(agent, bridge, record, transcripts):
            bridge.packet_count = 1
            bridge.closed.set()
            modem._sco_closed(bridge)
            await asyncio.Future()

        async def stop():
            agent.stopped = True
            stopped.set()

        monkeypatch.setattr(session, "_wait_connected", wait_connected)
        monkeypatch.setattr(session, "_run_agent_loop", run_loop)
        monkeypatch.setattr(agent, "stop", stop)
        task = asyncio.create_task(session._handle_call())
        try:
            await stopped.wait()
            assert not session.is_active and agent.stopped
            assert not task.done() and not list(tmp_path.glob("*/meta.json"))
            assert not {"Hangup", "HangupAll", "Connect"}.intersection(methods(bus))
            now[0] = 0.9
            modem.poll_once(now[0])
            assert "owner_took_over" not in statuses(events)
            assert not list(tmp_path.glob("*/meta.json"))
            path = modem._owned_path
            bus.calls = [] if final_state == "removed" else [[path, {"State": final_state}]]
            now[0] = 1
            modem.poll_once(now[0])
            await task
        finally:
            if not task.done():
                modem.close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(call_agent, "create_agent", lambda _: agent)
    # Keep our own event collector in addition to the service's session callbacks.
    def on_event(event):
        events.append(event)
        service._bluetooth_call_event(event)

    modem.on_call_event(on_event)
    asyncio.run(asyncio.wait_for(scenario(), timeout=2))
    meta_path = next(tmp_path.glob("*/meta.json"))
    takeover = final_state == "active"
    assert json.loads(meta_path.read_text(encoding="utf-8"))["status"] == (
        "owner_took_over" if takeover else "completed"
    )
    assert statuses(events).count("owner_took_over") == int(takeover)
    assert not {"Hangup", "HangupAll", "Connect"}.intersection(methods(bus))
    assert methods(bus).count("Unregister") == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX SCO fd")
def test_real_sco_eof_notifies_once_and_never_reconnects(phone, monkeypatch):
    from agentcall import bluetooth_audio

    modem, bus, _, events, _ = phone
    modem.dial(NUMBER)
    path = modem._owned_path
    bus.calls = [[path, {"State": "active"}]]
    modem.handle_signal(path, "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
    monkeypatch.setattr(bluetooth_audio, "authorize_sco", lambda _: None)
    pi, phone_socket = socket.socketpair(type=socket.SOCK_DGRAM)
    try:
        bridge = modem.audio_bridge
        assert bridge.accept_fd(pi.detach(), 1)
        phone_socket.send(b"\x01\0" * 30)
        phone_socket.settimeout(1)
        assert phone_socket.recv(60) == bytes(60)
        phone_socket.send(b"")
        bridge._thread.join(timeout=1)
        assert not bridge._thread.is_alive()
        modem.poll_once(modem.active_since + 30)
        assert not {"Connect", "Hangup", "HangupAll"}.intersection(methods(bus))
        assert methods(bus).count("Unregister") == 1
        assert statuses(events).count("owner_took_over") == 1
    finally:
        pi.close()
        phone_socket.close()


def test_bluetooth_disables_app_takeover_and_triage(phone, monkeypatch, tmp_path, caplog):
    modem, _, _, _, _ = phone
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TRIAGE_MODE", "enforce")
    session = make_service(modem, tmp_path).session
    session._active = True
    with caplog.at_level(logging.INFO):
        session._initialize_takeover_context("inbound")
        session._initialize_triage_context("inbound", None)
    assert session._triage_mode == "off" and session._triage_judge is None
    assert session._takeover_coordinator is None
    assert "overrides" in caplog.text and "App takeover disabled" in caplog.text
    assert session._request_owner_takeover(0, bypass_enabled=True)["code"] == "TAKEOVER_UNSUPPORTED"
    registry = session._build_tools("inbound")
    assert "request_owner_takeover" not in str(registry.specs())


@pytest.mark.parametrize("error_name,detail", [
    ("org.bluez.Error.InProgress", "Already connecting"),
    ("org.bluez.Error.Failed", "Host is down"),
    ("org.bluez.Error.NotReady", "Adapter not ready"),
    ("org.bluez.Error.Failed", "unexpected failure"),
])
def test_reconnect_logs_rate_limit_expected_errors_only(phone, monkeypatch, caplog, error_name, detail):
    from agentcall.ofono_dbus import OfonoCallError

    modem, bus, now, _, _ = phone
    bus.online = False

    def fail():
        raise OfonoCallError(error_name, detail)

    monkeypatch.setattr(modem, "_connect_profile", fail)
    with caplog.at_level(logging.INFO):
        for tick in (0, 2, 6, 14, 30, 60):
            now[0] = tick
            modem.poll_once(tick)
    messages = [record for record in caplog.records if "reconnect" in record.message]
    expected = detail != "unexpected failure"
    assert len(messages) == (3 if expected else 6)
    assert all(bool(record.exc_info) is not expected for record in messages)
    assert not modem.is_connected()


def test_dbus_error_preserves_detail_but_not_in_string():
    from agentcall.ofono_dbus import OfonoCallError, OfonoDBus

    dbus = OfonoDBus("/agent")

    async def call(_):
        return SimpleNamespace(message_type="error", error_name="org.bluez.Error.Failed", body=["Host is down"])

    dbus._bus = SimpleNamespace(call=call)
    dbus._Message = lambda **kwargs: kwargs
    dbus._MessageType = SimpleNamespace(ERROR="error")
    try:
        with pytest.raises(OfonoCallError) as exc:
            asyncio.run(dbus._call("org.bluez", "/", "org.bluez.Device1", "ConnectProfile"))
        assert exc.value.detail == "Host is down"
        assert str(exc.value) == "org.bluez.Device1.ConnectProfile: org.bluez.Error.Failed"
    finally:
        dbus.close()


def test_inbound_configuration_defaults_and_registry(monkeypatch):
    from agentcall import config

    for key in ("INBOUND_AI_ANSWER_MODE", "INBOUND_AI_ALLOWLIST", "INBOUND_AI_ANSWER_AFTER_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    assert config.get_str("INBOUND_AI_ANSWER_MODE") == "off"
    assert config.get_str("INBOUND_AI_ALLOWLIST") == ""
    assert config.get_int("INBOUND_AI_ANSWER_AFTER_SECONDS") == 15
    assert config.get_spec("INBOUND_AI_ALLOWLIST").hidden
    assert not config.get_spec("INBOUND_AI_ALLOWLIST").editable
    example = (Path(__file__).parents[2] / ".env.example").read_text(encoding="utf-8")
    assert "INBOUND_AI_ANSWER_MODE=off" in example
    assert "INBOUND_AI_ALLOWLIST=\n" in example
    assert "INBOUND_AI_ANSWER_AFTER_SECONDS=15" in example
