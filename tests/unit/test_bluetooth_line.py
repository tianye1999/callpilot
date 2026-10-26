"""Hardware-free HFP ownership, reconnect and SCO pacing contracts."""

from __future__ import annotations

import asyncio
import json
import os
import select
import socket
import threading

import pytest

from agentcall.bluetooth_audio import BluetoothAudioBridge, authorize_sco
from agentcall.bluetooth_modem import BluetoothModem

ADDRESS = "00:11:22:33:44:55"
MODEM = "/hfp/hci0_dev_00_11_22_33_44_55"
CARD = "/card0"


class FakeOfono:
    def __init__(self):
        self.online = True
        self.calls = []
        self.operations = []
        self.fail_dial = False

    def start(self, signal, audio):
        self.signal, self.audio = signal, audio

    def close(self):
        self.operations.append("close")

    def call(self, destination, path, interface, member, signature="", body=None):
        self.operations.append((member, path, body))
        if member == "GetModems":
            return [[[MODEM, {"Online": self.online}]]] if self.online is not None else [[]]
        if member == "GetManagedObjects":
            return [{"/org/bluez/hci0/dev_00_11_22_33_44_55": {
                "org.bluez.Device1": {"Address": ADDRESS},
            }}]
        if member == "GetProperties":
            return [{"Name": "中国移动", "Status": "registered", "Strength": 67}]
        if member == "GetCards":
            return [[[CARD, {"RemoteAddress": ADDRESS}]]]
        if member == "GetCalls":
            return [list(self.calls)]
        if member == "Dial":
            if self.fail_dial:
                raise RuntimeError("fake dial failure")
            self.calls = [[MODEM + "/voicecall01", {"State": "dialing"}]]
            return [MODEM + "/voicecall01"]
        return []


def connected_modem():
    bus = FakeOfono()
    modem = BluetoothModem(ADDRESS, transport=bus)
    modem.connect()
    modem.poll_once(0)
    return modem, bus


def methods(bus):
    return [item[0] for item in bus.operations if isinstance(item, tuple)]


def test_only_outbound_owns_agent_and_always_unregisters():
    modem, bus = connected_modem()
    assert "Register" not in methods(bus)
    assert modem.sim_identity.carrier == "中国移动"
    assert modem.sim_identity.plmn == ""
    assert modem.signal_strength == 67
    assert not modem.supports_sms
    modem.dial("+15550000101")
    modem.handle_signal(MODEM + "/voicecall01", "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
    assert methods(bus).index("Register") < methods(bus).index("Dial")
    assert next(op[2] for op in bus.operations if op[0] == "Register") == [modem.AGENT_PATH, b"\x01"]
    assert modem.send_dtmf("12#")
    modem.hangup()
    assert methods(bus).count("Unregister") == 1
    modem.close()
    assert methods(bus).count("Unregister") == 1


def test_dial_failure_releases_audio_ownership():
    modem, bus = connected_modem()
    bus.fail_dial = True
    with pytest.raises(RuntimeError, match="fake dial"):
        modem.dial("+15550000101")
    assert methods(bus).count("Unregister") == 1
    assert not modem.ai_owned
    modem.close()


def test_incoming_is_event_only_even_when_owner_answers():
    modem, bus = connected_modem()
    rings, ended = [], []
    modem.on_ring(rings.append)
    modem.on_hangup(lambda: ended.append(True))
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallAdded",
                        [MODEM + "/incoming", {"State": "incoming", "LineIdentification": "+15550000102"}])
    modem.handle_signal(MODEM + "/incoming", "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [MODEM + "/incoming"])
    assert rings == ["+15550000102"]
    assert ended == []  # Never route owner's hangup through AI cleanup/HangupAll.
    with pytest.raises(RuntimeError, match="Phase 1"):
        modem.answer()
    assert not {"Register", "Answer", "HangupAll"}.intersection(methods(bus))
    modem.close()


@pytest.mark.parametrize("drop", ["offline", "removed"])
def test_disconnect_reconnect_backoff_and_dial_gate(drop):
    modem, bus = connected_modem()
    changes = []
    modem.on_connection_state(changes.append)
    bus.online = False if drop == "offline" else None
    if drop == "offline":
        modem.handle_signal(MODEM, "org.ofono.Modem", "PropertyChanged", ["Online", False])
    else:
        modem.handle_signal("/", "org.ofono.Manager", "ModemRemoved", [MODEM])
    assert not modem.is_connected()
    with pytest.raises(RuntimeError, match="手机未连接"):
        modem.dial("+15550000101")
    for now in (0, 0.5, 1, 1.5, 2, 5):
        modem.poll_once(now)
    assert methods(bus).count("ConnectProfile") == 2
    assert all(op[2] == [modem.HFP_AG_UUID] for op in bus.operations if op[0] == "ConnectProfile")
    bus.online = True
    modem.poll_once(6)
    assert modem.is_connected()
    assert changes[-1] is True
    modem.close()


def test_audio_connect_fallback_requires_active_and_actual_packets():
    modem, bus = connected_modem()
    modem.dial("+15550000101")
    modem.handle_signal(MODEM + "/voicecall01", "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
    started = modem.active_since
    modem.poll_once(started + 3.9)
    assert "Connect" not in methods(bus)
    modem.poll_once(started + 4.1)
    assert methods(bus).count("Connect") == 1
    assert modem.audio_bridge.packet_count == 0  # Connect acceptance is not SCO readiness.
    modem.hangup()
    modem.poll_once(started + 10)
    assert methods(bus).count("Connect") == 1
    modem.close()


class FakePoll:
    def __init__(self, results):
        self.results = iter(results)
        self.timeouts = []

    def register(self, fd, mask):
        self.mask = mask

    def poll(self, timeout):
        self.timeouts.append(timeout)
        return next(self.results)


def test_defer_setup_authorizes_only_if_not_writable():
    writable = getattr(select, "POLLOUT", 4)
    poll = FakePoll([[], [(7, writable)]])
    reads = []
    authorize_sco(7, poller=poll, read=lambda fd, n: reads.append((fd, n)) or b"")
    assert reads == [(7, 1)]
    assert poll.timeouts == [0, 5000]
    poll = FakePoll([[(7, writable)]])
    authorize_sco(7, poller=poll, read=lambda *_: pytest.fail("already authorized"))


@pytest.mark.parametrize("events", [[], [(7, 8)], [(7, 16)], [(7, 4 | 8)]])
def test_defer_setup_rejects_timeout_and_error(events):
    with pytest.raises(OSError):
        authorize_sco(7, poller=FakePoll([[], events]), read=lambda *_: b"")


@pytest.mark.skipif(os.name != "posix", reason="POSIX SCO fd I/O; pure policy tests run on Windows")
def test_sco_socketpair_one_write_per_packet_and_stop_closes_fd():
    local, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    fd = local.detach()
    bridge = BluetoothAudioBridge()
    observed = []
    from agentcall.audio_bridge import AgentPcm
    bridge.set_output_observer(observed.append)
    bridge.write_modem_chunks([AgentPcm(b"ab")])
    bridge.accept_fd(fd, 1)
    peer.settimeout(1)
    peer.send(b"x" * 60)
    assert peer.recv(1024) == b"ab" + b"\0" * 58
    assert bridge.read_modem_chunk() == b"x" * 60
    peer.send(b"y" * 48)
    assert peer.recv(1024) == b"\0" * 48
    assert observed == [b"ab"]
    assert bridge.packet_count == 2
    bridge.stop()
    assert not any(t.name == bridge.thread_name and t.is_alive() for t in threading.enumerate())
    with pytest.raises(OSError):
        os.fstat(fd)
    peer.close()


def make_service(modem, tmp_path, hub=None):
    from agentcall.call_agent import CallAgentService
    from agentcall.call_log import CallLogger
    return CallAgentService("unused", "unused", provider="qwen", modem=modem, hub=hub,
                            call_logger=CallLogger(tmp_path, recording_enabled=True))


def test_service_incoming_never_starts_session_and_offline_guard(tmp_path, monkeypatch):
    modem, bus = connected_modem()
    service = make_service(modem, tmp_path)
    events = []
    monkeypatch.setattr(service, "_publish", events.append)
    monkeypatch.setattr(service.session, "start", lambda **_: pytest.fail("incoming started AI"))
    modem._on_ring("+15550000102")
    assert events[-1]["status"] == "incoming_skipped"
    assert "Register" not in methods(bus)
    modem._offline()
    ok, message = service.dial("+15550000101")
    assert not ok and "手机未连接" in message
    modem.close()


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("failure", [None, "agent_start", "agent_factory"])
def test_call_session_real_hfp_bridge_cleanup_and_records(tmp_path, monkeypatch, parallel, failure):
    from fakes import FakeAgent

    from agentcall import call_agent

    modem, bus = connected_modem()
    service = make_service(modem, tmp_path)
    session = service.session
    session._outbound_number = "+15550000101"
    session._active = True
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel).lower())
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("RECORDING_ENABLED", "true")
    agent = FakeAgent()

    def create_agent(provider):
        if failure == "agent_factory":
            raise RuntimeError("fake factory")
        return agent

    async def start(callback):
        if failure == "agent_start":
            raise RuntimeError("fake startup")
        agent._on_audio_out = callback

    async def wait_connected(**kwargs):
        modem.handle_signal(MODEM + "/voicecall01", "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
        return True

    async def run_loop(agent, bridge, record, transcripts):
        assert bridge is modem.audio_bridge
        assert session._resolve_dtmf_mode() == "qvts"
        assert session.send_dtmf("1") == (True, None)
        record.write_uplink(b"\0\0" * 160)
        session._make_transcript_handler(record, transcripts, agent)("user", "Fictional balance is available")
        session._active = False
        return bridge

    monkeypatch.setattr(call_agent, "create_agent", create_agent)
    monkeypatch.setattr(agent, "start", start)
    monkeypatch.setattr(session, "_wait_connected", wait_connected)
    monkeypatch.setattr(session, "_run_agent_loop", run_loop)
    if failure:
        with pytest.raises(RuntimeError, match="fake"):
            asyncio.run(session._handle_call())
    else:
        asyncio.run(session._handle_call())
    assert not modem.ai_owned
    assert methods(bus).count("Unregister") == 1
    records = list(tmp_path.glob("*/meta.json"))
    assert len(records) == 1
    meta = json.loads(records[0].read_text())
    assert meta["direction"] == "outbound"
    assert meta["status"] == ("failed" if failure else "completed")
    assert records[0].with_name("events.jsonl").exists()
    modem.close()


def test_sms_tools_return_immediately_without_reading_old_messages(tmp_path, monkeypatch):
    from agentcall.events import EventHub

    modem, bus = connected_modem()
    loop = asyncio.new_event_loop()
    hub = EventHub(loop)
    service = make_service(modem, tmp_path, hub)
    monkeypatch.setattr(hub, "wait_for_event", lambda *a, **kw: pytest.fail("SMS wait"))
    monkeypatch.setattr(hub, "history", lambda: pytest.fail("stale SIM SMS read"))
    from agentcall.call_tools import CallTools
    tools = CallTools(modem, hub=hub, get_caller=lambda: "+15550000101", get_record=lambda: None,
                      schedule_hangup=lambda: None)
    for handler in (tools._send_sms, tools._wait_for_sms, tools._query_code):
        assert handler({})["code"] == "SMS_UNSUPPORTED"
    service.session._prompt_gen_result = {"result_verification": "carrier_sms"}
    service.session._apply_prompt_gen_result()
    assert service.session._result_verification_mode == "none"
    modem.close()
    loop.close()


def test_factories_defaults_and_optional_imports(monkeypatch):
    import sys

    from agentcall import config
    from agentcall.audio_bridge import create_audio_bridge
    from agentcall.modem import Eg25Modem, Sim7600Modem, create_modem

    monkeypatch.delenv("MODEM_VENDOR", raising=False)
    assert config.get_str("MODEM_VENDOR") == "quectel"
    assert isinstance(create_modem("unused"), Eg25Modem)
    assert isinstance(create_modem("unused", vendor="simcom"), Sim7600Modem)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("BLUETOOTH_PHONE_ADDRESS", ADDRESS)
    phone = create_modem("unused", vendor="bluetooth")
    assert create_audio_bridge("nmea", "unused", None, 1, modem=phone) is phone.audio_bridge
    for platform in ("darwin", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        with pytest.raises(RuntimeError, match="Linux only"):
            create_modem("unused", vendor="bluetooth")


def test_carrier_name_mapping_uses_existing_table_and_override(monkeypatch):
    from agentcall.sim_identity import identify, identify_hfp, with_service_number_override
    assert identify_hfp({"Name": "中国移动", "Status": "registered"}).service_number == identify(
        "460000000000001", "+CREG: 1"
    ).service_number
    unknown = identify_hfp({"Name": "Example Operator", "Status": "roaming"})
    assert unknown.registered and unknown.service_number == ""
    assert with_service_number_override(unknown, "611").service_number == "611"
    assert with_service_number_override(unknown, "+15550000101").service_number == ""


@pytest.mark.parametrize("codec,card,owned", [(2, CARD, True), (1, "/other", True), (1, CARD, False)])
@pytest.mark.skipif(os.name != "posix", reason="POSIX fd ownership")
def test_unsolicited_audio_is_rejected_and_fd_closed(codec, card, owned):
    modem, bus = connected_modem()
    if owned:
        modem.dial("+15550000101")
    local, peer = socket.socketpair()
    fd = local.detach()
    assert not modem._accept_audio(card, fd, codec)
    with pytest.raises(OSError):
        os.fstat(fd)
    peer.close()
    modem.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd ownership")
def test_failed_sco_authorization_closes_fd_then_card_fallback(monkeypatch):
    modem, bus = connected_modem()
    modem.dial("+15550000101")
    modem.handle_signal(MODEM + "/voicecall01", "org.ofono.VoiceCall", "PropertyChanged", ["State", "active"])
    def fail(fd):
        raise OSError("fake authorization timeout")
    monkeypatch.setattr("agentcall.bluetooth_audio.authorize_sco", fail)
    local, peer = socket.socketpair()
    fd = local.detach()
    modem._accept_audio(CARD, fd, 1)
    modem.audio_bridge._thread.join(timeout=1)
    with pytest.raises(OSError):
        os.fstat(fd)
    modem.poll_once(modem.active_since + 4.1)
    assert "Connect" in methods(bus)
    peer.close()
    modem.close()


def test_call_removed_and_online_false_release_once(tmp_path):
    modem, bus = connected_modem()
    service = make_service(modem, tmp_path)
    modem.dial("+15550000101")
    modem.handle_signal(MODEM, "org.ofono.VoiceCallManager", "CallRemoved", [MODEM + "/voicecall01"])
    modem.handle_signal(MODEM, "org.ofono.Modem", "PropertyChanged", ["Online", False])
    assert methods(bus).count("Unregister") == 1
    assert not service.modem_connected
    assert not service.session.is_busy
    modem.close()


def test_hfp_api_meta_and_sms_degradation(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from agentcall.events import EventHub
    from agentcall.web.server import build_app

    modem, bus = connected_modem()
    async def check():
        hub = EventHub(asyncio.get_running_loop())
        service = make_service(modem, tmp_path, hub)
        monkeypatch.setattr("agentcall.web.server.detect_quectel_usb_online", lambda: False)
        app = build_app(hub, modem, service, {"provider": "qwen", "port": "unused"})
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/meta")
            meta = await response.json()
            assert meta["hardware"]["phone_connected"]
            assert meta["hardware"]["modem_connected"]
            assert meta["hardware"]["usb_online"] is False
            assert meta["hardware"]["signal_strength"] == 67
            assert meta["sim"]["carrier"] == "中国移动" and meta["sim"]["plmn"] == ""
            assert meta["capabilities"] == {"sms": False, "auto_answer": False}
            assert ADDRESS not in json.dumps(meta)
            for endpoint in ("/api/sms/send", "/api/setup/test_sms"):
                response = await client.post(endpoint, json={"number": "+15550000101", "text": "test"})
                assert response.status == 200
                result = await response.json()
                assert result["code"] == "SMS_UNSUPPORTED" and not result["ok"]
    asyncio.run(check())
    modem.close()


def test_registration_failure_and_unregister_failure_drop_bus_owner(monkeypatch):
    modem, bus = connected_modem()
    original = bus.call
    def fail(destination, path, interface, member, signature="", body=None):
        if member in {"Register", "Unregister"}:
            bus.operations.append((member, path, body))
            raise RuntimeError("fake daemon timeout")
        return original(destination, path, interface, member, signature, body)
    monkeypatch.setattr(bus, "call", fail)
    with pytest.raises(RuntimeError, match="fake daemon"):
        modem.dial("+15550000101")
    assert methods(bus).count("Unregister") == 1
    assert "close" in bus.operations and not modem.ai_owned
    modem.close()


def test_rx_drains_sco_packets_faster_than_the_session_10ms_loop():
    bridge = BluetoothAudioBridge()
    packets = [bytes([i]) * 60 for i in range(6)]
    for packet in packets:
        bridge._downlink.put_nowait(packet)
    assert bridge.read_modem_chunk() == b"".join(packets)
    assert bridge._downlink.empty()


def test_owner_call_blocks_dial_even_before_listener_updates_cache(tmp_path):
    modem, bus = connected_modem()
    service = make_service(modem, tmp_path)
    bus.calls = [[MODEM + "/owner", {"State": "active"}]]
    with pytest.raises(RuntimeError, match="手机正在通话"):
        modem.dial("+15550000101")
    assert "Register" not in methods(bus)
    modem.poll_once(1)
    assert service.line_busy()
    assert service.dial("+15550000101")[0] is False
    bus.calls = []
    modem.poll_once(2)
    assert not service.line_busy()
    modem.close()


def test_strength_only_change_emits_refresh_without_fake_imsi(monkeypatch):
    modem, bus = connected_modem()
    identities = []
    modem.on_sim_identity(identities.append)
    original = bus.call
    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == "GetProperties":
            result[0]["Strength"] = 42
        return result
    monkeypatch.setattr(bus, "call", change)
    modem.poll_once(1)
    assert len(identities) == 1 and modem.signal_strength == 42
    assert identities[0].plmn == ""
    modem.close()


def test_audio_stop_failure_still_unregisters(monkeypatch):
    modem, bus = connected_modem()
    modem.dial("+15550000101")
    def fail():
        raise RuntimeError("fake stalled worker")
    monkeypatch.setattr(modem.audio_bridge, "stop", fail)
    with pytest.raises(RuntimeError, match="fake stalled"):
        modem.hangup()
    assert methods(bus).count("Unregister") == 1
    monkeypatch.undo()
    modem.close()


def test_non_ai_remote_dialer_cannot_claim_hfp_audio(tmp_path, monkeypatch):
    modem, bus = connected_modem()
    service = make_service(modem, tmp_path)
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    assert service.remote_dialer_status()["enabled"] is False
    assert "AI" in service._reserve_remote_line(None)
    assert "Register" not in methods(bus)
    modem.close()
