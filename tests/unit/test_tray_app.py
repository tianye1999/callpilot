"""tray_app 纯逻辑单测（不导入 rumps、不开 GUI）。"""

from __future__ import annotations

import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tray_app


@pytest.fixture(autouse=True)
def _forbid_unmocked_dashboard_spawn(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("托盘单测必须显式注入窗口启动桩，不允许派生真实桌面或服务进程")
    monkeypatch.setitem(tray_app.open_or_focus_dashboard.__kwdefaults__, "popen", forbidden)


def test_early_window_spawn_failure_does_not_prevent_tray_startup(monkeypatch, caplog):
    monkeypatch.setattr(tray_app.sys, "argv", ["CallPilot"])
    monkeypatch.setattr(tray_app, "_bundled_macos", lambda: True)
    monkeypatch.setattr(tray_app, "_acquire_tray_singleton_lock", object)

    def fail_open(proc):
        raise OSError("test spawn failure")

    def reached_tray():
        raise RuntimeError("reached tray initialization")

    monkeypatch.setattr(tray_app, "open_or_focus_dashboard", fail_open)
    monkeypatch.setattr(tray_app, "_import_rumps", reached_tray)
    with pytest.raises(RuntimeError, match="reached tray initialization"):
        tray_app.main()
    assert "test spawn failure" in caplog.text


@pytest.mark.parametrize("entry", [
    "service",
    pytest.param("bridge", marks=pytest.mark.skipif(sys.platform == "win32", reason="PTY 桥仅支持 POSIX")),
])
def test_bundled_entry_logging_replaces_existing_handler_and_writes_file(tmp_path, entry):
    result = subprocess.run([sys.executable, "-c", '''
import logging, logging.handlers, sys
from pathlib import Path
logging.basicConfig(level=logging.INFO)
directory = Path(sys.argv[2])
if sys.argv[1] == "service":
    import app
    app.load_dotenv = lambda *args: None
    app._force_utf8 = lambda: None
    app.config.log_dir = lambda: directory
    def stop(*args):
        raise RuntimeError("stop before service initialization")
    app.config.get_str = stop
    run = app.main
    path = directory / "app.log"
else:
    sys.path.insert(0, str(Path.cwd() / "scripts"))
    import ec20_usb_pty
    def stop():
        raise RuntimeError("stop before USB or instance lock")
    ec20_usb_pty.acquire_instance_lock = stop
    path = directory / "modem_usb_pty.log"
    sys.argv = ["bridge", "--log-file", str(path)]
    run = ec20_usb_pty.main
sys.frozen = True
try:
    run()
except RuntimeError:
    pass
logging.getLogger("test").warning("file logging marker")
handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
assert len(handlers) == 1
assert handlers[0].maxBytes == 5 * 1024 * 1024 and handlers[0].backupCount == 3
assert handlers[0].encoding == "utf-8"
assert "file logging marker" in path.read_text(encoding="utf-8")
''', entry, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="需要真实 macOS Cocoa/PyObjC")
def test_real_rumps_reopen_hook_installs_with_bool_signature():
    result = subprocess.run([sys.executable, "-c", '''
import objc
from rumps.rumps import NSApp
import tray_app
calls = []
tray_app._install_reopen_handler(lambda: calls.append("open"))
method = NSApp.applicationShouldHandleReopen_hasVisibleWindows_
assert method.signature == b"Z@:@Z", method.signature
delegate = NSApp.alloc().init()
assert method(delegate, None, False) is False
assert method(delegate, None, True) is False
assert calls == ["open", "open"]
'''], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


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
    assert tray_app.menu_label("quit", "en") == "Quit CallPilot"
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


class _FakeProc:
    """模拟 subprocess.Popen：poll() 按 alive 返回 None(存活)/0(已退出)。"""

    def __init__(self, pid: int, alive: bool):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


def test_open_or_focus_opens_when_no_window():
    """无窗口进程时新开一个。"""
    calls: list = []
    new_proc = _FakeProc(111, alive=True)
    result = tray_app.open_or_focus_dashboard(
        None,
        popen=lambda cmd, cwd: calls.append(("popen", cmd)) or new_proc,
        bring_to_front=lambda pid: calls.append(("front", pid)),
        cmd_factory=lambda: ["cmd"],
    )
    assert result is new_proc
    assert calls == [("popen", ["cmd"])]  # 只 Popen，未激活


def test_open_or_focus_reuses_live_window():
    """已有存活窗口时拉到前台并复用，绝不再开新窗口。"""
    calls: list = []
    live = _FakeProc(222, alive=True)
    result = tray_app.open_or_focus_dashboard(
        live,
        popen=lambda cmd, cwd: calls.append(("popen", cmd)) or _FakeProc(999, True),
        bring_to_front=lambda pid: calls.append(("front", pid)),
        cmd_factory=lambda: ["cmd"],
    )
    assert result is live  # 复用旧句柄
    assert calls == [("front", 222)]  # 只激活，未 Popen


def test_open_or_focus_reopens_after_window_closed():
    """上一个窗口已关闭（poll 非 None）时重新开一个。"""
    calls: list = []
    dead = _FakeProc(333, alive=False)
    fresh = _FakeProc(444, alive=True)
    result = tray_app.open_or_focus_dashboard(
        dead,
        popen=lambda cmd, cwd: calls.append(("popen", cmd)) or fresh,
        bring_to_front=lambda pid: calls.append(("front", pid)),
        cmd_factory=lambda: ["cmd"],
    )
    assert result is fresh
    assert calls == [("popen", ["cmd"])]


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


@pytest.mark.parametrize("user_initiated", [False, True])
def test_background_install_publishes_failure_and_clears_on_retry(monkeypatch, tmp_path, user_initiated):
    from agentcall import macos_launchd, macos_shell

    monkeypatch.setattr(macos_shell, "ui_language", lambda: "zh")

    calls = []
    failure = macos_launchd.LaunchctlFailure(
        macos_launchd.APP_LABEL, "bootstrap", (), 5, stderr="Bootstrap failed: 5: Input/output error\nextra",
    )
    results = iter([
        macos_launchd.LaunchdInstallResult([], [failure], []), macos_launchd.LaunchdInstallResult([], [], []),
    ])

    def install(**kwargs):
        calls.append(kwargs)
        return next(results)

    monkeypatch.setattr(tray_app, "_bundled_macos", lambda: True)
    monkeypatch.setattr(macos_launchd, "app_support_dir", lambda: tmp_path)
    monkeypatch.setattr(tray_app, "_ensure_launchd_installed", install)
    tray_app._start_background_services(user_initiated=user_initiated)
    reason = macos_shell.read_startup_failure(tmp_path)
    assert "后台服务无法启动" in reason and "退出码 5" in reason
    assert "Input/output error" in reason and "extra" not in reason
    tray_app._start_background_services(user_initiated=True)
    assert macos_shell.read_startup_failure(tmp_path) == ""
    assert calls == [{"user_initiated": user_initiated}, {"user_initiated": True}]


@pytest.mark.parametrize("user_initiated", [False, True])
def test_ensure_launchd_forwards_user_launch_flag(monkeypatch, tmp_path, user_initiated):
    from agentcall import macos_launchd

    calls = []
    monkeypatch.setattr(tray_app, "_is_frozen", lambda: True)
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    monkeypatch.setattr(macos_launchd, "make_layout", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(macos_launchd, "install_launch_agents", lambda *args, **kwargs: calls.append(kwargs))
    tray_app._ensure_launchd_installed(user_initiated=user_initiated)
    assert calls == [{"no_restart_labels": {macos_launchd.TRAY_LABEL}, "user_initiated": user_initiated}]


@pytest.mark.parametrize("login", [False, True])
def test_bundled_health_probe_throttles_ticks_from_start(monkeypatch, tmp_path, login):
    import types

    from agentcall import macos_launchd, macos_shell

    now = [0.0]
    instances = []
    threads = []
    probes = []
    ticks = []
    installs = []
    startup_order = []
    reopen_callbacks = []

    class FakeMenuItem:
        def __init__(self, title, **kwargs):
            self.titles = [title]

        @property
        def title(self):
            return self.titles[-1]

        @title.setter
        def title(self, value):
            self.titles.append(value)

    class FakeApp:
        def __init__(self, *args, **kwargs):
            instances.append(self)
            self.icon_updates = []

        @property
        def icon(self):
            return self.icon_updates[-1]

        @icon.setter
        def icon(self, value):
            self.icon_updates.append(value)

        def run(self):
            pass

    class FakeThread:
        def __init__(self, *, target, daemon):
            assert daemon
            self.target = target
            self.alive = False
            threads.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def finish(self):
            self.target()
            self.alive = False

    monkeypatch.setattr(tray_app, "_bundled_macos", lambda: True)
    monkeypatch.setattr(tray_app, "_acquire_tray_singleton_lock", object)
    monkeypatch.setattr(tray_app, "open_or_focus_dashboard",
                        lambda proc: startup_order.append("window") or _FakeProc(123, True))
    monkeypatch.setattr(macos_shell, "observe_commands", lambda callback: None)
    monkeypatch.setattr(macos_shell, "ui_language", lambda: "zh")
    monkeypatch.setattr(tray_app, "_install_reopen_handler", reopen_callbacks.append)
    monkeypatch.setattr(tray_app.sys, "argv", ["CallPilot"] + (["--login"] if login else []))
    monkeypatch.setattr(tray_app, "_start_background_services", lambda **kwargs: installs.append(kwargs))
    monkeypatch.setattr(tray_app.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(tray_app.threading, "Thread", FakeThread)
    monkeypatch.setattr(tray_app, "probe_online", lambda url: probes.append(now[0]) or True)
    monkeypatch.setattr(
        macos_launchd, "make_layout",
        lambda executable: types.SimpleNamespace(env_file=tmp_path / ".env", support_dir=tmp_path),
    )
    monkeypatch.setattr(macos_shell, "should_open_window", lambda *args: False)
    monkeypatch.setattr(macos_shell, "take_commands", lambda: ticks.append(now[0]) or set())
    monkeypatch.setattr(tray_app, "_import_rumps", lambda: startup_order.append("rumps") or types.SimpleNamespace(
        App=FakeApp, MenuItem=FakeMenuItem,
        timer=lambda interval: lambda fn: fn,
    ))
    tray_app.main()
    assert startup_order == (["rumps"] if login else ["window", "rumps"])
    app = instances[0]
    threads[0].finish()
    assert installs == [{"user_initiated": not login}]
    threads.clear()  # 安装目标已打桩；后面只统计健康探测线程。

    app._requests(None)
    assert len(threads) == 1
    assert probes == []  # 主线程只启动工作线程，不执行 HTTP 探测。
    threads[-1].finish()
    for now[0] in (0.2, 0.4, 1.0, 2.8, 2.999):
        app._requests(None)
        app._refresh(None)  # 旧的五秒定时入口也不能绕过节流。
    assert probes == [0.0]
    assert len(threads) == 1
    assert len(ticks) == 6
    assert app.icon == tray_app.icon_path(True)
    assert app.icon_updates == [tray_app.icon_path(True)]
    assert app._status_item.titles == [tray_app.status_label(False), tray_app.status_label(True)]

    now[0] = 3.0
    app._requests(None)
    assert len(threads) == 2
    now[0] = 6.0
    app._requests(None)
    assert len(threads) == 2  # 慢探测仍在运行时不重叠启动。
    threads[-1].finish()
    now[0] = 6.2
    app._requests(None)
    assert len(threads) == 3  # 间隔从启动而非结束时间计算。
    app._online = False
    app._requests(None)
    app._requests(None)
    assert app.icon_updates == [tray_app.icon_path(True), tray_app.icon_path(False)]
    assert app._status_item.titles == [
        tray_app.status_label(False), tray_app.status_label(True), tray_app.status_label(False),
    ]

    app._start_services()
    pending_install = threads[-1]
    app._start_services()  # 安装仍在运行时，主动请求不能丢失。
    assert threads[-1] is pending_install
    pending_install.finish()
    app._requests(None)
    assert threads[-1] is not pending_install
    threads[-1].finish()
    assert installs[-2:] == [{"user_initiated": True}, {"user_initiated": True}]
    actions = []
    monkeypatch.setattr(app, "_open", lambda sender: actions.append("window"))
    monkeypatch.setattr(app, "_start_services", lambda: actions.append("services"))
    reopen_callbacks[0]()  # Cocoa reopen 直接处理，不等下一次 tick。
    assert actions == ["window", "services"]


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


def test_tray_singleton_lock_is_exclusive_and_releasable(tmp_path):
    pytest.importorskip("fcntl", reason="单例锁依赖 POSIX flock（tray 形态仅 macOS）")
    lock_path = tmp_path / "tray.lock"

    first = tray_app._acquire_tray_singleton_lock(lock_path)
    assert first is not None
    # 同一路径的第二个实例拿不到锁（让位信号）。
    assert tray_app._acquire_tray_singleton_lock(lock_path) is None

    first.close()  # 释放后可再获取（模拟旧实例退出）
    second = tray_app._acquire_tray_singleton_lock(lock_path)
    assert second is not None
    second.close()


def test_main_yields_quietly_when_singleton_lock_held(monkeypatch):
    monkeypatch.setattr(tray_app.sys, "argv", ["tray_app.py"])
    monkeypatch.setattr(
        tray_app, "_acquire_tray_singleton_lock", lambda lock_path=None: None
    )

    def _fail_import():
        raise AssertionError("让位路径不应再走到 rumps 初始化")

    monkeypatch.setattr(tray_app, "_import_rumps", _fail_import)

    tray_app.main()  # 已有实例在跑：应静默返回（进程退出码 0 语义）


@pytest.mark.parametrize("login,expected", [(False, ["open"]), (True, [])])
def test_bundled_second_instance_forwards_user_open_only(monkeypatch, login, expected):
    from agentcall import macos_shell

    calls = []
    monkeypatch.setattr(tray_app, "_bundled_macos", lambda: True)
    monkeypatch.setattr(tray_app, "_acquire_tray_singleton_lock", lambda: None)
    monkeypatch.setattr(tray_app.sys, "argv", ["CallPilot"] + (["--login"] if login else []))
    monkeypatch.setattr(macos_shell, "send_command", calls.append)
    monkeypatch.setattr(tray_app, "_import_rumps", lambda: pytest.fail("第二实例不得启动 GUI"))
    tray_app.main()
    assert calls == expected


def test_reopen_callback_forwards_to_same_open_action(monkeypatch):
    import types

    methods = []
    calls = []
    monkeypatch.setitem(sys.modules, "objc", types.SimpleNamespace(
        selector=lambda fn, **kw: fn, classAddMethods=lambda cls, items: methods.extend(items),
    ))
    monkeypatch.setitem(sys.modules, "rumps.rumps", types.SimpleNamespace(NSApp=object))
    tray_app._install_reopen_handler(lambda: calls.append("open"))
    assert methods[0](None, None, False) is False
    assert methods[0](None, None, True) is False
    assert calls == ["open", "open"]


def test_foreground_uses_running_application_not_automation(monkeypatch):
    import types

    calls = []
    application = types.SimpleNamespace(activateWithOptions_=lambda options: calls.append(options))
    running = types.SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid: application)
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(
        NSRunningApplication=running, NSApplicationActivateAllWindows=1, NSApplicationActivateIgnoringOtherApps=2,
    ))
    tray_app._bring_window_to_front(123)
    assert calls == [3]
