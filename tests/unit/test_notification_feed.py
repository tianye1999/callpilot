"""通知事件全程使用内存和假回调，不接触设备。"""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from agentcall import notification_events as notifications
from agentcall.events import EventHub
from agentcall.web import server


def test_feed_sequence_ring_and_restart():
    feed = notifications.NotificationFeed(limit=2)
    for n in range(3):
        feed.info("test", "title", str(n), "#sms")
    result = feed.read(0)
    assert [e["seq"] for e in result["events"]] == [2, 3]
    assert result["seq"] == 3
    assert result["truncated"] is True
    assert feed.read(2)["events"][0]["body"] == "2"
    assert feed.read(3)["events"] == []
    assert notifications.NotificationFeed().read(0)["epoch"] != result["epoch"]


def test_confirm_snapshot_survives_ring_and_expires():
    now = [100.0]
    feed = notifications.NotificationFeed(limit=2, clock=lambda: now[0])
    feed.confirm_requested("abc", "Proceed?", 60)
    for _ in range(3):
        feed.info("test", "title", "body", "#sms")
    assert feed.read(0)["confirmations"][0]["id"] == "abc"
    now[0] = 161
    assert feed.read(0)["confirmations"] == []


def test_confirm_closed_removes_snapshot():
    feed = notifications.NotificationFeed()
    feed.confirm_requested("abc", "Proceed?", 60)
    feed.confirm_closed("abc")
    result = feed.read(0)
    assert result["confirmations"] == []
    assert result["events"][-1]["kind"] == "confirm_closed"
    assert result["events"][-1]["target"] == "#confirm/abc"


@pytest.mark.parametrize("body", [
    "123456", "Use AB12CD now", "1 2 3 4 5 6", "１２３４５６", "Code: ABCDEF", "ＡＢＣＤＥＦ",
])
def test_sms_preview_does_not_expose_possible_codes(body):
    assert notifications.sms_preview(body) == "收到一条短信"


def test_sms_preview_is_short():
    body = "明天见面聊聊项目进展，然后一起去吃午饭，再讨论下一步"
    assert notifications.sms_preview(body) == body[:20] + "…"


def test_feed_http_local_and_cursor(monkeypatch):
    async def run():
        feed = notifications.NotificationFeed()
        monkeypatch.setattr(notifications, "feed", feed)
        feed.info("test", "Title", "Body", "#sms")
        app = server.build_app(EventHub(asyncio.get_running_loop()), None)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/notify/feed?after=0")
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert (await response.json())["events"][0]["seq"] == 1
            for cursor in ("-1", "abc"):
                assert (await client.get(f"/api/notify/feed?after={cursor}")).status == 400
            assert (await client.get("/api/notify/feed", headers={"Origin": "https://evil.example"})).status == 403
            assert (await client.get("/api/notify/feed", headers={"Host": "evil.example"})).status == 403
    asyncio.run(run())


def test_feed_rejects_remote_even_with_local_origin():
    request = make_mocked_request("GET", "/api/notify/feed", headers={"Host": "localhost"})
    request = request.clone(remote="192.0.2.1")
    with pytest.raises(server.web.HTTPForbidden):
        asyncio.run(server._notification_feed(request))


def test_summary_hook_only_successful_one_sentence(tmp_path, monkeypatch):
    from agentcall.call_log import CallLogger

    feed = notifications.NotificationFeed()
    monkeypatch.setattr(notifications, "feed", feed)
    record = CallLogger(tmp_path).begin_call("outbound", "+12025550123")
    record.set_summary({"ok": False, "summary": "失败"})
    assert feed.read(0)["events"] == []
    record.set_summary({"ok": True, "summary": "已完成预约。第二句不显示。"})
    event = feed.read(0)["events"][0]
    assert event["body"] == "已完成预约。"
    assert event["target"] == f"#history/{record.id}"
    assert "+12025550123" in record.id


def test_answered_hook_inbound_only(monkeypatch):
    from types import SimpleNamespace

    from agentcall.call_agent import CallSession

    feed = notifications.NotificationFeed()
    monkeypatch.setattr(notifications, "feed", feed)
    session = object.__new__(CallSession)
    session.hub = None
    session._record = SimpleNamespace(id="20261029-120000-inbound-+12025550123")
    session._outbound_number = "+12025550123"
    event = {"type": "call", "status": "answered", "caller": "+12025550123"}
    session._publish(event)
    assert feed.read(0)["events"] == []
    session._outbound_number = None
    session._publish(event)
    assert feed.read(0)["events"][0]["target"] == "#history/" + session._record.id


def test_notification_failure_never_escapes_shared_hook(monkeypatch):
    from types import SimpleNamespace

    from agentcall.call_agent import CallSession

    def broken(*args):
        raise RuntimeError("通知失败")

    monkeypatch.setattr(notifications.feed, "info", broken)
    events = []
    session = object.__new__(CallSession)
    session.hub = SimpleNamespace(publish=events.append)
    session._record = None
    session._outbound_number = None
    event = {"type": "call", "status": "answered", "caller": "+12025550123"}
    session._publish(event)
    assert events == [event]


def test_modem_backfill_marks_only_stored_sms(monkeypatch):
    from agentcall.modem import SerialModem
    from agentcall.sms_pdu import SmsDeliver

    feed = notifications.NotificationFeed()
    modem = SerialModem("unused-fake-port")
    modem._send = lambda command: "OK"
    modem._parse_cmgl = lambda response: [("1", SmsDeliver("10086", "", "历史短信"))]
    modem._on_sms = lambda sender, body, timestamp: feed.sms_received(sender, body)
    monkeypatch.setenv("SMS_DELETE_AFTER_INGEST", "false")
    modem._dump_stored_sms()
    assert feed.read(0)["events"] == []
    modem._on_sms("+12025550123", "新的短信", "")
    assert len(feed.read(0)["events"]) == 1


def test_history_target_reads_exact_record_and_detects_active(tmp_path):
    from types import SimpleNamespace

    from agentcall.call_log import CallLogger

    record = CallLogger(tmp_path).begin_call("inbound", "+12025550123")
    record.finish("completed")

    async def run():
        session = SimpleNamespace(is_active=True, _record=record)
        service = SimpleNamespace(session=session, call_logger=SimpleNamespace(base_dir=tmp_path))
        app = server.build_app(EventHub(asyncio.get_running_loop()), None, service=service)
        async with TestClient(TestServer(app)) as client:
            response = await client.get(f"/api/history/{record.id}")
            assert (await response.json())["active"] is True
            session.is_active = False
            response = await client.get(f"/api/history/{record.id}")
            assert (await response.json())["number"] == "+12025550123"
            assert (await client.get("/api/history/missing")).status == 404
    asyncio.run(run())
