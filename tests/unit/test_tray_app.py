"""tray_app 纯逻辑单测（不导入 rumps、不开 GUI）。"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tray_app


class MetaResponse:
    """模拟 /api/meta 响应（支持 with 语法和 JSON body）。"""

    status = 200

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_web_url_default_and_override(monkeypatch):
    monkeypatch.delenv("AGENTCALL_WEB_URL", raising=False)
    assert tray_app.web_url() == "http://127.0.0.1:47100"
    monkeypatch.setenv("AGENTCALL_WEB_URL", "http://127.0.0.1:9000/")
    assert tray_app.web_url() == "http://127.0.0.1:9000"  # 尾斜杠去掉


def test_icon_path_files_exist():
    on = tray_app.icon_path(True)
    off = tray_app.icon_path(False)
    assert on.endswith("menubar_on.png")
    assert off.endswith("menubar_off.png")
    # 源码运行时图标资源必须真实存在（打包时随 _MEIPASS 内嵌）
    assert Path(on).is_file() and Path(off).is_file()


def test_status_and_menu_labels_bilingual():
    assert tray_app.status_label(True, "zh") == "服务：运行中"
    assert tray_app.status_label(False, "zh") == "服务：已停止"
    assert tray_app.status_label(True, "en") == "Service: running"
    assert tray_app.menu_label("open", "en") == "Open dashboard"
    assert tray_app.menu_label("restart", "zh") == "重启服务"
    assert tray_app.menu_label("uninstall", "zh") == "卸载常驻"
    assert tray_app.menu_label("quit", "en") == "Quit"
    # 未知语言回退中文
    assert tray_app.status_label(True, "fr") == "服务：运行中"


def test_dashboard_command_points_to_desktop_app():
    cmd = tray_app.dashboard_command("/fake/python")
    assert cmd[0] == "/fake/python"
    assert cmd[1].endswith("desktop_app.py")


def test_dashboard_command_uses_frozen_executable(monkeypatch):
    monkeypatch.setattr(tray_app.sys, "executable", "/Applications/CallPilot.app/Contents/MacOS/CallPilot")
    assert tray_app.dashboard_command(frozen=True) == [
        "/Applications/CallPilot.app/Contents/MacOS/CallPilot",
        "--window",
    ]


def test_probe_online_true_false(monkeypatch):
    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(tray_app.urllib.request, "urlopen", lambda url, timeout=None: Resp())
    assert tray_app.probe_online("http://x") is True

    def boom(url, timeout=None):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(tray_app.urllib.request, "urlopen", boom)
    assert tray_app.probe_online("http://x") is False


def test_fetch_setup_required_true_false(monkeypatch):
    seen = []
    responses = iter([MetaResponse(b'{"setup_required": true}'), MetaResponse(b'{"setup_required": false}')])

    def fake_urlopen(url, timeout=None):
        seen.append({"url": url, "timeout": timeout})
        return next(responses)

    monkeypatch.setattr(tray_app.urllib.request, "urlopen", fake_urlopen)

    assert tray_app.fetch_setup_required("http://x", timeout=1.25) is True
    assert tray_app.fetch_setup_required("http://x/", timeout=1.25) is False
    assert seen == [
        {"url": "http://x/api/meta", "timeout": 1.25},
        {"url": "http://x/api/meta", "timeout": 1.25},
    ]


def test_fetch_setup_required_returns_none_on_bad_or_missing_meta(monkeypatch):
    responses = iter([
        MetaResponse(b'{"setup_complete": false}'),
        MetaResponse(b"not-json"),
    ])

    monkeypatch.setattr(tray_app.urllib.request, "urlopen", lambda url, timeout=None: next(responses))
    assert tray_app.fetch_setup_required("http://x") is None
    assert tray_app.fetch_setup_required("http://x") is None

    def boom(url, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(tray_app.urllib.request, "urlopen", boom)
    assert tray_app.fetch_setup_required("http://x") is None


def test_maybe_autoopen_dashboard_opens_when_setup_required():
    popen_calls = []

    def fake_popen(argv, cwd=None):
        popen_calls.append({"argv": argv, "cwd": cwd})

    result = tray_app.maybe_autoopen_dashboard(
        "http://x",
        wait=5.0,
        poll_interval=0.5,
        probe=lambda url: True,
        fetch_setup_required_func=lambda url: True,
        popen=fake_popen,
        dashboard_cmd_factory=lambda: ["python", "desktop_app.py"],
        sleep=lambda seconds: None,
    )

    assert result is True
    assert popen_calls == [{"argv": ["python", "desktop_app.py"], "cwd": str(tray_app.PROJECT_ROOT)}]


def test_maybe_autoopen_dashboard_skips_when_already_configured():
    popen_calls = []

    result = tray_app.maybe_autoopen_dashboard(
        "http://x",
        wait=5.0,
        poll_interval=0.5,
        probe=lambda url: True,
        fetch_setup_required_func=lambda url: False,
        popen=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        sleep=lambda seconds: None,
    )

    assert result is False
    assert popen_calls == []


def test_maybe_autoopen_dashboard_times_out_without_real_sleep():
    probe_calls = []
    sleep_calls = []
    now = {"value": 0.0}

    def fake_probe(url):
        probe_calls.append((url, now["value"]))
        return False

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        now["value"] += seconds

    result = tray_app.maybe_autoopen_dashboard(
        "http://x",
        wait=1.0,
        poll_interval=0.25,
        probe=fake_probe,
        fetch_setup_required_func=lambda url: True,
        popen=lambda *args, **kwargs: None,
        sleep=fake_sleep,
        monotonic=lambda: now["value"],
    )

    assert result is False
    assert len(probe_calls) == 5
    assert sleep_calls == [0.25, 0.25, 0.25, 0.25]


def test_maybe_autoopen_dashboard_skips_when_setup_probe_unknown():
    popen_calls = []

    result = tray_app.maybe_autoopen_dashboard(
        "http://x",
        wait=5.0,
        poll_interval=0.5,
        probe=lambda url: True,
        fetch_setup_required_func=lambda url: None,
        popen=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        sleep=lambda seconds: None,
    )

    assert result is False
    assert popen_calls == []


def test_maybe_autoopen_dashboard_logs_popen_failure(caplog):
    def fake_popen(argv, cwd=None):
        raise OSError("no window")

    result = tray_app.maybe_autoopen_dashboard(
        "http://x",
        wait=5.0,
        poll_interval=0.5,
        probe=lambda url: True,
        fetch_setup_required_func=lambda url: True,
        popen=fake_popen,
        dashboard_cmd_factory=lambda: ["python", "desktop_app.py"],
        sleep=lambda seconds: None,
    )

    assert result is False
    assert "自动打开控制台失败" in caplog.text


def test_request_restart_posts(monkeypatch):
    seen = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return Resp()

    monkeypatch.setattr(tray_app.urllib.request, "urlopen", fake_urlopen)
    assert tray_app.request_restart("http://127.0.0.1:47100") is True
    assert seen["url"] == "http://127.0.0.1:47100/api/restart"
    assert seen["method"] == "POST"
