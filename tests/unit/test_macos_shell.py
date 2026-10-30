"""外壳纯逻辑：不启动 Cocoa、launchd 或真实服务。"""

import json
import subprocess
import sys

import pytest

from agentcall import macos_launchd, macos_shell


@pytest.mark.parametrize("languages,expected", [
    (["zh-Hans-CN", "en"], "zh"), (["zh-TW"], "zh"), (["en", "zh-Hant"], "zh"),
    (["en-US"], "en"), (["ja-JP"], "en"), ([], "en"),
])
def test_ui_language_uses_preferred_languages_not_agent_language(monkeypatch, languages, expected):
    monkeypatch.setenv("AGENT_LANGUAGE", "zh" if expected == "en" else "en")
    assert macos_shell.ui_language(languages) == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="需要 macOS 原生 run loop，不创建托盘或窗口")
def test_common_mode_timer_fires_during_menu_tracking():
    result = subprocess.run([sys.executable, "-c", '''
import time
import rumps
from AppKit import NSApplication, NSMenu, NSEventTrackingRunLoopMode
from Foundation import NSDate, NSRunLoop
from agentcall.macos_shell import install_menu_tracking
NSApplication.sharedApplication()
ticks, opened, closed = [], [], []
timer = rumps.Timer(lambda sender: ticks.append(time.monotonic()), 0.01)
timer.start()
loop = NSRunLoop.mainRunLoop()
def track(seconds, until=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and (until is None or len(ticks) < until):
        loop.runMode_beforeDate_(NSEventTrackingRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.01))
try:
    track(0.05)
    assert not ticks, ticks
    menu = NSMenu.alloc().initWithTitle_("Offline test")
    delegate = install_menu_tracking(menu, [timer], lambda: opened.append(True), lambda: closed.append(True))
    delegate.menuWillOpen_(menu)
    track(1, until=2)
    delegate.menuDidClose_(menu)
    assert opened == closed == [True]
    assert len(ticks) >= 2, ticks
finally:
    timer.stop()
'''], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_failure_and_quit_messages_are_bilingual():
    assert "port" in macos_shell.failure_reason("Address already in use", lang="en")
    assert "通话" in macos_shell.quit_message({"call_active": True}, lang="zh")
    assert "call is in progress" in macos_shell.quit_message({"call_active": True}, lang="en")
    assert "no longer make or receive calls" in macos_shell.quit_message({}, lang="en")


@pytest.mark.skipif(sys.platform != "darwin", reason="需要真实 macOS 分布式通知")
def test_command_notification_wakes_observer_without_poll_timer(tmp_path):
    result = subprocess.run([sys.executable, "-c", '''
import subprocess, sys, time
from pathlib import Path
from Foundation import NSDate, NSRunLoop, NSDistributedNotificationCenter
from agentcall import macos_shell
support = Path(sys.argv[1])
received = []
observer = macos_shell.observe_commands(lambda: received.append(macos_shell.take_commands(support)), support)
sender = subprocess.Popen([sys.executable, "-c", """
import sys, time
from pathlib import Path
from agentcall.macos_shell import send_command
sys.frozen = True
send_command('open', Path(sys.argv[1]))
""", str(support)])
deadline = time.monotonic() + 5
while not received and time.monotonic() < deadline:
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.01))
assert sender.wait(timeout=5) == 0
NSDistributedNotificationCenter.defaultCenter().removeObserver_(observer)
assert received == [{'open'}], received
''', str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_diagnostics_reads_service_and_bridge_file_logs(tmp_path):
    layout = macos_launchd.make_layout(tmp_path / "CallPilot", uid=123, support_dir=tmp_path)
    layout.log_dir.mkdir()
    (layout.log_dir / "app.log").write_text("Address already in use\n", encoding="utf-8")
    (layout.log_dir / "modem_usb_pty.log").write_text("USBError: disconnected\n", encoding="utf-8")
    reason, report = macos_shell.collect_diagnostics(
        layout, {}, runner=lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", ""), lang="en",
    )
    assert "port" in reason
    logs = json.loads(report)["recent_log_tail_sanitized"]
    assert logs["app.log"] and logs["modem_usb_pty.log"]


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_launchd_failure_page_hides_details_but_preserves_diagnostics(tmp_path, lang):
    error = 'SMAppService Error Domain=SMAppServiceErrorDomain Code=1 "Operation not permitted"\nDetails'
    result = macos_launchd.LaunchdInstallResult([], [macos_launchd.LaunchctlFailure(
        macos_launchd.APP_LABEL, "unregister", (), 1, stderr=error,
    )], [])
    reason = macos_shell.launchd_failure_reason(result, lang)
    macos_shell.write_startup_failure(tmp_path, reason, diagnostics=result.failure_summary())
    assert macos_shell.read_startup_failure(tmp_path) == reason
    assert ("复制诊断信息" if lang == "zh" else "Copy diagnostics") in reason
    assert "launchctl" not in reason and "Error Domain" not in reason
    assert macos_launchd.APP_LABEL not in reason
    details = macos_shell.read_startup_failure(tmp_path, field="diagnostics")
    assert error in details
    assert macos_launchd.APP_LABEL in details and "unregister" in details and "exit 1" in details
    macos_shell.write_startup_failure(tmp_path, "")
    assert macos_shell.read_startup_failure(tmp_path) == ""
    assert macos_shell.read_startup_failure(tmp_path, field="diagnostics") == ""


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_launchd_approval_failure_page_uses_localized_settings_guidance(lang):
    from agentcall import macos_service_management as sm

    result = macos_launchd.LaunchdInstallResult([], [macos_launchd.LaunchctlFailure(
        macos_launchd.APP_LABEL, "approval-required", (), 1, stderr=str(sm.ApprovalRequired()),
    )], [])
    reason = macos_shell.launchd_failure_reason(result, lang)
    assert "caffeinate" in reason
    assert ("登录项与扩展" if lang == "zh" else "Login Items & Extensions") in reason
    assert ("重试" if lang == "zh" else "retry") in reason
    assert ("Login Items" if lang == "zh" else "系统设置") not in reason
    assert "launchctl" not in reason and "Error Domain" not in reason


@pytest.mark.parametrize("lang,expected", [
    ("zh", "正在完成旧版本迁移，约 1 分钟后会自动启动，无需操作。"),
    ("en", "Finishing migration from the previous version. CallPilot will start automatically in about "
           "1 minute. No action is needed."),
])
def test_legacy_migration_reason_does_not_ask_for_settings_or_retry(lang, expected):
    result = macos_launchd.LaunchdInstallResult([], [macos_launchd.LaunchctlFailure(
        macos_launchd.APP_LABEL, "legacy-migration-pending", (), 1,
    )], [])
    assert macos_shell.launchd_failure_reason(result, lang) == expected


def test_shell_logging_rotates_and_captures_uncaught_exceptions(tmp_path):
    result = subprocess.run([sys.executable, "-c", '''
import logging, sys, threading, traceback
from pathlib import Path
from agentcall.macos_shell import configure_shell_logging
directory = Path(sys.argv[1])
configure_shell_logging("window", directory, max_bytes=2048, backup_count=2)
configure_shell_logging("window", directory, max_bytes=2048, backup_count=2)
for _ in range(100):
    logging.warning("rotation test " + "x" * 100)
logging.warning("unique marker")
try:
    raise RuntimeError("callback exception marker")
except RuntimeError:
    traceback.print_exc()
def fail():
    raise RuntimeError("worker exception marker")
thread = threading.Thread(target=fail)
thread.start()
thread.join()
raise RuntimeError("main exception marker")
''', str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    files = list(tmp_path.glob("window.log*"))
    assert 1 < len(files) <= 3
    assert all(path.stat().st_size <= 2048 for path in files)
    content = "".join(path.read_text(encoding="utf-8") for path in files)
    assert content.count("unique marker") == 1
    assert "RuntimeError: worker exception marker" in content
    assert "RuntimeError: main exception marker" in content
    assert "RuntimeError: callback exception marker" in content
    assert "Traceback" in content


def test_shell_stderr_handler_errors_do_not_recurse(tmp_path):
    result = subprocess.run([sys.executable, "-c", '''
import logging, sys
from pathlib import Path
from agentcall.macos_shell import configure_shell_logging
configure_shell_logging("tray", Path(sys.argv[1]))
handler = logging.getLogger().handlers[-1]
handler.emit = lambda record: sys.stderr.write("handler error\\n")
sys.stderr.write("outer error\\n")
''', str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("handler error") == 1


@pytest.mark.parametrize("login,setup,expected", [
    (False, False, True), (False, True, True), (True, False, False), (True, True, True),
])
def test_should_open_window(login, setup, expected):
    assert macos_shell.should_open_window(login, setup) is expected


def test_reopen_and_second_instance_coalesce(tmp_path):
    macos_shell.send_command("open", tmp_path)
    macos_shell.send_command("open", tmp_path)
    assert macos_shell.take_commands(tmp_path) == {"open"}
    assert macos_shell.take_commands(tmp_path) == set()


def test_quit_cancel_and_order(tmp_path):
    layout = macos_launchd.make_layout(tmp_path / "CallPilot", uid=123, support_dir=tmp_path)
    calls = []
    loaded = {macos_launchd.APP_LABEL, macos_launchd.BRIDGE_LABEL}

    def runner(cmd, **kwargs):
        label = cmd[-1].split("/")[-1]
        if cmd[1] == "bootout":
            calls.append(label)
            loaded.discard(label)
        return subprocess.CompletedProcess(
            cmd, int(label not in loaded) if cmd[1] == "print" else 0,
            "", f'Could not find service "{label}"' if label not in loaded else "",
        )

    for confirmed in (False, True):
        result = macos_shell.quit_services(
            confirmed, layout, lambda: calls.append("window"), lambda: calls.append("tray"), runner=runner,
        )
        assert result is confirmed
        assert calls == ([macos_launchd.APP_LABEL, macos_launchd.BRIDGE_LABEL, "window", "tray"] if confirmed else [])


def test_autostart_changes_login_files_not_running_jobs(tmp_path):
    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot", uid=123, support_dir=tmp_path / "support", launch_dir=tmp_path / "agents",
    )
    assert macos_launchd.autostart_enabled(layout)
    macos_launchd.set_autostart(layout, False)
    assert not macos_launchd.autostart_enabled(layout)
    assert not list(layout.launch_agents_dir.glob("*.plist"))
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[1] == "print" else 0, "", "")

    result = macos_launchd.install_launch_agents(layout, runner=runner)
    assert not result.failures
    assert len([c for c in calls if c[1] == "bootstrap"]) == 3
    assert not list(layout.launch_agents_dir.glob("*.plist"))
    macos_launchd.set_autostart(layout, True)
    assert len(list(layout.launch_agents_dir.glob("*.plist"))) == 3
    for plist in macos_launchd.build_plists(layout).values():
        assert plist["AssociatedBundleIdentifiers"] == ["ai.bondings.callpilot"]
    assert macos_launchd.build_plists(layout)[macos_launchd.TRAY_LABEL]["ProgramArguments"][-1] == "--login"


def test_geometry_roundtrip_and_missing_monitor(tmp_path):
    screens = [(0, 0, 1440, 900)]
    path = tmp_path / "window.json"
    default = macos_shell.restore_geometry(path, screens)
    assert default[2:] == (1100, 780)
    macos_shell.save_geometry(path, (2000, -800, 1800, 1200))
    assert macos_shell.restore_geometry(path, screens) == (0, 0, 1440, 900)
    macos_shell.save_geometry(path, (30, 40, 900, 700))
    assert macos_shell.restore_geometry(path, screens) == (30, 40, 900, 700)
    path.write_text(json.dumps([None, "bad", 1, 2]), encoding="utf-8")
    assert macos_shell.restore_geometry(path, screens) == default


def test_diagnostics_never_copy_raw_secrets():
    report = macos_shell.diagnostic_report(
        "26.10.1", "15.0", {"app": "state = running\nAPI_KEY=secret-one"},
        {"app": "Authorization: Bearer secret-two\nAddress already in use\nsecret-three"},
        {"hardware": {"usb_online": True, "api_key": "secret-four"}, "setup_sms_token": "secret-five"},
    )
    assert "secret-" not in report
    assert "端口" in report
    assert "usb_online" in report


def test_quit_failure_keeps_ui(tmp_path):
    layout = macos_launchd.make_layout(tmp_path / "CallPilot", uid=123, support_dir=tmp_path)
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1 if cmd[1] == "bootout" else 0)
    with pytest.raises(RuntimeError, match="停止"):
        macos_shell.quit_services(True, layout, lambda: pytest.fail("关窗"), lambda: pytest.fail("退出"), runner)


def test_quit_print_permission_error_is_not_absent(tmp_path):
    layout = macos_launchd.make_layout(tmp_path / "CallPilot", uid=123, support_dir=tmp_path)

    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "permission denied")

    with pytest.raises(RuntimeError, match="无法确认"):
        macos_shell.quit_services(True, layout, lambda: pytest.fail("关窗"), lambda: pytest.fail("退出"), runner)


def test_quit_message_warns_active_and_unknown_calls():
    assert "正在通话中" in macos_shell.quit_message({"call_active": True})
    assert "无法确认" in macos_shell.quit_message({})
    assert macos_shell.quit_message({"call_active": False}) == "退出后将不再接打电话。"


def test_failure_reasons_are_plain_language():
    assert "端口" in macos_shell.failure_reason("OSError: Address already in use")
    assert "错误退出" in macos_shell.failure_reason("Traceback (most recent call last)")
    assert "USB" in macos_shell.failure_reason("USBError: disconnected")


def test_fetch_meta_rejects_another_http_service(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b'{"ok":true}'

    monkeypatch.setattr(macos_shell.urllib.request, "urlopen", lambda *a, **kw: Response())
    assert macos_shell.fetch_meta("http://localhost") == {}


def test_collect_diagnostics_is_bounded_and_sanitized(tmp_path):
    import plistlib

    executable = tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot"
    executable.parent.mkdir(parents=True)
    (executable.parent.parent / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "26.10.1"}))
    layout = macos_launchd.make_layout(executable, uid=123, support_dir=tmp_path / "support")
    layout.log_dir.mkdir(parents=True)
    (layout.log_dir / "launchd-app.err.log").write_text(
        "secret\n" * 10000 + "Address already in use", encoding="utf-8",
    )
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(cmd, 0, "state = running\nAPI_KEY=secret", "")

    reason, report = macos_shell.collect_diagnostics(layout, {"key": "secret"}, runner)
    assert len(calls) == 3
    assert "端口" in reason
    assert "secret" not in report
    assert json.loads(report)["app_version"] == "26.10.1"
    assert len(json.loads(report)["recent_log_tail_sanitized"]["launchd-app.err.log"]) == 30
