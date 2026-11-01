import json

import pytest

from agentcall import config, remote_alert_push
from agentcall.call_log import CallRecord
from agentcall.notification_events import publish_notification, sms_backfill


@pytest.fixture(autouse=True)
def alert_queue(monkeypatch):
    monkeypatch.setattr(remote_alert_push, "queue", remote_alert_push.AlertQueue())
    monkeypatch.setenv("REMOTE_ALERT_PUSH_ENABLED", "true")


def drain():
    sent = []
    remote_alert_push.drain(sent.append)
    return [json.loads(value) for value in sent]


def test_disabled_default_and_no_backfill(monkeypatch):
    monkeypatch.delenv("REMOTE_ALERT_PUSH_ENABLED")
    assert not config.get_bool("REMOTE_ALERT_PUSH_ENABLED")
    publish_notification("sms_received", "private-number", "private-body")
    assert drain() == []
    monkeypatch.setenv("REMOTE_ALERT_PUSH_ENABLED", "true")
    with sms_backfill():
        publish_notification("sms_received", "private-number", "private-body")
    assert drain() == []
    publish_notification("sms_received", "private-number", "private-body")
    events = drain()
    assert len(events) == 1
    assert events[0]["kind"] == "sms"
    assert set(events[0]) == {"v", "type", "kind", "eventId"}
    assert "private" not in json.dumps(events)


@pytest.mark.parametrize("direction,source,answered,status,expected", [
    ("inbound", None, True, "completed", "call_digest"),
    ("inbound", None, False, "completed", "missed_call"),
    ("inbound", None, True, "failed", "missed_call"),
    ("inbound", "remote_web_dialer", True, "completed", None),
    ("inbound", "REMOTE_HANDSET", False, "failed", None),
    ("outbound", None, True, "completed", None),
])
def test_persisted_call_classification(tmp_path, direction, source, answered, status, expected):
    record = CallRecord("private-local-id", tmp_path, direction, "private-number", source=source)
    if answered:
        record.log_event("answered")
    record.finish(status)
    before_summary = drain()
    record.set_summary({"ok": True, "summary": "private-content"})
    events = before_summary + drain()
    assert [e["kind"] for e in events] == ([expected] if expected else [])
    assert "private" not in json.dumps(events)
    if expected == "call_digest":
        assert before_summary == []


def test_failed_summary_is_not_digest(tmp_path):
    record = CallRecord("id", tmp_path, "inbound", None)
    record.log_event("answered")
    record.finish("completed")
    record.set_summary({"ok": False})
    assert drain() == []


def test_summary_before_finish_and_repeated_writes_notify_once(tmp_path):
    record = CallRecord("id", tmp_path, "inbound", None)
    record.log_event("answered")
    record.set_summary({"ok": True, "summary": "private-content"})
    assert drain() == []
    record.finish("completed")
    record.set_summary({"ok": True, "summary": "private-content"})
    record.finish("completed")
    assert [event["kind"] for event in drain()] == ["call_digest"]


def test_real_sms_callback_deduplicates_after_persistence(caplog):
    from fakes import FakeModem
    from test_call_service import FakeSmsEmailForwarder, make_hub, make_service

    modem = FakeModem()
    hub = make_hub()
    make_service(modem, hub=hub, sms_email_forwarder=FakeSmsEmailForwarder(hub))
    modem.trigger_sms("private-number", "private-content")
    modem.trigger_sms("private-number", "private-content")
    assert len(drain()) == 1
    assert hub.history()[-1]["type"] == "sms_in"
    assert "private-number" not in caplog.text
    assert "private-content" not in caplog.text


def test_enqueue_failure_does_not_escape_or_leak(monkeypatch, caplog):
    def fail(*_args):
        raise RuntimeError("private-content")

    monkeypatch.setattr(remote_alert_push.queue, "put", fail)
    remote_alert_push.publish("sms")
    assert "Alert push enqueue failed" in caplog.text
    assert "private-content" not in caplog.text


def test_bounded_expiring_queue_and_send_failure(caplog):
    clock = [0.0]
    queue = remote_alert_push.AlertQueue(clock=lambda: clock[0])
    for _ in range(150):
        queue.put("sms")
    sent = []
    queue.drain(sent.append)
    assert len(sent) == 128
    queue.put("sms")
    clock[0] = 301
    queue.drain(sent.append)
    assert len(sent) == 128
    queue.put("sms")

    def fail(_):
        raise RuntimeError("private-token")

    queue.drain(fail)
    assert "private-token" not in caplog.text
    assert "Alert push send failed" in caplog.text
