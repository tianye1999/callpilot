"""Offline phone-first acceptance checks; no serial ports or network clients."""

import asyncio
import json
import threading
from pathlib import Path
from queue import Full
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from fakes import FakeAgent, FakeAudioBridge

from agentcall import config
from agentcall.call_agent import CallAgentService, CallSession
from agentcall.cloud_control import CloudEdgeClient
from agentcall.modem import Eg25Modem, Sim7600Modem
from agentcall.remote_dialer import IssuedLiveKitSession, RemoteDialerInvite
from agentcall.takeover_coordinator import InboundTakeoverCoordinator, MediaOwner, TakeoverState


def test_phone_first_configuration_defaults_and_example(monkeypatch):
    example = dotenv_values(Path(__file__).parents[2] / ".env.example", encoding="utf-8")
    for key, default in (
        ("INBOUND_PHONE_FIRST_ENABLED", "false"),
        ("INBOUND_PHONE_FIRST_RING_SECONDS", "15"),
        ("INBOUND_PHONE_FIRST_MEDIA_SECONDS", "20"),
    ):
        monkeypatch.delenv(key, raising=False)
        assert config.get_str(key) == default == example[key]
    for value in ("0", "16", "nan", "1.5"):
        with pytest.raises(ValueError):
            config._check_value(config.get_spec("INBOUND_PHONE_FIRST_RING_SECONDS"), value)
    spec = config.get_spec("INBOUND_PHONE_FIRST_MEDIA_SECONDS")
    assert spec.kind == "int"
    for value in ("5", "20", "30"):
        config._check_value(spec, value)
    for value in ("4", "31", "nan", "1.5", "", "-1"):
        with pytest.raises(ValueError):
            config._check_value(spec, value)


def test_unanswered_coordinator_has_no_ai_owner():
    router = SimpleNamespace(owner=MediaOwner.NONE)
    router.switch_owner = lambda owner: setattr(router, "owner", owner)
    coordinator = InboundTakeoverCoordinator(
        call_id="call_test", generation=1, media_router=router, unanswered=True,
    )
    assert coordinator.state is TakeoverState.RINGING_OWNER
    assert router.owner is MediaOwner.NONE
    assert coordinator.rollback_precommit("timeout").accepted
    assert coordinator.state is TakeoverState.AI_ACTIVE


def test_reserved_incoming_answer_is_once_and_old_lease_cannot_hangup(monkeypatch):
    modem = Eg25Modem("unused")
    commands = []

    def command(cmd):
        commands.append(cmd)
        status = 0 if "ATA" in commands else 4
        return f'+CLCC: 1,1,{status},0,0,"",129\r\nOK' if cmd == "AT+CLCC" else "OK"

    monkeypatch.setattr(modem, "_write_command", command)
    lease = modem.reserve_incoming()
    assert modem.answer_reserved_incoming(lease, lambda: True)
    assert modem.answer_reserved_incoming(lease, lambda: True)
    assert commands.count("ATA") == 1
    modem.reserve_incoming()
    modem.finish_reserved_incoming(lease)
    assert not any(cmd in ("ATH", "AT+CHUP") for cmd in commands)


@pytest.mark.parametrize("ending", ["urc", "clcc", "generation"])
def test_unanswered_end_invalidates_answer_without_ata(monkeypatch, ending):
    modem = Eg25Modem("unused")
    commands = []
    monkeypatch.setattr(modem, "_write_command", lambda cmd: commands.append(cmd) or "OK")
    monkeypatch.setattr(modem, "_request_release_cause", lambda *args: None)
    lease = modem.reserve_incoming()
    if ending == "urc":
        modem._process_response_urcs("NO CARRIER")
    elif ending == "clcc":
        modem._process_clcc_response("OK")
        modem._process_clcc_response("OK")
    else:
        modem._maintenance_generation += 1
    assert not modem.answer_reserved_incoming(lease, lambda: True)
    assert "ATA" not in commands


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleep_sync(seconds)
        await asyncio.sleep(0)

    def sleep_sync(self, seconds):
        self.now += seconds
        self.tick()

    def tick(self):
        pass


@pytest.mark.parametrize("outcome", [
    "delayed_active", "still_incoming", "invalid_snapshots", "error", "ata_no_carrier",
    "no_carrier", "cend", "absent", "other_call", "stale_generation",
])
def test_reserved_ata_confirmation_is_bounded_and_urc_friendly(monkeypatch, outcome):
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "false")
    modem = Eg25Modem("unused")
    clock = Clock()
    commands, sleeps, fast_poll = [], [], []
    snapshots = 0

    def command(cmd):
        nonlocal snapshots
        commands.append(cmd)
        if cmd == "ATA":
            clock.now += 0.1
            return {"error": "ERROR", "ata_no_carrier": "NO CARRIER"}.get(outcome, "OK")
        if "ATA" not in commands:
            return '+CLCC: 1,1,4,0,0,"",129\r\nOK'
        snapshots += 1
        if outcome == "absent":
            return "OK"
        if outcome == "other_call":
            return '+CLCC: 2,1,0,0,0,"",129\r\nOK'
        if outcome == "invalid_snapshots":
            return "ERROR" if snapshots % 2 else '+CLCC: 1,1,0,0,0,"",129'
        state = 0 if outcome == "error" or (outcome == "delayed_active" and snapshots >= 4) else 4
        return f'+CLCC: 1,1,{state},0,0,"",129\r\nOK'

    def sleep(seconds):
        # A different thread must be able to process URCs while confirmation waits.
        acquired = threading.Event()

        def reader():
            with modem._serial_lock:
                acquired.set()
                if outcome in ("no_carrier", "cend"):
                    modem._process_response_urcs("NO CARRIER" if outcome == "no_carrier" else "+CEND: 1,0,0,0\r\n")
                elif outcome == "stale_generation":
                    modem._maintenance_generation += 1

        worker = threading.Thread(target=reader)
        worker.start()
        worker.join(timeout=0.5)
        assert acquired.is_set(), "confirmation sleep must release the serial lock"
        sleeps.append(seconds)
        clock.sleep_sync(seconds)

    monkeypatch.setattr(modem, "_write_command", command)
    monkeypatch.setattr(modem, "_request_release_cause", lambda *args: None)
    monkeypatch.setattr(modem, "_start_fast_clcc_poll", lambda: fast_poll.append(True))
    lease = modem.reserve_incoming()
    connected = modem.answer_reserved_incoming(lease, lambda: True, clock=clock, sleep=sleep)
    assert connected is (outcome in ("delayed_active", "still_incoming", "invalid_snapshots"))
    assert modem.is_call_connected() is connected
    assert commands.count("ATA") == 1
    assert fast_poll == ([True] if connected else [])
    assert not any(cmd in ("ATH", "AT+CHUP") for cmd in commands)
    assert all(seconds == pytest.approx(0.25) for seconds in sleeps)
    if outcome == "delayed_active":
        assert len(sleeps) == 3
        assert lease.ata_ms == pytest.approx(850)
    elif outcome in ("still_incoming", "invalid_snapshots"):
        assert sum(sleeps) == pytest.approx(2)
        assert lease.ata_ms == pytest.approx(2100)
    else:
        modem.finish_reserved_incoming(lease)
        assert not any(cmd in ("ATH", "AT+CHUP") for cmd in commands)
    if outcome in ("error", "ata_no_carrier"):
        assert snapshots == 0
    elif outcome in ("absent", "other_call"):
        assert snapshots == 2


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.delenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", raising=False)
    monkeypatch.setenv("INBOUND_PHONE_FIRST_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_CLOUD_ENABLED", "true")
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "false")
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "false")
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "must not play")
    clock = Clock()
    modem = Sim7600Modem("unused")
    modem.POST_ANSWER_SETTLE_SECONDS = 0
    commands = []

    def command(cmd):
        commands.append(cmd)
        if cmd == "AT+CLCC":
            state = 0 if "ATA" in commands else 4
            return f'+CLCC: 1,1,{state},0,0,"",129\r\nOK'
        return "OK"

    monkeypatch.setattr(modem, "_write_command", command)
    monkeypatch.setattr(modem, "initialize_for_voice", lambda mode: commands.append("PCM"))
    monkeypatch.setattr(modem, "hangup", lambda: commands.append("HANGUP"))
    answer = modem.answer_reserved_incoming
    monkeypatch.setattr(modem, "answer_reserved_incoming", lambda lease, current: answer(
        lease, current, clock=clock, sleep=clock.sleep_sync,
    ))
    service = CallAgentService("unused", "unused", provider="openai", modem=modem)
    session = service.session
    session._phone_first_cloud_ready = lambda: True
    session._phone_first_clock = clock
    session._phone_first_sleep = clock.sleep
    monkeypatch.setattr(service, "_reject_if_credentials_missing", lambda: (False, None))
    notifications = []
    monkeypatch.setattr("agentcall.notification_events.publish_notification", lambda *args: notifications.append(args))
    bridge = FakeAudioBridge()
    bridge.writer_count = 0
    bridge.max_writers = 0

    def start_bridge():
        bridge.started = True
        bridge.writer_count += 1
        bridge.max_writers = max(bridge.writer_count, bridge.max_writers)

    def stop_bridge():
        bridge.stopped = True
        bridge.writer_count = max(0, bridge.writer_count - 1)

    monkeypatch.setattr(bridge, "start", start_bridge)
    monkeypatch.setattr(bridge, "stop", stop_bridge)
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)

    def ring():
        # Exercise the real admission/start reset without launching its worker.
        with monkeypatch.context() as patch:
            patch.setattr("agentcall.call_agent.threading.Thread", lambda **kw: SimpleNamespace(
                start=lambda: None, is_alive=lambda: True,
            ))
            modem._on_ring(None)

    issued = IssuedLiveKitSession(
        invite=RemoteDialerInvite("session_test", "", 1000), room_name="room_test",
        browser_identity="web_test", edge_identity="edgepart_test", browser_token="",
        edge_token="test", livekit_url="wss://invalid.example",
    )

    def claim():
        request = session.next_takeover_offer()
        assert request is not None
        return session.accept_takeover_claim(
            offer_id=request.offer_id, call_id=request.call_id, claim_id="claim_test",
            generation=request.generation, nonce=request.nonce, issued=issued,
        )

    endpoint = SimpleNamespace(media_ready=True, events=[], closed=False)

    async def connect():
        pass

    async def close():
        endpoint.closed = True

    async def event(value):
        endpoint.events.append(value)

    endpoint.connect, endpoint.close, endpoint.send_event = connect, close, event
    async def next_command(timeout):
        return None

    endpoint.next_command = next_command
    session._takeover_endpoint_factory = lambda issued: endpoint

    async def pump(*args):
        assert modem.is_call_connected()
        assert session.takeover_state is TakeoverState.MOBILE_ACTIVE
        return bridge

    monkeypatch.setattr(session, "_pump_mobile_media", pump)
    return SimpleNamespace(
        service=service, session=session, modem=modem, commands=commands, clock=clock,
        ring=ring, claim=claim, issued=issued, endpoint=endpoint, bridge=bridge, notifications=notifications,
    )


@pytest.mark.parametrize("delay", [8, 12, 19])
def test_delayed_media_ready_records_both_latencies(rig, monkeypatch, delay):
    def forbidden(*args, **kwargs):
        pytest.fail("ready mobile must not start AI")

    monkeypatch.setattr("agentcall.call_agent.create_agent", forbidden)
    rig.endpoint.media_ready = False
    original = rig.modem._write_command

    def command(cmd):
        if cmd == "ATA":
            rig.clock.now += 0.3
        return original(cmd)

    monkeypatch.setattr(rig.modem, "_write_command", command)
    rig.ring()

    def claim():
        assert rig.claim().accepted
        claimed_at = rig.clock.now

        def ready():
            if rig.clock.now >= claimed_at + delay:
                rig.endpoint.media_ready = True
                rig.clock.tick = lambda: None

        rig.clock.tick = ready

    rig.clock.tick = claim
    asyncio.run(rig.session._handle_call())
    assert rig.session._record.source == "remote_web_dialer"
    assert rig.commands.count("ATA") == 1
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    latencies = {event["stage"]: event["ms"] for event in events if event["type"] == "latency"}
    assert delay * 1000 <= latencies["phone_first_media_ready_ms"] < delay * 1000 + 51
    assert latencies["phone_first_ata_ms"] == pytest.approx(300)
    edge_stages = [event["stage"] for event in events
                   if event["type"] == "latency" and event["stage"].startswith("edge_")]
    assert edge_stages == [
        "edge_claim_received", "edge_endpoint_connect_start", "edge_endpoint_connect_return",
        "edge_media_ready", "edge_ata_start", "edge_ata_return", "edge_pcm_ready", "edge_ath_complete",
    ]
    assert all(latencies[stage] >= 0 for stage in edge_stages)
    assert [latencies[stage] for stage in edge_stages] == sorted(latencies[stage] for stage in edge_stages)


def test_full_offer_queue_falls_back_to_ai(rig, monkeypatch, caplog):
    initialize = rig.session._initialize_takeover_context

    def full(request):
        raise Full

    def initialize_full(*args, **kwargs):
        initialize(*args, **kwargs)
        monkeypatch.setattr(rig.session._takeover_offer_queue, "put_nowait", full)

    monkeypatch.setattr(rig.session, "_initialize_takeover_context", initialize_full)
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    rig.ring()
    asyncio.run(rig.session._handle_call())
    assert agent.started and agent.said
    assert rig.commands.count("ATA") == 1
    assert rig.clock.now == 100
    assert "phone-first offer queue full" in caplog.text
    meta = json.loads((rig.session._record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] != "failed" and meta["answered"]


@pytest.mark.parametrize("provider,parallel", [("openai", False), ("openai", True), ("moss", True)])
def test_mobile_answer_never_starts_ai_or_greeting(rig, monkeypatch, provider, parallel):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel))
    rig.session.provider = provider

    def forbidden(*args, **kw):
        pytest.fail("phone-first mobile path must not start AI or triage")

    monkeypatch.setattr("agentcall.call_agent.create_agent", forbidden)
    monkeypatch.setattr(rig.session, "_initialize_triage_context", forbidden)
    monkeypatch.setattr(rig.modem, "play_answer_greeting", forbidden)
    rig.ring()

    def claim_once():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = claim_once
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert rig.commands.count("PCM") == 1
    assert rig.commands.count("HANGUP") == 1
    assert rig.bridge.started and rig.bridge.stopped
    assert rig.endpoint.closed
    assert rig.bridge.max_writers == 1 and rig.bridge.writer_count == 0
    assert not rig.notifications
    meta = json.loads((rig.session._record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["answered"] and meta["status"] == "completed"
    assert meta["source"] == "remote_web_dialer"


def test_early_hangup_revokes_one_unanswered_record(rig, monkeypatch):
    rig.ring()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))

    def end_once():
        rig.clock.tick = lambda: None
        rig.modem._invalidate_reserved_incoming()
        rig.modem._on_hangup()

    rig.clock.tick = end_once
    asyncio.run(rig.session._handle_call())
    assert "ATA" not in rig.commands and "PCM" not in rig.commands
    assert rig.session.next_takeover_revoke().reason == "CALL_ENDED"
    record = rig.session._record
    assert len(list(record.path.parent.glob("*/meta.json"))) == 1
    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "not_connected" and not meta["answered"]
    assert "source" not in meta
    assert not (record.path / "summary.json").exists()


@pytest.mark.parametrize("cause", [
    "timeout", "disconnect", "no_media", "post_answer_failure", "connect_failure", "disconnect_after_claim",
])
def test_phone_first_falls_back_to_ai_with_one_ata(rig, monkeypatch, cause):
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    rig.modem.POST_ANSWER_SETTLE_SECONDS = 0
    rig.ring()

    def tick():
        if cause == "disconnect":
            rig.session._phone_first_cloud_ready = lambda: False
        elif cause in ("no_media", "post_answer_failure", "connect_failure", "disconnect_after_claim"):
            rig.clock.tick = lambda: None
            assert rig.claim().accepted
            if cause == "no_media":
                rig.endpoint.media_ready = False
            elif cause == "post_answer_failure":
                rig.session._takeover_coordinator.commit_mobile = lambda fence: (_ for _ in ()).throw(
                    RuntimeError("commit failed")
                )
            elif cause == "connect_failure":
                async def fail():
                    raise RuntimeError("connect failed")
                rig.endpoint.connect = fail
            else:
                rig.session._phone_first_cloud_ready = lambda: False

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert agent.started and agent.said
    assert rig.bridge.max_writers == 1 and rig.bridge.writer_count == 0
    limit = 115.1 if cause == "timeout" else 120.2 if cause == "no_media" else 103
    assert rig.clock.now < limit


@pytest.mark.parametrize("configured", [None, "false"])
def test_disabled_path_has_no_new_queries_timers_commands_or_events(rig, monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("INBOUND_PHONE_FIRST_ENABLED")
    else:
        monkeypatch.setenv("INBOUND_PHONE_FIRST_ENABLED", configured)

    def forbidden(*args, **kw):
        pytest.fail("disabled path touched phone-first dependency")

    rig.session._phone_first_cloud_ready = forbidden
    rig.session._phone_first_clock = forbidden
    rig.session._phone_first_sleep = forbidden
    monkeypatch.setattr(rig.modem, "reserve_incoming", forbidden)
    monkeypatch.setattr("agentcall.cloud_control.CloudControlApi.list_devices", forbidden)
    monkeypatch.setattr("agentcall.call_agent.threading.Timer", forbidden)
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    rig.ring()
    asyncio.run(rig.session._handle_call())
    assert rig.commands == ["ATA", "PCM", "HANGUP"]
    assert rig.session._phone_first_lease is None
    assert agent.started and agent.said
    assert rig.session.next_takeover_offer() is None
    assert rig.session.next_takeover_revoke() is None
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    assert not any(e["type"].startswith("phone_first") for e in events)


@pytest.mark.parametrize("value", ["0", "16", "nan", "", "1.5", "-1"])
def test_invalid_wait_uses_original_start_and_warns(rig, monkeypatch, caplog, value):
    monkeypatch.setenv("INBOUND_PHONE_FIRST_RING_SECONDS", value)
    starts = []
    monkeypatch.setattr(rig.session, "start", lambda **kw: starts.append(kw))
    rig.ring()
    assert starts == [{}]
    assert "INBOUND_PHONE_FIRST_RING_SECONDS" in caplog.text
    assert not rig.commands


@pytest.mark.parametrize("unavailable", ["takeover", "hosted", "cloud", "unregistered", "factory"])
def test_local_preflight_failure_uses_original_path(rig, monkeypatch, unavailable):
    keys = {"takeover": "INBOUND_TAKEOVER_ENABLED", "hosted": "REMOTE_WEB_DIALER_ENABLED",
            "cloud": "REMOTE_CLOUD_ENABLED"}
    if unavailable in keys:
        monkeypatch.setenv(keys[unavailable], "false")
    elif unavailable == "unregistered":
        rig.session._phone_first_cloud_ready = lambda: False
    else:
        rig.session._takeover_endpoint_factory = None
    starts = []
    monkeypatch.setattr(rig.session, "start", lambda **kw: starts.append(kw))
    rig.ring()
    assert starts == [{}]
    assert not rig.commands


def test_cloud_availability_is_cached_without_credential_or_pairing_io():
    def forbidden():
        pytest.fail("read credentials on ringing path")

    client = CloudEdgeClient("https://invalid.example", SimpleNamespace(), SimpleNamespace(load=forbidden))
    assert not client.takeover_available()
    client._set_connected(True)
    assert client.takeover_available()
    client._set_connected(False)
    assert not client.takeover_available()


def test_duplicate_ring_clcc_do_not_extend_deadline_or_offer(rig, monkeypatch):
    rig.ring()
    deadline = rig.session._phone_first_deadline
    generation = rig.session._session_generation
    offers = []

    def tick():
        offer = rig.session.next_takeover_offer()
        if offer:
            offers.append(offer)
        rig.ring()
        rig.modem._process_clcc_response('+CLCC: 1,1,4,0,0,"",129\r\nOK')
        assert rig.session._phone_first_deadline == deadline
        assert rig.session._session_generation == generation

    rig.clock.tick = tick
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    asyncio.run(rig.session._handle_call())
    assert len(offers) == 1
    assert offers[0].expires_at - offers[0].created_at == pytest.approx(15)
    assert rig.commands.count("ATA") == 1


@pytest.mark.parametrize("when,accepted", [(114.99, True), (115.0, False), (115.01, False)])
def test_claim_deadline_has_one_winner(rig, monkeypatch, when, accepted):
    rig.ring()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        rig.clock.now = when
        assert rig.claim().accepted is accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert agent.started is not accepted
    assert rig.bridge.max_writers == 1


def test_old_generation_cannot_answer_or_finish_new_reservation(rig):
    rig.ring()
    old_lease = rig.session._phone_first_lease
    old_generation = rig.session._session_generation
    rig.session._session_generation += 1
    rig.session._phone_first_lease = rig.modem.reserve_incoming()
    assert not asyncio.run(rig.session._answer_phone_first(old_generation, old_lease))
    rig.modem.finish_reserved_incoming(old_lease)
    assert rig.commands == []


@pytest.mark.parametrize("guard", ["maintenance", "recovering", "remote", "credentials"])
def test_original_admission_guards_precede_phone_first(rig, monkeypatch, guard):
    if guard == "maintenance":
        rig.service._hardware_maintenance = True
    elif guard == "recovering":
        rig.modem._reset_in_progress = True
    elif guard == "remote":
        rig.service._remote_call_owner = object()
    else:
        monkeypatch.setattr(rig.service, "_reject_if_credentials_missing", lambda: (True, "missing"))
    rig.ring()
    assert not rig.session.is_busy and not rig.commands
    assert rig.session._phone_first_lease is None


@pytest.mark.parametrize("parallel", [False, True])
def test_post_answer_failure_parallel_and_legacy_have_one_answer_event(rig, monkeypatch, parallel):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel))
    # The AI fallback may use its existing module greeting; this test isolates ATA.
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    rig.ring()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted
        rig.session._takeover_coordinator.commit_mobile = lambda fence: (_ for _ in ()).throw(RuntimeError())

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert rig.bridge.max_writers == 1
    assert agent.started and agent.said
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    assert sum(event["type"] == "answered" for event in events) == 1


def test_duplicate_claim_never_renews_media_budget(rig, monkeypatch):
    rig.ring()
    session = rig.session

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted
        request = session._takeover_request
        deadline = session._phone_first_claim_deadline
        assert deadline == pytest.approx(rig.clock.now + 20)
        monkeypatch.setenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", "30")
        rig.clock.now += 1
        repeated = session.accept_takeover_claim(
            offer_id=request.offer_id, call_id=request.call_id, claim_id="claim_test",
            generation=request.generation, nonce=request.nonce, issued=rig.issued,
        )
        assert repeated.accepted and repeated.idempotent
        assert session._phone_first_claim_deadline == deadline

    rig.clock.tick = tick
    asyncio.run(session._handle_call())
    assert rig.commands.count("ATA") == 1


@pytest.mark.parametrize("configured,seconds", [
    (None, 20), ("5", 5), ("12", 12), ("30", 30),
    ("4", 20), ("31", 20), ("nan", 20), ("1.5", 20), ("", 20), ("-1", 20),
])
@pytest.mark.parametrize("pending_connect", [False, True])
def test_media_timeout_uses_live_config_and_warns_on_invalid_value(
    rig, monkeypatch, caplog, configured, seconds, pending_connect,
):
    if configured is not None:
        monkeypatch.setenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", configured)
    rig.ring()
    cancelled = []

    async def connect():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    if pending_connect:
        rig.endpoint.connect = connect
    rig.endpoint.media_ready = False
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted
        assert rig.session._phone_first_claim_deadline == pytest.approx(rig.clock.now + seconds)

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert cancelled == ([True] if pending_connect else [])
    assert rig.endpoint.closed
    assert 100 + seconds <= rig.clock.now < 100 + seconds + 0.2
    assert agent.started and rig.commands.count("ATA") == 1
    invalid = configured is not None and configured not in {"5", "12", "30"}
    warnings = [record for record in caplog.records if "INBOUND_PHONE_FIRST_MEDIA_SECONDS" in record.message]
    assert len(warnings) == int(invalid)
    if invalid:
        assert warnings[0].levelname == "WARNING"
        assert "20" in warnings[0].message


def test_media_budget_is_reread_for_next_call(rig, monkeypatch):
    session = rig.session

    for seconds in (5, 30):
        monkeypatch.setenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", str(seconds))
        rig.commands.clear()
        rig.endpoint.closed = False
        rig.ring()

        def claim_once():
            rig.clock.tick = lambda: None
            assert rig.claim().accepted
            assert session._phone_first_claim_deadline == pytest.approx(rig.clock.now + seconds)

        rig.clock.tick = claim_once
        session._run()
        assert session._record.source == "remote_web_dialer"
        assert rig.commands.count("ATA") == 1


def test_hangup_while_media_connects_never_answers_or_runs_ai(rig, monkeypatch):
    rig.ring()

    async def connect():
        rig.modem._invalidate_reserved_incoming()
        rig.modem._on_hangup()

    rig.endpoint.connect = connect
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert "ATA" not in rig.commands and "PCM" not in rig.commands
    assert rig.endpoint.closed
    assert rig.session.next_takeover_revoke().reason == "CALL_ENDED"


@pytest.mark.parametrize("fault", ["ata_error", "gone_before_answer", "pcm_failed"])
def test_modem_failure_never_reanswers_or_creates_ai_on_dead_call(rig, monkeypatch, fault):
    rig.ring()
    original = rig.modem._write_command

    def command(cmd):
        if (fault == "ata_error" and (cmd == "ATA" or "ATA" in rig.commands)) or fault == "gone_before_answer":
            rig.commands.append(cmd)
            return "ERROR" if cmd == "ATA" else "OK"
        return original(cmd)

    monkeypatch.setattr(rig.modem, "_write_command", command)
    if fault == "pcm_failed":
        rig.modem.last_voice_enable = {"ok": False}
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") <= 1
    assert not rig.bridge.started
    meta = json.loads((rig.session._record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == ("failed" if fault == "pcm_failed" else "not_connected")


def test_clcc_error_does_not_end_window_but_two_empty_snapshots_do(rig, monkeypatch):
    rig.ring()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))

    def tick():
        rig.clock.tick = lambda: None
        for _ in range(3):
            rig.modem._process_clcc_response("ERROR")
        assert rig.session.is_active
        rig.modem._process_clcc_response("OK")
        assert rig.session.is_active
        rig.modem._process_clcc_response("OK")
        assert not rig.session.is_active

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert not rig.commands
    assert rig.session.next_takeover_revoke().reason == "CALL_ENDED"


def test_stop_keeps_admission_closed_until_blocked_answer_worker_finishes(rig, monkeypatch):
    rig.ring()
    lease = rig.session._phone_first_lease
    generation = rig.session._session_generation
    entered, release = threading.Event(), threading.Event()
    original = rig.modem._write_command

    def command(cmd):
        result = original(cmd)
        if cmd == "ATA":
            entered.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(rig.modem, "_write_command", command)

    async def exercise():
        task = asyncio.create_task(rig.session._answer_phone_first(generation, lease))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            rig.session.stop()
            assert rig.session.is_busy
            rig.ring()
            assert rig.session._session_generation == generation
        finally:
            release.set()
        assert not await task

    asyncio.run(exercise())
    rig.modem.finish_reserved_incoming(lease)
    rig.session._resources_released = True
    rig.ring()
    before = list(rig.commands)
    rig.modem.finish_reserved_incoming(lease)
    assert not asyncio.run(rig.session._answer_phone_first(generation, lease))
    assert rig.commands == before


def test_mobile_and_early_end_publish_terminal_ui_status(rig):
    rig.ring()
    events = []
    rig.session.hub = SimpleNamespace(publish=events.append)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert [event["status"] for event in events if event["type"] == "call"] == ["answered", "ended"]


def test_media_cleanup_cannot_delay_ai_fallback(rig, monkeypatch):
    rig.ring()
    rig.endpoint.media_ready = False
    agent_started = asyncio.Event()
    agent = FakeAgent()

    def create(provider):
        agent_started.set()
        return agent

    monkeypatch.setattr("agentcall.call_agent.create_agent", create)

    async def close():
        await asyncio.wait_for(agent_started.wait(), timeout=0.1)
        rig.endpoint.closed = True

    rig.endpoint.close = close

    async def loop(*args):
        assert rig.clock.now < 120.2
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.endpoint.closed


@pytest.mark.parametrize("after_pcm", [False, True])
@pytest.mark.parametrize("with_command_id", [False, True])
def test_owner_cancels_media_preparation_then_ai_answers(rig, monkeypatch, after_pcm, with_command_id):
    rig.ring()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    reads = 0

    async def next_command(timeout):
        nonlocal reads
        reads += 1
        if after_pcm and reads == 1:
            return None
        assert ("ATA" in rig.commands) is after_pcm
        assert ("PCM" in rig.commands) is after_pcm
        return {"type": "hangup", **({"commandId": "32e5a14a-d2ad-4365-9908-7b071ec4db62"}
                                    if with_command_id else {})}

    rig.endpoint.next_command = next_command

    async def loop(*args):
        assert rig.session.takeover_state is TakeoverState.AI_ACTIVE
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert agent.started and rig.commands.count("ATA") == 1
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    assert sum(event["type"] == "takeover_owner_cancelled_precommit" for event in events) == 1
    assert sum(event.get("stage") == "edge_hangup_during_prepare" for event in events) == 1
    assert not any(event["type"] == "takeover_owner_hangup" for event in events)
    assert not any(event.get("type") == "hangup.ack" for event in rig.endpoint.events)


def test_late_duplicate_claim_cannot_revoke_winner_media_grace(rig, monkeypatch):
    rig.ring()
    session = rig.session
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("winner lost to duplicate"))

    def tick():
        rig.clock.tick = lambda: None
        rig.clock.now = 114.99
        assert rig.claim().accepted
        request = session._takeover_request
        fence = session.takeover_fence
        rig.clock.now = 115.1
        repeated = session.accept_takeover_claim(
            offer_id=request.offer_id, call_id=request.call_id, claim_id="claim_test",
            generation=request.generation, nonce=request.nonce, issued=rig.issued,
        )
        assert not repeated.accepted
        assert session.takeover_fence == fence

    rig.clock.tick = tick
    asyncio.run(session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert session._record.source == "remote_web_dialer"


@pytest.mark.parametrize("response", [
    'NO CARRIER\r\n+CLCC: 1,1,4,0,0,"",129\r\nOK',
    '+CLCC: 1,1,4,0,0,"",129',
])
def test_answer_preflight_rejects_ended_or_incomplete_snapshot(monkeypatch, response):
    modem = Eg25Modem("unused")
    commands = []
    monkeypatch.setattr(modem, "_write_command", lambda cmd: commands.append(cmd) or response)
    monkeypatch.setattr(modem, "_request_release_cause", lambda *args: None)
    lease = modem.reserve_incoming()
    assert not modem.answer_reserved_incoming(lease, lambda: True)
    assert commands == ["AT+CLCC"]


@pytest.mark.parametrize("ending", ["hangup", "disconnect"])
def test_direct_mobile_uses_existing_duplex_pump_and_disconnect_policy(rig, monkeypatch, ending):
    rig.ring()
    monkeypatch.setenv("REMOTE_DISCONNECT_GRACE_SECONDS", "0")
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    monkeypatch.setattr(rig.session, "_pump_mobile_media", CallSession._pump_mobile_media.__get__(rig.session))
    rig.bridge.feed_uplink(b"caller-to-owner")
    sent = []
    reads = 0

    async def next_command(timeout):
        nonlocal reads
        if rig.session.takeover_state is TakeoverState.MOBILE_ACTIVE:
            reads += 1
            if reads > 1:
                if ending == "hangup":
                    return {"type": "hangup"}
                rig.endpoint.media_ready = False
        return None

    rig.endpoint.next_command = next_command
    rig.endpoint.take_browser_audio = lambda: [b"owner-to-caller"]
    rig.endpoint.push_modem_audio = sent.append

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.bridge.downlink == [b"owner-to-caller"]
    assert sent == [b"caller-to-owner"]
    assert rig.commands.count("ATA") == rig.commands.count("HANGUP") == 1
    assert rig.endpoint.closed and rig.bridge.stopped
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    stages = [event["stage"] for event in events if event["type"] == "latency"]
    assert stages.count("edge_first_phone_to_modem") == stages.count("edge_first_modem_to_phone") == 1
    assert stages.index("edge_pcm_ready") < stages.index("edge_first_phone_to_modem")
    assert stages.index("edge_first_phone_to_modem") < stages.index("edge_ath_complete")
    assert stages.count("edge_hangup_received") == (1 if ending == "hangup" else 0)


def test_later_ai_takeover_does_not_inherit_expired_phone_first_window(rig):
    rig.ring()
    session = rig.session
    session._initialize_takeover_context("inbound")
    session._phone_first_cloud_ready = lambda: False
    assert asyncio.run(session._handle_phone_first(None)) == "ai"
    rig.clock.now = 116
    session._takeover_endpoint_rejection = lambda: None
    assert session.begin_owner_takeover()["success"]
    assert rig.claim().accepted


def test_next_clcc_only_call_can_reuse_index_after_window_cleanup():
    modem = Eg25Modem("unused")
    rings = []
    modem.on_ring(rings.append)
    incoming = '+CLCC: 1,1,4,0,0,"",129\r\nOK'
    modem._process_clcc_response(incoming)
    lease = modem.reserve_incoming()
    modem._invalidate_reserved_incoming()
    modem.finish_reserved_incoming(lease)
    modem._process_clcc_response(incoming)
    assert rings == [None, None]
