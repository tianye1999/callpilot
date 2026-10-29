"""app.py entrypoint helpers."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app
from agentcall.agents import local_agent
from agentcall.web import server


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.started = False

    def start(self):
        self.started = True
        self.callback()


@pytest.mark.parametrize("provider", ["local", "qwen", "openai", "doubao"])
def test_main_serves_meta_while_local_models_preload(monkeypatch, tmp_path, provider):
    entered, release = threading.Event(), threading.Event()
    threads, sites = [], []
    loop = asyncio.new_event_loop()
    original_forever = loop.run_forever
    original_preload = local_agent.start_model_preload
    site_type = app.web.TCPSite

    def blocked_load(**kwargs):
        entered.set()
        assert release.wait(2)

    def preload():
        threads.append(original_preload())
        return threads[-1]

    def site(*args, **kwargs):
        sites.append(site_type(*args, **kwargs))
        return sites[-1]

    async def check_health():
        port = sites[0]._server.sockets[0].getsockname()[1]
        async with ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/api/meta") as response:
                assert response.status == 200
                assert (await response.json())["provider"] == provider
        if provider == "local":
            assert entered.is_set() and threads[0].is_alive()
        else:
            assert not entered.is_set() and threads == []

    def run_forever():
        monkeypatch.setattr(loop, "run_forever", original_forever)
        loop.run_until_complete(check_health())
        raise KeyboardInterrupt

    monkeypatch.setattr(local_agent, "_get_shared_models", blocked_load)
    monkeypatch.setattr(local_agent, "start_model_preload", preload)
    monkeypatch.setattr(app.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(app.web, "TCPSite", site)
    monkeypatch.setattr(app, "_open_browser_later", lambda _: monkeypatch.setattr(loop, "run_forever", run_forever))
    monkeypatch.setattr(app, "_force_utf8", lambda: None)
    monkeypatch.setattr(app, "_enable_thread_dump", lambda: None)
    monkeypatch.setattr(app, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(app.config, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(app.config, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(app.logging, "FileHandler", lambda *a, **kw: app.logging.NullHandler())
    monkeypatch.setattr(app.number_profiles, "ensure_seeded", lambda: None)
    monkeypatch.setattr(app.call_playbooks, "ensure_seeded", lambda: None)
    monkeypatch.setattr(server, "detect_quectel_usb_online", lambda **kwargs: False)
    monkeypatch.setattr(app, "CallAgentService", lambda **kw: SimpleNamespace(
        modem=None, monitor=None, uplink_monitor=None, start=lambda: None, stop_service=lambda: None,
    ))
    for key, value in {
        "AGENT_PROVIDER": provider, "LOCAL_LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "fake-key",
        "QWEN_PREWARM": "false", "WEB_HOST": "127.0.0.1", "WEB_PORT": "0",
        "MODEM_AUDIO_MODE": "uac", "REMOTE_WEB_DIALER_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    try:
        app.main()
    finally:
        release.set()
        for thread in threads:
            thread.join(1)
            assert not thread.is_alive()
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)


def test_open_browser_later_skips_webbrowser_when_frozen(monkeypatch):
    opened = []
    timers = []
    monkeypatch.setattr(app.config, "_is_frozen", lambda: True)
    monkeypatch.setattr(app.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(
        app.threading,
        "Timer",
        lambda delay, callback: timers.append(FakeTimer(delay, callback)) or timers[-1],
    )

    assert app._open_browser_later("http://127.0.0.1:47100") is None
    assert opened == []
    assert timers == []


def test_open_browser_later_opens_browser_in_source_mode(monkeypatch):
    monkeypatch.delenv("WEB_OPEN_BROWSER", raising=False)
    opened = []
    timers = []
    monkeypatch.setattr(app.config, "_is_frozen", lambda: False)
    monkeypatch.setattr(app.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(
        app.threading,
        "Timer",
        lambda delay, callback: timers.append(FakeTimer(delay, callback)) or timers[-1],
    )

    timer = app._open_browser_later("http://127.0.0.1:47100", delay=0.25)

    assert timer is timers[0]
    assert timer.delay == 0.25
    assert timer.started is True
    assert opened == ["http://127.0.0.1:47100"]


def test_open_browser_later_skips_timer_when_disabled(monkeypatch):
    monkeypatch.setenv("WEB_OPEN_BROWSER", "false")
    monkeypatch.setattr(app.config, "_is_frozen", lambda: False)
    timers = []
    monkeypatch.setattr(app.threading, "Timer", lambda *args: timers.append(args))

    assert app._open_browser_later("http://127.0.0.1:47100") is None
    assert timers == []


def test_restart_after_cleanup_exits_for_launchd_managed_service(monkeypatch):
    calls = []
    monkeypatch.setattr(app.sys, "platform", "darwin")
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.agentcall.app")
    monkeypatch.setattr(app.os, "execv", lambda *args: calls.append(args))

    app._restart_after_cleanup()

    assert calls == []


def test_restart_after_cleanup_execs_manual_process_without_duplicate_argv0(monkeypatch):
    calls = []
    monkeypatch.setattr(app.sys, "platform", "darwin")
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.setattr(app.config, "_is_frozen", lambda: True)
    monkeypatch.setattr(app.sys, "executable", "/tmp/CallPilot")
    monkeypatch.setattr(app.sys, "argv", ["/tmp/CallPilot", "--service"])
    monkeypatch.setattr(app.os, "execv", lambda path, argv: calls.append((path, argv)))

    app._restart_after_cleanup()

    assert calls == [("/tmp/CallPilot", ["/tmp/CallPilot", "--service"])]


def test_restart_after_cleanup_preserves_script_for_source_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(app.sys, "platform", "linux")
    monkeypatch.setattr(app.config, "_is_frozen", lambda: False)
    monkeypatch.setattr(app.sys, "executable", "/tmp/python")
    monkeypatch.setattr(app.sys, "argv", ["/repo/app.py", "--service"])
    monkeypatch.setattr(app.os, "execv", lambda path, argv: calls.append((path, argv)))

    app._restart_after_cleanup()

    assert calls == [("/tmp/python", ["/tmp/python", "/repo/app.py", "--service"])]


def test_invalid_optional_cloud_url_does_not_crash_core_app(monkeypatch, caplog):
    monkeypatch.setattr(app.config, "get_str", lambda _key: "http://bad.example?secret=x")

    api, store, client = app._create_cloud_components(object())

    assert (api, store, client) == (None, None, None)
    assert "已禁用远程云功能" in caplog.text


def test_cloud_components_wire_content_repository_behind_edge_gate(monkeypatch):
    hub = object()
    call_logger = object()
    service = type("Service", (), {"call_logger": call_logger})()
    repository = object()
    captured = {}

    class FakeStore:
        @staticmethod
        def sign(_message):
            return "signature"

    class FakeClient:
        def __init__(self, base_url, actual_service, store, **kwargs):
            captured.update(
                base_url=base_url,
                service=actual_service,
                store=store,
                **kwargs,
            )
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(app, "CloudCredentialStore", FakeStore)
    monkeypatch.setattr(app, "CloudControlApi", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(app, "CloudEdgeClient", FakeClient)
    monkeypatch.setattr(
        app,
        "ContentSyncRepository",
        lambda actual_hub, actual_logger: (
            repository
            if (actual_hub, actual_logger) == (hub, call_logger)
            else None
        ),
    )
    monkeypatch.setattr(
        app.config,
        "get_str",
        lambda key: "https://api-beta.bondings.ai"
        if key == "REMOTE_CLOUD_URL"
        else "",
    )
    monkeypatch.setattr(
        app.config,
        "get_bool",
        lambda key: key == "REMOTE_CONTENT_READ_ENABLED",
    )

    _api, _store, client = app._create_cloud_components(service, hub)

    assert client.started is True
    assert captured["service"] is service
    assert captured["content_repository"] is repository
    assert captured["content_read_enabled"]() is True
