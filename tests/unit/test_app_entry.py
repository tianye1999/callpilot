"""app.py entrypoint helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.started = False

    def start(self):
        self.started = True
        self.callback()


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
