"""CallAgentService / CallSession 行为单测（FakeModem 驱动，无硬件）。"""

from __future__ import annotations

import asyncio
import time

from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.events import EventHub


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
    class SpyRecord:
        def __init__(self) -> None:
            self.downlink: list[bytes] = []
            self.events: list[tuple[str, dict]] = []

        def write_downlink(self, pcm: bytes) -> None:
            self.downlink.append(pcm)

        def log_event(self, type: str, **fields) -> None:  # noqa: A002
            self.events.append((type, fields))

    monkeypatch.setenv("DTMF_MODE", "inband")
    modem = FakeModem()
    service = make_service(modem, audio_mode="uac")
    service.session._active = True
    record = SpyRecord()
    service.session._record = record  # type: ignore[assignment]

    ok, err = service.send_dtmf("5")

    assert ok and err is None
    assert modem.calls == []
    bridge = FakeAudioBridge()
    service.session._drain_agent_audio(bridge)
    assert len(bridge.downlink) == 1
    tone = bridge.downlink[0]
    assert len(tone) == int(8000 * 0.2) * 2
    assert record.downlink == [tone]
    assert record.events == [("dtmf", {"digits": "5", "mode": "inband"})]


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

    ok, err = service.send_dtmf("1")
    assert not ok and "按键发送失败" in (err or "")


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
