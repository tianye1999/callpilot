"""窗口退出回归：全部使用假窗口和临时目录，不启动 Cocoa 或打包进程。"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import desktop_app


def _startup(monkeypatch, tmp_path, destroy, **kwargs):
    from agentcall import macos_shell

    monkeypatch.setattr(macos_shell, "ui_language", lambda: "zh")
    startup = desktop_app.BundledStartup(
        SimpleNamespace(destroy=destroy), "http://127.0.0.1:47100", SimpleNamespace(support_dir=tmp_path), **kwargs,
    )
    ticks = iter([False, True])
    monkeypatch.setattr(startup.closed, "wait", lambda timeout: next(ticks, True))
    (tmp_path / "close-window").touch()
    return startup


def test_consumed_close_request_logs_destroy_attempt(monkeypatch, tmp_path, caplog):
    destroyed = []
    startup = _startup(monkeypatch, tmp_path, lambda: destroyed.append(True))

    with caplog.at_level(logging.INFO, logger="desktop_app"):
        startup.run()

    assert destroyed == [True]
    assert not (tmp_path / "close-window").exists()
    messages = [record.getMessage() for record in caplog.records if record.name == "desktop_app"]
    assert any("关闭" in message for message in messages)
    assert any("销毁" in message or "destroy" in message for message in messages)


def test_destroy_exception_is_logged_instead_of_losing_close_request(monkeypatch, tmp_path, caplog):
    def destroy():
        raise RuntimeError("fake Cocoa close failure")

    startup = _startup(monkeypatch, tmp_path, destroy)
    with caplog.at_level(logging.INFO, logger="desktop_app"):
        startup.run()

    assert not (tmp_path / "close-window").exists()
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_bundled_window_entry_emits_normal_lifecycle_log(monkeypatch, caplog):
    from agentcall import macos_shell

    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(macos_shell, "configure_shell_logging", lambda process: calls.append(process))
    monkeypatch.setattr(desktop_app, "_bundled_macos_service_is_system_managed", lambda: True)
    monkeypatch.setattr(desktop_app, "_bundled_main", lambda *args, **kwargs: calls.append("run"))

    with caplog.at_level(logging.INFO, logger="desktop_app"):
        desktop_app.main()

    assert calls == ["window", "run"]
    assert any(record.name == "desktop_app" and "窗口" in record.getMessage() for record in caplog.records)


def test_exit_guard_is_armed_before_destroy(monkeypatch, tmp_path):
    calls = []
    startup = _startup(monkeypatch, tmp_path, lambda: calls.append("destroy"),
                       request_exit=lambda: calls.append("arm-exit"))
    startup.run()
    assert calls == ["arm-exit", "destroy"]


@pytest.mark.parametrize("close_kind", ["request", "red-button"])
def test_exit_guard_survives_closed_event_and_normal_exit_releases_lock_first(monkeypatch, tmp_path, close_kind):
    from agentcall import macos_shell

    calls, timers = [], []

    class Event:
        def __init__(self):
            self.callbacks = []

        def __iadd__(self, callback):
            self.callbacks.append(callback)
            return self

        def set(self):
            for callback in self.callbacks:
                callback()

    class Timer:
        def __init__(self, interval, function, args=(), kwargs=None):
            self.interval, self.function, self.args = interval, function, args
            self.kwargs = kwargs or {}
            self.daemon = False
            timers.append(self)

        def start(self):
            calls.append("arm-exit")

        def cancel(self):
            pytest.fail("closed 事件不是进程退出，不能取消退出保障")

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    closed = Event()
    window = SimpleNamespace(
        expose=lambda *args: None, get_current_url=lambda: None, events=SimpleNamespace(closed=closed),
        destroy=lambda: (calls.append("destroy"), closed.set()),
    )
    lock = SimpleNamespace(seek=lambda pos: None, truncate=lambda: None, write=lambda text: None,
                           flush=lambda: None, close=lambda: calls.append("unlock"))
    monkeypatch.setattr(macos_shell, "ui_language", lambda: "zh")
    monkeypatch.setattr(desktop_app, "_window_lock", lambda support: lock)
    monkeypatch.setattr(desktop_app, "_install_window_hooks", lambda *args: None)
    monkeypatch.setattr(desktop_app.threading, "Thread", Thread)
    monkeypatch.setattr(desktop_app.threading, "Timer", Timer)
    monkeypatch.setattr(desktop_app, "_destroy_bundled_window", lambda win: win.destroy())
    from agentcall import macos_launchd

    monkeypatch.setattr(macos_launchd, "make_layout", lambda executable: SimpleNamespace(support_dir=tmp_path))

    def runloop(start):
        if close_kind == "request":
            (tmp_path / "close-window").touch()
            start()
            assert calls == ["arm-exit", "destroy"]
        else:
            closed.set()
            assert calls == ["arm-exit"]
        closed.set()
        assert len(timers) == 1 and 0 < timers[0].interval <= 2.5 and timers[0].daemon
        # 即便 Cocoa 已发 closed，主循环仍未返回时也须保证进程退出。
        timer = timers[0]
        timer.function(*timer.args, **timer.kwargs)
        assert calls[-1] == ("exit", 0)

    webview = SimpleNamespace(create_window=lambda *args, **kwargs: window, start=runloop)
    desktop_app._bundled_main(webview, "http://127.0.0.1:47100", exit_process=lambda code: calls.append(("exit", code)))
    assert calls[-2:] == ["unlock", ("exit", 0)]


def _close_fixture(monkeypatch, tmp_path, *, release_on=None, change_at=None, replacement_pid=None,
                   original_identity="original", changed_identity="original"):
    clock = [0.0]
    released = [False]
    calls = []
    lock_path = tmp_path / "window.lock"
    lock_path.write_text("4242", encoding="utf-8")
    handle = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(desktop_app.time, "monotonic", lambda: clock[0])

    def sleep(delay):
        clock[0] += delay
        if change_at is not None and clock[0] >= change_at and replacement_pid is not None:
            lock_path.write_text(str(replacement_pid), encoding="utf-8")

    def identity(pid):
        if released[0]:
            return None
        if change_at is not None and clock[0] >= change_at:
            return changed_identity
        return original_identity

    def kill(pid, sig):
        calls.append((round(clock[0], 3), pid, sig))
        if sig == release_on:
            released[0] = True

    monkeypatch.setattr(desktop_app.time, "sleep", sleep)
    monkeypatch.setattr(desktop_app, "_window_lock", lambda support: handle if released[0] else None)
    monkeypatch.setattr(desktop_app, "_window_process_identity", identity)
    monkeypatch.setattr(desktop_app.os, "kill", kill)
    return calls, clock, released


def test_close_guard_terminates_verified_old_window_before_four_seconds(monkeypatch, tmp_path):
    calls, clock, _ = _close_fixture(monkeypatch, tmp_path, release_on=signal.SIGTERM)
    desktop_app.close_bundled_window(tmp_path)
    assert len(calls) == 1
    when, pid, sig = calls[0]
    assert 2.5 <= when <= 2.6 and pid == 4242 and sig == signal.SIGTERM
    assert clock[0] < 4


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="macOS/POSIX 退出兜底")
def test_close_guard_escalates_only_same_verified_window(monkeypatch, tmp_path):
    calls, clock, _ = _close_fixture(monkeypatch, tmp_path, release_on=signal.SIGKILL)
    desktop_app.close_bundled_window(tmp_path)
    assert [(pid, sig) for _, pid, sig in calls] == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert 3.3 <= calls[1][0] <= 3.4
    assert clock[0] < 4


@pytest.mark.parametrize("original,changed,new_pid", [
    (None, None, None),
    ("original", "reused-pid", None),
    ("original", None, None),
    ("original", "original", 9999),
])
def test_close_guard_refuses_unknown_or_replaced_process(monkeypatch, tmp_path, original, changed, new_pid):
    calls, clock, _ = _close_fixture(
        monkeypatch, tmp_path, change_at=2.0, replacement_pid=new_pid,
        original_identity=original, changed_identity=changed,
    )
    with pytest.raises(RuntimeError):
        desktop_app.close_bundled_window(tmp_path)
    assert calls == []
    assert clock[0] <= 4.1


def test_close_guard_does_not_signal_window_that_released_lock(monkeypatch, tmp_path):
    calls, _, released = _close_fixture(monkeypatch, tmp_path)
    released[0] = True
    desktop_app.close_bundled_window(tmp_path)
    assert calls == []


def test_close_guard_does_not_escalate_after_pid_identity_changes(monkeypatch, tmp_path):
    calls, _, _ = _close_fixture(monkeypatch, tmp_path, change_at=2.8, changed_identity="reused-pid")
    with pytest.raises(RuntimeError):
        desktop_app.close_bundled_window(tmp_path)
    assert [(pid, sig) for _, pid, sig in calls] == [(4242, signal.SIGTERM)]


@pytest.mark.parametrize("uid,command,accepted", [
    (502, "/Applications/CallPilot.app/Contents/MacOS/CallPilot --window", True),
    (503, "/Applications/CallPilot.app/Contents/MacOS/CallPilot --window", False),
    (502, "/Applications/CallPilot.app/Contents/MacOS/CallPilot --service", False),
    (502, "/Other/CallPilot.app/Contents/MacOS/CallPilot --window", False),
    (502, "/Applications/CallPilot.app/Contents/MacOS/CallPilot --window --extra", False),
])
def test_window_identity_requires_same_user_executable_and_window_role(monkeypatch, uid, command, accepted):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/Applications/CallPilot.app/Contents/MacOS/CallPilot")
    monkeypatch.setattr(desktop_app.os, "getuid", lambda: 502, raising=False)
    monkeypatch.setattr(desktop_app.os, "getpid", lambda: 999)
    results = SimpleNamespace(returncode=0, stdout=f"{uid} Fri Oct 30 12:34:56 2026 {command}\n")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return results

    monkeypatch.setattr(desktop_app.subprocess, "run", run)
    identity = desktop_app._window_process_identity(4242)
    assert (identity is not None) is accepted
    assert calls[0][1]["timeout"] <= 0.2
    if accepted:
        assert "12:34:56" in identity


def test_window_identity_query_failure_is_not_process_absence(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(desktop_app.os, "getpid", lambda: 999)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fake ps", 0.2)

    monkeypatch.setattr(desktop_app.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="身份"):
        desktop_app._window_process_identity(4242)


def test_unlocked_window_with_unqueryable_process_is_not_confirmed_exited(monkeypatch, tmp_path, caplog):
    calls, clock, _released = _close_fixture(monkeypatch, tmp_path)
    handle = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(desktop_app, "_window_lock", lambda support: None if clock[0] == 0 else handle)

    def identity(pid):
        if clock[0] == 0:
            return "original"
        raise RuntimeError("进程身份查询超时")

    monkeypatch.setattr(desktop_app, "_window_process_identity", identity)
    with pytest.raises(RuntimeError):
        desktop_app.close_bundled_window(tmp_path)
    assert calls == []
    assert "窗口退出已确认" not in caplog.text
    assert 3.7 <= clock[0] <= 4


def test_slow_identity_queries_share_the_four_second_close_budget(monkeypatch, tmp_path):
    calls, clock, _ = _close_fixture(monkeypatch, tmp_path)
    lock = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(desktop_app, "_window_lock", lambda support: None if clock[0] == 0 else lock)
    queried = []

    def identity(pid):
        queried.append(clock[0])
        if len(queried) == 1:
            return "original"
        clock[0] += 0.2
        raise RuntimeError("fake ps timeout")

    monkeypatch.setattr(desktop_app, "_window_process_identity", identity)
    with pytest.raises(RuntimeError):
        desktop_app.close_bundled_window(tmp_path)
    assert not calls
    assert clock[0] <= 4.0


def test_destroy_dispatches_red_button_close_on_main_thread(monkeypatch):
    queued, calls = [], []
    monkeypatch.setattr(sys, "platform", "darwin")
    native = SimpleNamespace(performClose_=lambda sender: calls.append("perform-close"))
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(
        AppHelper=SimpleNamespace(callAfter=lambda callback: queued.append(callback)),
    ))
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", SimpleNamespace(
        BrowserView=SimpleNamespace(instances={"window": SimpleNamespace(window=native)}),
    ))
    window = SimpleNamespace(uid="window", destroy=lambda: pytest.fail("不能回到等待 shown 的 destroy"))
    desktop_app._destroy_bundled_window(window)
    assert calls == [] and len(queued) == 1
    queued[0]()
    assert calls == ["perform-close"]


@pytest.mark.skipif(sys.platform != "darwin", reason="窗口退出与 POSIX 文件锁集成")
def test_stuck_fake_runloop_exits_child_and_releases_real_temp_lock(tmp_path):
    """只启动隔离 Python 子进程和假 GUI，验证 os._exit 真正释放内核锁。"""
    code = '''
import sys, threading
from pathlib import Path
from types import SimpleNamespace
import desktop_app
from agentcall import macos_launchd, macos_shell
support = Path(sys.argv[1])
macos_launchd.make_layout = lambda exe: SimpleNamespace(support_dir=support)
macos_shell.ui_language = lambda: "zh"
macos_shell.configure_shell_logging("window", support / "logs")
desktop_app._install_window_hooks = lambda *args: None
class Event:
    def __iadd__(self, callback):
        return self
window = SimpleNamespace(expose=lambda *args: None, get_current_url=lambda: None,
                         events=SimpleNamespace(closed=Event()), destroy=lambda: None)
def runloop(start):
    (support / "close-window").touch()
    start()
    threading.Event().wait(10)
webview = SimpleNamespace(create_window=lambda *args, **kwargs: window, start=runloop)
desktop_app._bundled_main(webview, "http://unused.invalid", desktop_app._exit_window_process)
raise AssertionError("假事件循环不应先于退出保障返回")
'''
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                            capture_output=True, text=True, timeout=6)
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 4
    lock = desktop_app._window_lock(tmp_path)
    assert lock is not None
    lock.close()
    log = (tmp_path / "logs/window.log").read_text(encoding="utf-8")
    assert "窗口进程退出" in log
    assert "窗口关闭请求已消费" in log
    assert "窗口 Cocoa 事件循环已返回" not in log
