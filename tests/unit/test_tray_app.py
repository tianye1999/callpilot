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
    from agentcall.macos_shell import configure_shell_logging
    configure_shell_logging("service", directory)
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
if sys.argv[1] == "service":
    import traceback
    logging.info("info logging marker")
    try:
        raise RuntimeError("logged exception marker")
    except RuntimeError:
        logging.exception("caught exception")
    shell_log = (directory / "service.log").read_text(encoding="utf-8")
    app_log = path.read_text(encoding="utf-8")
    for marker in ("file logging marker", "info logging marker", "RuntimeError: logged exception marker"):
        assert app_log.count(marker) == 1
        assert marker not in shell_log
    assert "[INFO] root: info logging marker" in app_log
    try:
        raise RuntimeError("direct traceback marker")
    except RuntimeError:
        traceback.print_exc()
    def fail():
        raise RuntimeError("uncaught thread marker")
    import threading
    thread = threading.Thread(target=fail)
    thread.start()
    thread.join()
    try:
        raise RuntimeError("uncaught main marker")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    shell_log = (directory / "service.log").read_text(encoding="utf-8")
    for marker in ("direct traceback marker", "uncaught thread marker", "uncaught main marker"):
        assert marker in shell_log
    assert "Traceback (most recent call last)" in shell_log
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
    assert "后台服务无法启动" in reason and "复制诊断信息" in reason
    assert "Input/output error" not in reason and "launchctl" not in reason
    details = macos_shell.read_startup_failure(tmp_path, field="diagnostics")
    assert failure.message == details
    tray_app._start_background_services(user_initiated=True)
    assert macos_shell.read_startup_failure(tmp_path) == ""
    assert macos_shell.read_startup_failure(tmp_path, field="diagnostics") == ""
    assert calls == [{"user_initiated": user_initiated}, {"user_initiated": True}]


@pytest.mark.parametrize("user_initiated", [False, True])
def test_ensure_launchd_forwards_user_launch_flag(monkeypatch, tmp_path, user_initiated):
    from agentcall import macos_launchd, macos_upgrade

    calls = []
    monkeypatch.setattr(tray_app, "_is_frozen", lambda: True)
    monkeypatch.setattr(tray_app.sys, "platform", "darwin")
    monkeypatch.setattr(macos_launchd, "make_layout", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(macos_upgrade, "reconcile", lambda *args, **kwargs: calls.append(kwargs))
    checks = []
    monkeypatch.setattr(macos_upgrade, "_lease_request", lambda *args: checks.append(args) or {})
    tray_app._ensure_launchd_installed(user_initiated=user_initiated)
    assert calls[0]["user_initiated"] is user_initiated
    assert callable(calls[0]["acquire"])
    assert checks == ([(tray_app.web_url(), "check", {})] if user_initiated else [])


@pytest.fixture
def migration_install(monkeypatch, tmp_path):
    import plistlib
    import subprocess
    import types

    from agentcall import macos_launchd as ld
    from agentcall import macos_service_management as sm
    from agentcall import macos_shell

    layout = ld.make_layout(tmp_path / "CallPilot", uid=123, support_dir=tmp_path / "support",
                            launch_dir=tmp_path / "agents")
    layout.launch_agents_dir.mkdir()
    (layout.launch_agents_dir / f"{ld.APP_LABEL}.plist").write_bytes(plistlib.dumps({"Label": ld.APP_LABEL}))
    now, calls, states, waits, reasons = [1000.0], [], {label: 2 for label in ld.LAUNCH_LABELS}, [], []

    class Service:
        def __init__(self, label):
            self.label = label

        def status(self):
            return states[self.label]

        def registerAndReturnError_(self, error):
            states[self.label] = 1
            return True, None

    native = object.__new__(sm.NativeServices)
    monkeypatch.setattr(native, "_service", Service)

    def runner(cmd, **kwargs):
        label = cmd[-1].split("/")[-1]
        return subprocess.CompletedProcess(cmd, 113 if cmd[1] == "print" else 0, "",
                                           f'Could not find service "{label}"' if cmd[1] == "print" else "")

    def install(**kwargs):
        calls.append(now[0])
        return sm.install(layout, runner, services=native, **kwargs,
                          sleep=lambda _: pytest.fail("Single install must not sleep for BTM"))

    def sleep(delay):
        waits.append(delay)
        reasons.append(macos_shell.read_startup_failure(layout.support_dir))
        now[0] += delay

    monkeypatch.setattr(tray_app, "_bundled_macos", lambda: True)
    monkeypatch.setattr(ld, "app_support_dir", lambda: layout.support_dir)
    monkeypatch.setattr(macos_shell, "ui_language", lambda: "zh")
    monkeypatch.setattr(sm.time, "time", lambda: now[0])
    monkeypatch.setattr(tray_app, "_ensure_launchd_installed", install)
    return types.SimpleNamespace(layout=layout, now=now, calls=calls, states=states, waits=waits,
                                 sleep=sleep, reasons=reasons)


@pytest.mark.parametrize("status", [0, 3])
def test_background_migration_retries_until_success_and_clears_failure(migration_install, status):
    from agentcall import macos_shell

    env = migration_install

    def sleep(delay):
        env.sleep(delay)
        if env.now[0] >= 1070:
            env.states.update({label: status for label in env.states})

    tray_app._retry_background_services(user_initiated=True, sleep=sleep)
    assert env.waits == [10] * 7
    assert env.calls == list(range(1000, 1071, 10))
    assert all(reason == "正在完成旧版本迁移，约 1 分钟后会自动启动，无需操作。" for reason in env.reasons)
    assert macos_shell.read_startup_failure(env.layout.support_dir) == ""
    assert macos_shell.read_startup_failure(env.layout.support_dir, field="diagnostics") == ""
    assert not (env.layout.support_dir / "legacy-cleanup.json").exists()


@pytest.mark.parametrize("reopen_age", [0, 70, 175])
def test_background_migration_timeout_uses_original_cleanup_time(migration_install, reopen_age):
    from agentcall import macos_shell

    env = migration_install
    tray_app._start_background_services()
    env.now[0] += reopen_age
    tray_app._retry_background_services(sleep=env.sleep)
    assert sum(env.waits) == 180 - reopen_age
    assert all(0 < delay <= 10 for delay in env.waits)
    assert all("无需操作" in reason for reason in env.reasons)
    assert "登录项与扩展" in macos_shell.read_startup_failure(env.layout.support_dir)
    assert not (env.layout.support_dir / "legacy-cleanup.json").exists()


def test_background_without_cleanup_immediately_shows_approval(migration_install):
    from agentcall import macos_launchd, macos_shell

    env = migration_install
    (env.layout.launch_agents_dir / f"{macos_launchd.APP_LABEL}.plist").unlink()
    tray_app._retry_background_services(sleep=lambda _: pytest.fail("Real approval must not wait"))
    assert env.calls == [1000]
    assert "登录项与扩展" in macos_shell.read_startup_failure(env.layout.support_dir)


def test_migration_wait_can_be_cancelled_without_losing_reopen_marker(monkeypatch, migration_install):
    import threading

    from agentcall import macos_shell

    env = migration_install
    cancelled = threading.Event()

    def wait(delay):
        assert delay == 10
        cancelled.set()
        return True

    monkeypatch.setattr(cancelled, "wait", wait)
    tray_app._retry_background_services(cancel=cancelled,
                                        sleep=lambda _: pytest.fail("Must use cancellable wait"))
    assert env.calls == [1000]
    assert (env.layout.support_dir / "legacy-cleanup.json").exists()
    assert macos_shell.read_startup_failure(env.layout.support_dir, field="action") == "legacy-migration-pending"


def test_migration_retries_have_monotonic_limit_if_wall_clock_stalls(migration_install):
    from agentcall import macos_shell

    env = migration_install
    elapsed = [0]

    def sleep(delay):
        elapsed[0] += delay

    tray_app._retry_background_services(sleep=sleep, monotonic=lambda: elapsed[0])
    assert elapsed[0] == 180
    assert "登录项与扩展" in macos_shell.read_startup_failure(env.layout.support_dir)


def test_second_instance_migration_does_not_run_retry_loop(monkeypatch, migration_install):
    env = migration_install
    monkeypatch.setattr(tray_app, "_acquire_tray_singleton_lock", lambda: None)
    monkeypatch.setattr(tray_app, "_handoff_previous_tray", lambda: None)
    monkeypatch.setattr(tray_app.sys, "argv", ["CallPilot"])
    monkeypatch.setattr(tray_app, "_import_rumps", lambda: pytest.fail("Second instance must yield"))
    monkeypatch.setattr(tray_app.time, "sleep", lambda _: pytest.fail("Second instance must not wait"))
    tray_app.main()
    assert env.calls == [1000]
    assert (env.layout.support_dir / "legacy-cleanup.json").exists()


@pytest.fixture(params=[False, True], ids=["foreground", "login"])
def bundled_tray(monkeypatch, tmp_path, request):
    import types

    from agentcall import macos_launchd, macos_shell

    login = request.param
    now = [0.0]
    instances = []
    threads = []
    probes = []
    ticks = []
    installs = []
    startup_order = []
    reopen_callbacks = []
    before_start = []
    tracking = []

    class FakeMenuItem:
        def __init__(self, title, **kwargs):
            self.titles = [title]
            self.callback = kwargs.get("callback")

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
    monkeypatch.setattr(tray_app, "_install_tracking_hooks", lambda app, rumps: tracking.append(app))
    monkeypatch.setattr(macos_shell, "ui_language", lambda: "zh")
    monkeypatch.setattr(tray_app, "_install_reopen_handler", reopen_callbacks.append)
    monkeypatch.setattr(tray_app.sys, "argv", ["CallPilot"] + (["--login"] if login else []))
    monkeypatch.setattr(tray_app, "_start_background_services", lambda **kwargs: installs.append(kwargs))
    monkeypatch.setattr(tray_app.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(tray_app.threading, "Thread", FakeThread)
    meta = {"hardware": {"usb_online": True, "modem_connected": True},
            "sim": {"present": True, "network_attached": True}, "credentials": {"ok": True}}
    monkeypatch.setattr(macos_shell, "fetch_meta", lambda url, **kw: probes.append(now[0]) or meta.copy())
    monkeypatch.setattr(tray_app, "update_status_icon", lambda app, state: app.icon_updates.append(state))
    monkeypatch.setattr(
        macos_launchd, "make_layout",
        lambda executable: types.SimpleNamespace(env_file=tmp_path / ".env", support_dir=tmp_path),
    )
    monkeypatch.setattr(macos_shell, "should_open_window", lambda *args: False)
    monkeypatch.setattr(macos_shell, "take_commands", lambda: ticks.append(now[0]) or set())
    monkeypatch.setattr(tray_app, "_import_rumps", lambda: startup_order.append("rumps") or types.SimpleNamespace(
        App=FakeApp, MenuItem=FakeMenuItem,
        timer=lambda interval: lambda fn: fn,
        events=types.SimpleNamespace(before_start=types.SimpleNamespace(register=before_start.append)),
    ))
    tray_app.main()
    assert startup_order == (["rumps"] if login else ["window", "rumps"])
    app = instances[0]
    threads[0].finish()
    assert installs == [{"user_initiated": not login}]
    threads.clear()  # 安装目标已打桩；后面只统计健康探测线程。
    assert len(before_start) == 1
    before_start[0]()
    assert tracking == [app]
    return types.SimpleNamespace(app=app, threads=threads, probes=probes, ticks=ticks,
                                 now=now, installs=installs, reopen_callbacks=reopen_callbacks, meta=meta)


def test_bundled_health_probe_throttles_ticks_from_start(monkeypatch, bundled_tray):
    env = bundled_tray
    app, threads, probes, ticks, now = env.app, env.threads, env.probes, env.ticks, env.now
    installs, reopen_callbacks, meta = env.installs, env.reopen_callbacks, env.meta

    app._requests(None)
    assert len(threads) == 1
    assert probes == []  # 主线程只启动工作线程，不执行 HTTP 探测。
    threads[-1].finish()
    for now[0] in (0.2, 0.4, 0.6, 0.8, 0.999):
        app._requests(None)
        app._refresh(None)  # 旧的五秒定时入口也不能绕过节流。
    assert probes == [0.0]
    assert len(threads) == 1
    assert len(ticks) == 6
    assert app.icon_updates == ["transient", "ready"]
    assert app._status_item.titles == [tray_app.status_label(False), "正在启动…", "可以接打电话"]
    assert [item.title for item in app.menu][2:] == ["打开 CallPilot", "退出 CallPilot"]
    assert app.menu[0].callback is None and app.menu[1].callback is None

    now[0] = 1.0
    app._requests(None)
    assert len(threads) == 2
    now[0] = 2.0
    app._requests(None)
    assert len(threads) == 2  # 慢探测仍在运行时不重叠启动。
    threads[-1].finish()
    now[0] = 2.2
    app._requests(None)
    assert len(threads) == 3  # 间隔从启动而非结束时间计算。
    app._meta = {}
    app._requests(None)
    app._requests(None)
    assert app.icon_updates == ["transient", "ready", "transient"]
    assert app._status_item.titles == [
        tray_app.status_label(False), "正在启动…", "可以接打电话", "正在启动…",
    ]

    app._meta = {**meta, "call_active": True, "call": {"number_suffix": "1234", "started_at": 100}}
    monkeypatch.setattr(tray_app.time, "time", lambda: 183)
    app._render_status()
    assert [item.title for item in app.menu][2:] == ["挂断", "旁听", "打开 CallPilot", "退出 CallPilot"]
    assert app._status_item.title == "通话中 · 尾号 1234 · 01:23"
    monkeypatch.setattr(tray_app.time, "time", lambda: 184)
    before = len(app.icon_updates)
    app._render_status()
    assert app._status_item.title.endswith("01:24")
    assert len(app.icon_updates) == before
    hangups = []
    monkeypatch.setattr(tray_app, "request_hangup", lambda url: hangups.append(url) or True)
    app.menu[2].callback(None)
    assert not hangups
    hangup_thread = threads[-1]
    app.menu[2].callback(None)
    assert threads[-1] is hangup_thread
    hangup_thread.finish()
    assert hangups == [tray_app.web_url()]
    listens = []
    monkeypatch.setattr(tray_app, "open_listen_dashboard", lambda proc: listens.append(proc) or proc)
    app.menu[3].callback(None)
    assert listens == [app._window_proc]
    app._meta = meta.copy()
    app._render_status()
    assert "挂断" not in [item.title for item in app.menu]
    assert app.menu[2] is app._action_items["open_app"]

    app._meta["hardware"] = {"usb_online": True, "modem_connected": False}
    now[0] = 100
    app._render_status()
    now[0] = 160
    app._render_status()
    assert app._displayed_state == "transient"
    now[0] = 160.001
    app._render_status()
    assert app._displayed_state == "attention"
    app._meta["hardware"]["usb_online"] = False
    app._render_status()
    app._meta["hardware"]["usb_online"] = True
    app._render_status()
    assert app._displayed_state == "transient"

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


def test_menu_open_rebuilds_cached_call_and_forces_async_probe(bundled_tray):
    env = bundled_tray
    app = env.app
    app._meta = {**env.meta, "call_active": True}
    app._render_status()
    app.menu.clear()
    app._last_probe_started = env.now[0]
    app._menu_will_open()
    assert [item.title for item in app.menu][2:4] == ["挂断", "旁听"]
    assert len(env.threads) == 1 and env.probes == []
    env.threads[-1].finish()
    # 探测已返回空闲，菜单保持展开，由 common-modes tick 消费最新缓存。
    app._requests(None)
    assert app._menu_open
    assert app._displayed_state == "ready"
    assert "挂断" not in [item.title for item in app.menu]
    app._menu_did_close()
    assert not app._menu_open


def test_call_start_and_open_menu_probe_cadence(bundled_tray, monkeypatch):
    env = bundled_tray
    app = env.app
    app._requests(None)
    env.threads[-1].finish()
    env.now[0] = 0.2
    app._requests(None)
    env.meta.update(call_active=True, call={"number_suffix": "1234", "started_at": 100})
    monkeypatch.setattr(tray_app.time, "time", lambda: 101)
    env.now[0] = 0.999
    app._requests(None)
    assert len(env.threads) == 1
    env.now[0] = 1.0
    app._requests(None)
    env.threads[-1].finish()
    env.now[0] = 1.2
    app._menu_will_open()
    assert app._displayed_state == "in_call"
    assert "挂断" in [item.title for item in app.menu]
    assert len(env.threads) == 3  # 开菜单立即探测，不等一秒节流。
    app._menu_will_open()
    assert len(env.threads) == 3  # 重复打开不重叠探测。
    env.threads[-1].finish()
    env.now[0] = 1.699
    app._requests(None)
    assert len(env.threads) == 3
    env.now[0] = 1.7
    app._requests(None)
    assert len(env.threads) == 4
    monkeypatch.setattr(tray_app.time, "time", lambda: 102)
    app._requests(None)
    assert app._status_item.title.endswith("00:02")
    env.threads[-1].finish()
    app._menu_did_close()
    app._meta = {}
    app._render_status()
    env.now[0] = 4.699
    app._requests(None)
    assert len(env.threads) == 4
    env.now[0] = 4.7
    app._requests(None)
    assert len(env.threads) == 5  # 服务不可达时恢复三秒节流。


def test_tracking_hooks_only_register_owned_timers_in_common_modes(monkeypatch):
    import types

    from agentcall import macos_shell

    opened, closed, registered, delegates = [], [], [], []
    def requests(sender):
        pass
    def refresh(sender):
        pass
    class Delegate:
        @classmethod
        def alloc(cls):
            return cls()
        def init(self):
            return self
    monkeypatch.setattr(macos_shell, "_tray_menu_delegate_class", lambda: Delegate)
    monkeypatch.setitem(sys.modules, "Foundation", types.SimpleNamespace(
        NSRunLoop=types.SimpleNamespace(mainRunLoop=lambda: types.SimpleNamespace(
            addTimer_forMode_=lambda timer, mode: registered.append((timer, mode)))),
        NSRunLoopCommonModes="common",
    ))
    app = types.SimpleNamespace(
        menu=types.SimpleNamespace(_menu=types.SimpleNamespace(setDelegate_=delegates.append)),
        _requests=requests, _refresh=refresh,
        _menu_will_open=lambda: opened.append(True), _menu_did_close=lambda: closed.append(True),
    )
    timers = [types.SimpleNamespace(callback=callback, _nstimer=index)
              for index, callback in enumerate([requests, refresh, lambda sender: None])]
    tray_app._install_tracking_hooks(app, types.SimpleNamespace(timers=lambda: timers))
    assert registered == [(0, "common"), (1, "common")]
    assert delegates == [app._menu_delegate]
    app._menu_delegate.on_open()
    app._menu_delegate.on_close()
    assert opened == closed == [True]


def test_open_listen_reuses_window_and_queues_anchor(monkeypatch, tmp_path):
    proc = _FakeProc(123, True)
    calls = []
    monkeypatch.setattr(tray_app, "open_or_focus_dashboard", lambda current: calls.append(current) or proc)
    assert tray_app.open_listen_dashboard(proc, tmp_path) is proc
    assert calls == [proc]
    assert (tmp_path / "listen-window").exists()
    assert (tmp_path / "focus-window").exists()


def test_open_notification_reuses_window_and_queues_validated_target(monkeypatch, tmp_path):
    from agentcall.macos_shell import take_notification_target

    proc = _FakeProc(123, True)
    monkeypatch.setattr(tray_app, "open_or_focus_dashboard", lambda current: current)
    assert tray_app.open_notification_dashboard(proc, "#confirm/abc", tmp_path) is proc
    assert (tmp_path / "focus-window").exists()
    assert take_notification_target(tmp_path) == "#confirm/abc"
    assert take_notification_target(tmp_path) is None
    with pytest.raises(ValueError):
        tray_app.open_notification_dashboard(proc, "https://evil.example", tmp_path)


@pytest.mark.parametrize("payload,success", [(b'{"ok":true}', True), (b'{"ok":false}', False),
                                            (b'bad', False)])
def test_request_hangup_posts_existing_endpoint(monkeypatch, payload, success):
    import io

    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        response = io.BytesIO(payload)
        response.status = 200
        return response

    monkeypatch.setattr(tray_app.urllib.request, "urlopen", urlopen)
    assert tray_app.request_hangup("http://test/") is success
    assert requests[0].full_url == "http://test/api/call/hangup"
    assert requests[0].method == "POST" and requests[0].data == b"{}"


def test_request_hangup_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("offline")
    monkeypatch.setattr(tray_app.urllib.request, "urlopen", fail)
    assert not tray_app.request_hangup("http://test")


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
    installs = []
    monkeypatch.setattr(tray_app, "_start_background_services", lambda **kw: installs.append(kw))
    monkeypatch.setattr(tray_app, "_bundled_macos", lambda: True)
    monkeypatch.setattr(tray_app, "_acquire_tray_singleton_lock", lambda: None)
    monkeypatch.setattr(tray_app.sys, "argv", ["CallPilot"] + (["--login"] if login else []))
    monkeypatch.setattr(macos_shell, "send_command", calls.append)
    monkeypatch.setattr(tray_app, "_handoff_previous_tray", lambda: (calls.append("open"), None)[1])
    monkeypatch.setattr(tray_app, "_import_rumps", lambda: pytest.fail("第二实例不得启动 GUI"))
    tray_app.main()
    assert calls == expected
    assert installs == ([] if login else [{"user_initiated": True}])


@pytest.mark.skipif(sys.platform == "win32", reason="PTY 桥仅支持 POSIX（需 fcntl）")
def test_bridge_only_records_version_after_acquiring_instance_lock(monkeypatch, tmp_path):
    from agentcall import macos_launchd, macos_upgrade
    from scripts import ec20_usb_pty

    monkeypatch.setattr(tray_app, "_bundled_macos", lambda: True)
    monkeypatch.setattr(tray_app, "_prepend_runtime_paths", lambda: None)
    monkeypatch.setattr(macos_launchd, "app_support_dir", lambda: tmp_path)
    recorded = []
    monkeypatch.setattr(macos_upgrade, "record_bridge_version", lambda *args: recorded.append(True))
    def busy():
        raise RuntimeError("Another bridge holds the lock")
    monkeypatch.setattr(ec20_usb_pty, "acquire_instance_lock", busy)
    monkeypatch.setattr(ec20_usb_pty, "main", lambda: ec20_usb_pty.acquire_instance_lock())
    with pytest.raises(RuntimeError):
        tray_app._run_bridge(["--auto"])
    assert not recorded
    assert ec20_usb_pty.acquire_instance_lock is busy


def test_previous_tray_handoff_releases_only_ui_singleton(monkeypatch, tmp_path):
    import json

    import desktop_app
    from agentcall import macos_launchd, macos_shell

    (tmp_path / "tray-version.json").write_text(json.dumps({"version": "26.9.1", "handoff": True}))
    monkeypatch.setattr(macos_launchd, "app_support_dir", lambda: tmp_path)
    commands, lock = [], object()
    monkeypatch.setattr(macos_shell, "send_command", commands.append)
    monkeypatch.setattr(desktop_app, "close_bundled_window", lambda support: commands.append("window-closed"))
    waits = []
    locks = iter([None] * 100 + [lock])
    monkeypatch.setattr(tray_app, "_acquire_tray_singleton_lock", lambda: next(locks))
    monkeypatch.setattr(tray_app.time, "sleep", waits.append)
    assert tray_app._handoff_previous_tray() is lock
    assert commands == ["replace", "window-closed"]
    assert len(waits) == 100  # Ten seconds must not abandon the successor.


def test_same_generation_reopen_does_not_replace_tray(monkeypatch, tmp_path):
    import json

    from agentcall import macos_launchd, macos_shell
    from agentcall.app_version import VERSION

    (tmp_path / "tray-version.json").write_text(json.dumps(
        {"version": VERSION, "executable": sys.executable, "lifecycle": "smappservice-v1"}))
    monkeypatch.setattr(macos_launchd, "app_support_dir", lambda: tmp_path)
    monkeypatch.setattr(macos_launchd, "make_layout", lambda _: macos_launchd.LaunchdLayout(
        tmp_path / "CallPilot", tmp_path, tmp_path, tmp_path / "agents", 123))
    commands = []
    monkeypatch.setattr(macos_shell, "send_command", commands.append)
    assert tray_app._handoff_previous_tray() is None
    assert commands == ["open"]


def test_same_version_wp6_tray_still_hands_off(monkeypatch, tmp_path):
    import json

    import desktop_app
    from agentcall import macos_launchd, macos_shell
    from agentcall.app_version import VERSION

    (tmp_path / "tray-version.json").write_text(json.dumps(
        {"version": VERSION, "executable": sys.executable, "handoff": True}))
    monkeypatch.setattr(macos_launchd, "app_support_dir", lambda: tmp_path)
    commands, lock = [], object()
    monkeypatch.setattr(macos_shell, "send_command", commands.append)
    monkeypatch.setattr(desktop_app, "close_bundled_window", lambda support: commands.append("window-closed"))
    monkeypatch.setattr(tray_app, "_acquire_tray_singleton_lock", lambda: lock)
    assert tray_app._handoff_previous_tray() is lock
    assert commands == ["replace", "window-closed"]


def test_uninstall_timeout_resume_wins_over_unconsumed_pause(monkeypatch, bundled_tray):
    from agentcall import macos_shell

    monkeypatch.setattr(macos_shell, "take_commands", lambda: {"pause", "resume"})
    bundled_tray.app._requests(None)
    assert bundled_tray.app._uninstall_pending is False


def test_non_crash_exit_recovery_uses_guarded_reconciler(bundled_tray):
    env = bundled_tray
    env.now[0] = 31
    env.app._requests(None)
    assert env.app._install_thread is not None
    env.app._install_thread.finish()
    assert env.installs[-1] == {"user_initiated": False}


def test_reopen_retry_and_timer_share_one_install_worker(monkeypatch, bundled_tray):
    from agentcall import macos_shell

    env = bundled_tray
    workers = []
    monkeypatch.setattr(tray_app, "_retry_background_services", lambda **kwargs: workers.append(kwargs))
    env.app._start_services()
    worker = env.app._install_thread
    env.app._reopen()
    env.now[0] = 31
    monkeypatch.setattr(macos_shell, "take_commands", lambda: {"retry"})
    env.app._requests(None)
    assert env.app._install_thread is worker
    assert workers == []  # Only the already scheduled thread may enter the retry loop.
    worker.finish()
    assert len(workers) == 1
    assert workers[0]["cancel"] is env.app._install_cancel


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
