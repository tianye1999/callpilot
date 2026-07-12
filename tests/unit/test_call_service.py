"""CallAgentService / CallSession 行为单测（FakeModem 驱动，无硬件）。"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.events import EventHub
from agentcall.remote_dialer import IssuedLiveKitSession, RemoteDialerInvite


def make_service(
    modem: FakeModem,
    hub: EventHub | None = None,
    audio_mode: str = "uac",
    sms_email_forwarder=None,
) -> CallAgentService:
    kwargs = {}
    if sms_email_forwarder is not None:
        kwargs["sms_email_forwarder"] = sms_email_forwarder
    return CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="qwen",
        audio_mode=audio_mode,
        hub=hub,
        modem=modem,  # type: ignore[arg-type]  # FakeModem 与 Eg25Modem 同形
        **kwargs,
    )


def make_hub() -> EventHub:
    return EventHub(asyncio.new_event_loop())


class FakeSmsEmailForwarder:
    def __init__(self, hub: EventHub | None = None) -> None:
        self.hub = hub
        self.enqueued: list[tuple[str | None, str]] = []
        self.history_at_enqueue: list[dict] = []
        self.stopped = False

    def enqueue(self, sender: str | None, body: str, **_kwargs) -> bool:
        self.enqueued.append((sender, body))
        if self.hub is not None:
            self.history_at_enqueue = self.hub.history()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self.stopped = True


class SpyCallRecord:
    def __init__(self) -> None:
        self.downlink: list[bytes] = []
        self.events: list[tuple[str, dict]] = []

    def write_downlink(self, pcm: bytes) -> None:
        self.downlink.append(pcm)

    def log_event(self, type: str, **fields) -> None:  # noqa: A002
        self.events.append((type, fields))


class FakeRemoteCoordinator:
    def __init__(self) -> None:
        self.call_active = threading.Event()
        self.stop_reasons: list[str] = []
        self.commands: list[dict] = []

    def request_call_stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)

    def submit_local_command(self, command: dict) -> bool:
        self.commands.append(command)
        return True

    def status(self) -> dict:
        return {"status": "media_ready", "call_active": self.call_active.is_set()}


class FakeRemoteWorker:
    def __init__(self, coordinator: FakeRemoteCoordinator) -> None:
        self.coordinator = coordinator
        self.is_running = False
        self.started = False
        self.stop_reasons: list[str] = []

    def start(self, timeout: float = 10.0) -> None:
        self.started = True
        self.is_running = True

    def stop(self, reason: str, **_kwargs) -> None:
        self.stop_reasons.append(reason)
        self.is_running = False


# ---- RING 去重 ----

def test_ring_starts_session_once(monkeypatch):
    modem = FakeModem()
    service = make_service(modem)
    starts: list[str | None] = []

    def fake_start(outbound_number: str | None = None, task: str | None = None) -> None:
        starts.append(outbound_number)
        service.session._active = True  # 模拟会话进行中

    monkeypatch.setattr(service.session, "start", fake_start)

    modem.trigger_ring("13800000000")
    modem.trigger_ring("13800000000")  # RING 与 CLCC 轮询重复触发

    assert starts == [None]


# ---- 收到短信后邮件转发 ----


def test_new_sms_is_published_before_nonblocking_email_enqueue():
    modem = FakeModem()
    hub = make_hub()
    forwarder = FakeSmsEmailForwarder(hub)
    make_service(modem, hub=hub, sms_email_forwarder=forwarder)

    modem.trigger_sms("10086", "您的验证码是 482913")

    assert forwarder.enqueued == [("10086", "您的验证码是 482913")]
    assert forwarder.history_at_enqueue[-1]["type"] == "sms_in"
    assert forwarder.history_at_enqueue[-1]["text"] == "您的验证码是 482913"


def test_service_does_not_replay_sms_history_to_email_forwarder():
    modem = FakeModem()
    hub = make_hub()
    hub.publish({"type": "sms_in", "sender": "10086", "text": "历史短信"})
    forwarder = FakeSmsEmailForwarder(hub)

    make_service(modem, hub=hub, sms_email_forwarder=forwarder)

    assert forwarder.enqueued == []


def test_stop_service_stops_sms_email_worker():
    modem = FakeModem()
    forwarder = FakeSmsEmailForwarder()
    service = make_service(modem, sms_email_forwarder=forwarder)

    service.stop_service()

    assert forwarder.stopped is True
    assert ("close", ()) in modem.calls


# ---- 外呼互斥与等待接通 ----

def test_dial_rejected_when_session_active(monkeypatch):
    modem = FakeModem()
    service = make_service(modem)
    monkeypatch.setattr(
        service.session, "start",
        lambda outbound_number=None, task=None, preset_hint=None: setattr(service.session, "_active", True),
    )

    ok, err = service.dial("13900000000")
    assert ok

    ok2, err2 = service.dial("13911111111")
    assert not ok2
    assert err2


def test_dial_empty_number_rejected():
    service = make_service(FakeModem())
    ok, err = service.dial("  ")
    assert not ok


def test_wait_connected_times_out():
    modem = FakeModem()
    session = make_service(modem).session
    session._active = True

    assert asyncio.run(session._wait_connected(timeout=0.2)) is False


def test_wait_connected_success():
    modem = FakeModem()
    session = make_service(modem).session
    session._active = True
    modem.trigger_call_connected()

    assert asyncio.run(session._wait_connected(timeout=0.2)) is True


# ---- 服务层高层方法：hangup / send_dtmf ----

def test_service_hangup_requires_active_call(monkeypatch):
    service = make_service(FakeModem())
    ok, err = service.hangup()
    assert not ok and "没有进行中的通话" in (err or "")

    stopped = []
    monkeypatch.setattr(service.session, "stop", lambda: stopped.append(True))
    service.session._active = True
    ok, err = service.hangup()
    assert ok and err is None
    assert stopped == [True]


def test_service_send_dtmf_requires_active_call():
    modem = FakeModem()
    sent: list[str] = []
    modem.send_dtmf = lambda digits: sent.append(digits) or True  # type: ignore[attr-defined]
    service = make_service(modem, audio_mode="nmea")

    ok, err = service.send_dtmf("103#")
    assert not ok and "没有进行中的通话" in (err or "")
    assert sent == []

    service.session._active = True
    ok, err = service.send_dtmf("103#")
    assert ok and err is None
    assert sent == ["103#"]


def test_service_send_dtmf_uses_inband_audio_for_uac(monkeypatch):
    monkeypatch.setenv("DTMF_MODE", "inband")
    modem = FakeModem()
    service = make_service(modem, audio_mode="uac")
    service.session._active = True
    record = SpyCallRecord()
    service.session._record = record  # type: ignore[assignment]

    ok, err = service.send_dtmf("5")

    assert ok and err is None
    assert modem.calls == []
    bridge = FakeAudioBridge()
    service.session._drain_agent_audio(bridge)
    assert len(bridge.downlink) == 1
    tone = bridge.downlink[0]
    # #80-D 实际送桥 payload:lead 100ms 静音 + tone 200ms + tail 120ms
    # 静音 = 420ms（单键无末尾 gap，gap 仅用于 digit 之间）。
    assert len(tone) == int(8000 * 0.42) * 2
    samples = np.frombuffer(tone, dtype=np.int16)
    lead = int(8000 * 0.10)
    tone_end = lead + int(8000 * 0.20)
    assert np.all(samples[:lead] == 0)          # 头部隔离带
    assert np.any(samples[lead:tone_end] != 0)  # 双音本体
    assert np.all(samples[tone_end:] == 0)      # 尾部隔离带（单键无末尾 gap）
    assert record.downlink == [tone]
    assert record.events == [
        ("dtmf", {"count": 1, "mode": "inband", "result": "success"})
    ]


def test_service_send_dtmf_keeps_qvts_for_nmea(monkeypatch):
    monkeypatch.setenv("DTMF_MODE", "inband")
    modem = FakeModem()
    service = make_service(modem, audio_mode="nmea")
    service.session._active = True

    ok, err = service.send_dtmf("5")

    assert ok and err is None
    assert ("send_dtmf", ("5",)) in modem.calls
    assert service.session._outgoing_audio.empty()


def test_service_send_dtmf_reports_modem_failure():
    modem = FakeModem()
    modem.send_dtmf = lambda digits: False  # type: ignore[attr-defined]
    service = make_service(modem, audio_mode="nmea")
    service.session._active = True
    record = SpyCallRecord()
    service.session._record = record  # type: ignore[assignment]

    ok, err = service.send_dtmf("73#")

    assert not ok and "按键发送失败" in (err or "")
    assert record.events == [
        ("dtmf", {"count": 3, "mode": "qvts", "result": "failure"})
    ]
    assert "73#" not in str(record.events)


def test_service_send_dtmf_mode_resolution_failure_is_redacted(monkeypatch):
    modem = FakeModem()
    service = make_service(modem, audio_mode="uac")
    service.session._active = True
    record = SpyCallRecord()
    service.session._record = record  # type: ignore[assignment]

    def fail_config_read(_key: str) -> str:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("agentcall.call_agent.config.get_str", fail_config_read)

    ok, err = service.send_dtmf("73#")

    assert not ok and "按键发送失败" in (err or "")
    assert record.events == [
        ("dtmf", {"count": 3, "mode": "unknown", "result": "failure"})
    ]
    assert "73#" not in str(record.events)


# ---- 远程网页拨号会话 ----


def test_remote_invite_default_off_does_not_start_worker(monkeypatch):
    monkeypatch.delenv("REMOTE_WEB_DIALER_ENABLED", raising=False)
    service = make_service(FakeModem())
    built: list[bool] = []
    monkeypatch.setattr(
        service,
        "_build_remote_worker",
        lambda: built.append(True),
    )

    invite, error = service.create_remote_dialer_invite()

    assert invite is None
    assert "未启用" in (error or "")
    assert built == []


def test_remote_invite_starts_once_and_reuses_active_invite(monkeypatch):
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    service = make_service(FakeModem())
    coordinator = FakeRemoteCoordinator()
    worker = FakeRemoteWorker(coordinator)
    invite = RemoteDialerInvite(
        session_id="session-1",
        url="https://dial.example/#short-lived-token",
        expires_at=time.time() + 300,
    )
    builds: list[bool] = []

    def build():
        builds.append(True)
        return invite, worker

    monkeypatch.setattr(service, "_build_remote_worker", build)

    first, first_error = service.create_remote_dialer_invite()
    second, second_error = service.create_remote_dialer_invite()

    assert first_error is None and second_error is None
    assert first == second == {
        "session_id": "session-1",
        "url": invite.url,
        "expires_at": invite.expires_at,
    }
    assert worker.started is True
    assert builds == [True]


def test_paired_remote_session_never_reuses_or_interrupts_an_active_worker(monkeypatch):
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    service = make_service(FakeModem())
    worker = FakeRemoteWorker(FakeRemoteCoordinator())
    invite = RemoteDialerInvite(
        session_id="session-1",
        url="https://dial.example/#short-lived-token",
        expires_at=time.time() + 300,
    )
    monkeypatch.setattr(service, "_build_remote_worker", lambda: (invite, worker))

    first, first_error = service.create_paired_remote_dialer_invite("device-a-123456789")
    same_device, same_error = service.create_paired_remote_dialer_invite("device-a-123456789")
    other_device, other_error = service.create_paired_remote_dialer_invite("device-b-123456789")
    legacy, legacy_error = service.create_remote_dialer_invite()

    assert first_error is None
    assert first and first["session_id"] == "session-1"
    assert same_device is None and "正在使用" in (same_error or "")
    assert other_device is None and "正在使用" in (other_error or "")
    assert legacy is None and "已配对手机" in (legacy_error or "")
    assert worker.stop_reasons == []


def test_remote_worker_uses_remote_dtmf_mode_not_ai_call_mode(monkeypatch):
    monkeypatch.setenv("REMOTE_DTMF_MODE", "qvts")
    monkeypatch.setenv("DTMF_MODE", "inband")
    monkeypatch.setenv("REMOTE_MEDIA_PROVIDER", "livekit")
    monkeypatch.setenv("LIVEKIT_URL", "wss://project.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "api-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "api-secret")
    monkeypatch.setenv(
        "REMOTE_CONTROL_URL", "https://dial.example/remote_dialer.html"
    )
    invite = RemoteDialerInvite(
        session_id="session-1",
        url="https://dial.example/#token",
        expires_at=time.time() + 300,
    )
    issued = IssuedLiveKitSession(
        invite=invite,
        room_name="room",
        browser_identity="browser",
        edge_identity="edge",
        browser_token="browser-token",
        edge_token="edge-token",
        livekit_url="wss://project.livekit.cloud",
    )
    monkeypatch.setattr(
        "agentcall.call_agent.issue_livekit_session", lambda **_kwargs: issued
    )
    monkeypatch.setattr(
        "agentcall.livekit_media.LiveKitRemoteMediaEndpoint",
        lambda _issued: object(),
    )

    _invite, worker = make_service(FakeModem())._build_remote_worker()

    assert worker.coordinator.runtime.dtmf_mode == "qvts"
    assert worker.coordinator.runtime.recording_enabled is False


def test_cloud_remote_session_uses_server_token_without_local_signing_secret(monkeypatch):
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_CLOUD_ENABLED", "true")
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    service = make_service(FakeModem())
    worker = FakeRemoteWorker(FakeRemoteCoordinator())
    issued: list[IssuedLiveKitSession] = []

    def build(server_issued: IssuedLiveKitSession):
        issued.append(server_issued)
        return worker

    monkeypatch.setattr(service, "_build_remote_worker_for_issued", build)
    command = {
        "callId": "call_abcdefghijkl",
        "expiresAtUnix": time.time() + 300,
        "session": {
            "sessionId": "session_abcdefghijkl",
            "roomName": "callpilot_abcdefghijkl",
            "browserIdentity": "web_abcdefghijkl",
            "edgeIdentity": "edgepart_abcdefghijkl",
            "livekitUrl": "wss://project.livekit.cloud",
            "token": "server-issued-edge-token",
        },
    }

    ok, error = service.start_cloud_remote_session(command)

    assert ok is True and error is None
    assert worker.started is True
    assert issued[0].edge_token == "server-issued-edge-token"
    assert issued[0].browser_token == ""
    assert service._remote_session_device_id == "cloud:call_abcdefghijkl"


def test_cloud_remote_session_default_off_is_byte_compatible(monkeypatch):
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.delenv("REMOTE_CLOUD_ENABLED", raising=False)
    service = make_service(FakeModem())
    built: list[bool] = []
    monkeypatch.setattr(
        service, "_build_remote_worker_for_issued", lambda _issued: built.append(True)
    )

    ok, error = service.start_cloud_remote_session({})

    assert ok is False and error == "CLOUD_DISABLED"
    assert built == []


def test_expired_remote_invite_stops_old_worker_before_replacement(monkeypatch):
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    service = make_service(FakeModem())
    old_worker = FakeRemoteWorker(FakeRemoteCoordinator())
    old_worker.is_running = True
    service._remote_worker = old_worker  # type: ignore[assignment]
    service._remote_invite = RemoteDialerInvite(
        session_id="expired",
        url="https://dial.example/#expired",
        expires_at=time.time() - 1,
    )
    new_invite = RemoteDialerInvite(
        session_id="new-session",
        url="https://dial.example/#new",
        expires_at=time.time() + 300,
    )
    new_worker = FakeRemoteWorker(FakeRemoteCoordinator())
    monkeypatch.setattr(
        service, "_build_remote_worker", lambda: (new_invite, new_worker)
    )

    payload, error = service.create_remote_dialer_invite()

    assert error is None
    assert payload and payload["session_id"] == "new-session"
    assert old_worker.stop_reasons == ["invite_expired"]
    assert new_worker.started is True


def test_remote_reserved_line_blocks_local_ai_dial_and_routes_hangup(monkeypatch):
    from agentcall import rate_limit

    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_DIAL_LIMIT_PER_HOUR", "0")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    rate_limit.reset_remote_dial_rate_limit_state()
    modem = FakeModem()
    service = make_service(modem)
    coordinator = FakeRemoteCoordinator()
    worker = FakeRemoteWorker(coordinator)
    worker.is_running = True
    service._remote_worker = worker  # type: ignore[assignment]

    assert service._reserve_remote_line(coordinator) is None  # type: ignore[arg-type]
    ok, error = service.dial("10000")
    assert ok is False
    assert "正在通话" in (error or "")

    modem.trigger_hangup()
    assert coordinator.stop_reasons == ["remote_party_hangup"]
    assert ("hangup", ()) not in modem.calls

    service._release_remote_line(coordinator)  # type: ignore[arg-type]
    rate_limit.reset_remote_dial_rate_limit_state()


def test_local_dashboard_dtmf_and_shutdown_route_to_remote_worker(monkeypatch):
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    service = make_service(FakeModem())
    coordinator = FakeRemoteCoordinator()
    worker = FakeRemoteWorker(coordinator)
    worker.is_running = True
    service._remote_worker = worker  # type: ignore[assignment]
    service._remote_call_owner = coordinator  # type: ignore[assignment]

    ok, error = service.send_dtmf("2")
    assert ok is True and error is None
    assert coordinator.commands == [{"type": "dtmf", "digits": "2"}]

    service.stop_service()
    assert worker.stop_reasons == ["service_shutdown"]


def test_remote_dial_reservation_uses_hourly_rate_limit(monkeypatch):
    from agentcall import rate_limit

    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_DIAL_LIMIT_PER_HOUR", "1")
    rate_limit.reset_remote_dial_rate_limit_state()
    service = make_service(FakeModem())
    coordinator = FakeRemoteCoordinator()
    worker = FakeRemoteWorker(coordinator)
    worker.is_running = True
    service._remote_worker = worker  # type: ignore[assignment]

    assert service._reserve_remote_line(coordinator) is None  # type: ignore[arg-type]
    service._release_remote_line(coordinator)  # type: ignore[arg-type]
    error = service._reserve_remote_line(coordinator)  # type: ignore[arg-type]

    assert "过于频繁" in (error or "")
    rate_limit.reset_remote_dial_rate_limit_state()


# ---- 来电全链路：接听 → 开场白 → 下行音频 → 挂断收尾 ----

def test_inbound_call_full_lifecycle(monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "李明")
    monkeypatch.setenv("AGENT_PERSONA", "数字分身")
    modem = FakeModem()
    hub = make_hub()
    bridge = FakeAudioBridge()
    agent = FakeAgent()

    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem, hub=hub)
    modem.trigger_ring("13800000000")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not bridge.downlink:
        time.sleep(0.05)

    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert "answer" in modem.call_names()  # ATA 接听
    assert agent.started and agent.said  # 开场白已发
    assert "李明的数字分身" in agent._session_instructions
    assert "不方便接" in agent.said[0]
    assert bridge.downlink  # 开场白 PCM 写回模组
    assert agent.stopped and bridge.stopped  # 会话收尾
    assert "hangup" in modem.call_names()  # 物理挂断兜底

    statuses = [e["status"] for e in hub.history() if e.get("type") == "call"]
    assert statuses[0] == "ringing"
    assert "answered" in statuses
    assert statuses[-1] == "ended"


def test_outbound_call_uses_digital_twin_prompt(monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "李明")
    monkeypatch.setenv("AGENT_PERSONA", "数字分身")
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()

    monkeypatch.setenv("AGENT_OUTBOUND_TASK", "查询本机套餐和剩余流量")
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem)
    ok, err = service.dial("10000")
    assert ok, err
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and ("dial", ("10000",)) not in modem.calls:
        time.sleep(0.05)
    modem.trigger_call_connected("10000")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not bridge.downlink:
        time.sleep(0.05)

    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert ("dial", ("10000",)) in modem.calls
    assert "李明的数字分身" in agent._session_instructions
    assert "查询本机套餐和剩余流量" in agent._session_instructions
    assert "有什么可以帮您" in agent._session_instructions
    assert "我是李明的数字分身" in agent.said[0]
    assert "让我打" not in agent.said[0]  # 开场白已去掉“让我打来”
    assert "查询本机套餐和剩余流量" in agent.said[0]


def test_duplicate_sms_not_republished_or_reforwarded():
    """补收/重复上报同一短信：去重后不重复入库、不重复转发邮件（#SMS 补收）。"""
    modem = FakeModem()
    hub = make_hub()
    forwarder = FakeSmsEmailForwarder(hub)
    make_service(modem, hub=hub, sms_email_forwarder=forwarder)

    modem.trigger_sms("10086", "余额100元", "26/07/10,14:00:00")
    modem.trigger_sms("10086", "余额100元", "26/07/10,14:00:00")  # 重复（如补收）

    sms_in = [e for e in hub.history() if e.get("type") == "sms_in"]
    assert len(sms_in) == 1                     # 只入库一次
    assert forwarder.enqueued == [("10086", "余额100元")]  # 只转发一次


def test_same_text_different_timestamp_both_delivered():
    """同发件方同正文、时间戳不同 = 两条真实短信，都入库都转发（不误去重）。"""
    modem = FakeModem()
    hub = make_hub()
    forwarder = FakeSmsEmailForwarder(hub)
    make_service(modem, hub=hub, sms_email_forwarder=forwarder)

    modem.trigger_sms("10001", "剩余1.00GB", "26/07/09,14:00:00")
    modem.trigger_sms("10001", "剩余1.00GB", "26/07/10,14:00:00")

    sms_in = [e for e in hub.history() if e.get("type") == "sms_in"]
    assert len(sms_in) == 2
    assert len(forwarder.enqueued) == 2

def test_outbound_opening_mode_wait_skips_opening(monkeypatch, tmp_path):
    """#80-B:profile opening_mode=wait → 外呼接通后不发开场白,等对方先说。"""
    import json as _json

    profiles = tmp_path / "number_profiles.json"
    profiles.write_text(
        _json.dumps(
            {
                "profiles": [
                    {
                        "number": "10000",
                        "scenario": "IVR 热线:按键菜单必须调 send_dtmf",
                        "opening_mode": "wait",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profiles))
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem)
    ok, err = service.dial("10000")
    assert ok, err
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and ("dial", ("10000",)) not in modem.calls:
        time.sleep(0.05)
    modem.trigger_call_connected("10000")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not agent.started:
        time.sleep(0.05)
    time.sleep(0.3)  # 留出会说开场白的窗口(错误实现会在此期间 say)

    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert agent.said == []  # wait 模式:全程未主动开场
    assert "IVR 热线" in agent._session_instructions  # profile scenario 已生效


def test_outbound_opening_mode_default_still_says_opening(monkeypatch, tmp_path):
    """对照:profile 未声明 opening_mode → 行为不变,照说开场白。"""
    import json as _json

    profiles = tmp_path / "number_profiles.json"
    profiles.write_text(
        _json.dumps(
            {"profiles": [{"number": "10000", "scenario": "普通场景"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profiles))
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem)
    ok, err = service.dial("10000")
    assert ok, err
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and ("dial", ("10000",)) not in modem.calls:
        time.sleep(0.05)
    modem.trigger_call_connected("10000")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not agent.said:
        time.sleep(0.05)

    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert len(agent.said) == 1  # 默认行为:开场白照发
