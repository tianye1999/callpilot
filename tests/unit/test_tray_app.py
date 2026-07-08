"""tray_app 纯逻辑单测（不导入 rumps、不开 GUI）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tray_app


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

    import urllib.error
    def boom(url, timeout=None):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(tray_app.urllib.request, "urlopen", boom)
    assert tray_app.probe_online("http://x") is False


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
