"""Release checks use fake HTTPS responses and temporary local settings only."""

import asyncio
import json

import pytest

from agentcall import app_updates as updates
from agentcall.notification_events import NotificationFeed


def release(**changes):
    return dict(product="physical-sim", platform="macos", version="26.11.1",
                publishedAt=1793232000000, notes={"zh": "更新说明", "en": "Release notes"},
                downloadUrl="https://downloads.example.test/app.dmg", **changes)


@pytest.mark.parametrize("new,old,expected", [
    ("26.10.10", "26.10.9", True), ("26.11.1", "26.10.99", True),
    ("26.10.1", "26.10.1.0", False), ("26.9.99", "26.10.1", False),
    ("v26.11.1", "26.10.1", False), ("26.11.1", "unknown", False),
    ("26..1", "26.10.1", False), ("２６.11.1", "26.10.1", False),
])
def test_versions_compare_numeric_segments(new, old, expected):
    assert updates.is_newer(new, old) is expected


class Response:
    def __init__(self, status=200, data=None, error=None):
        self.status, self.data, self.error = status, data, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.parametrize("response", [
    Response(404), Response(400), Response(500), Response(error=TimeoutError()),
    Response(error=OSError()), Response(data=[]), Response(data={}),
    Response(data={**release(), "downloadUrl": "http://example.test/app"}),
    Response(data={**release(), "notes": {"zh": 42, "en": "ok"}}),
    Response(data={**release(), "platform": "windows"}),
    Response(data={**release(), "publishedAt": True}),
])
def test_failed_or_invalid_checks_are_silent(response, caplog):
    assert asyncio.run(updates.fetch_release(Session(response), "https://example.test/latest")) is None
    assert not caplog.records


def test_https_timeout_and_no_redirects():
    session = Session(Response(data=release()))
    assert asyncio.run(updates.fetch_release(session, "http://example.test")) is None
    assert not session.calls
    assert asyncio.run(updates.fetch_release(session, "https://example.test")) == release()
    assert session.calls[0][1]["timeout"].total <= 5
    assert session.calls[0][1]["allow_redirects"] is False


def test_dismissal_and_notification_survive_restart(tmp_path):
    path = tmp_path / "updates.json"
    state = updates.UpdateChecker(path, version="26.10.1")
    state.latest = release()
    state.dismiss("26.11.1")
    assert state.snapshot()["dismissed"] is True
    feed = NotificationFeed()
    state.notify(feed, busy=True)
    assert not feed.read(0)["events"]
    state.notify(feed, busy=False)
    assert len(feed.read(0)["events"]) == 1
    assert not state.claim_notification("26.11.1", busy=True)
    assert state.claim_notification("26.11.1", busy=False)
    restored = updates.UpdateChecker(path, version="26.10.1")
    restored.latest = release()
    assert restored.snapshot()["dismissed"] is True
    restored.notify(feed, busy=False)
    assert len(feed.read(0)["events"]) == 1
    assert "26.11.1" in json.loads(path.read_text())["notified"]
    restored.latest = {**release(), "version": "26.12.1"}
    assert restored.snapshot()["dismissed"] is False


def test_check_at_start_and_every_24_hours(tmp_path):
    state = updates.UpdateChecker(tmp_path / "updates.json", version="26.10.1")
    session = Session(Response(data=release()))
    async def run():
        await state.check(session, "https://example.test", now=0)
        await state.check(session, "https://example.test", now=86399)
        assert len(session.calls) == 1
        await state.check(session, "https://example.test", now=86400)
    asyncio.run(run())
    assert len(session.calls) == 2
    assert state.snapshot()["version"] == "26.11.1"


def test_version_is_pyproject_snapshot_and_notes_are_bundled():
    from agentcall.app_version import RELEASE_NOTES, VERSION

    assert VERSION == "26.10.6"
    assert RELEASE_NOTES["zh"] and RELEASE_NOTES["en"]


def test_app_reopen_requests_check_without_duplicate_startup(tmp_path):
    state = updates.UpdateChecker(tmp_path / "updates.json", version="26.10.1")
    session = Session(Response(data=release()))
    async def run():
        await state.check(session, "https://example.test", now=0)
        state.request_check(now=1)
        await state.check(session, "https://example.test", now=1)
        assert len(session.calls) == 1
        state.request_check(now=60)
        await state.check(session, "https://example.test", now=60)
    asyncio.run(run())
    assert len(session.calls) == 2


def test_nonmacos_update_checks_are_disabled(monkeypatch):
    from agentcall import platforms
    from agentcall.web import server

    monkeypatch.setenv("UPDATE_CHECK_ENABLED", "true")
    monkeypatch.setattr(platforms, "IS_MACOS", False)
    assert not server._updates_enabled()
