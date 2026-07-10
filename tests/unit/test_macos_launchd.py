"""macOS bundled-app launchd plist generation tests."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from agentcall import macos_launchd


def test_bundle_root_and_resources_resolve_from_app_executable():
    exe = Path("/Applications/CallPilot.app/Contents/MacOS/CallPilot")
    expected_root = exe.resolve().parents[2]

    assert macos_launchd.bundle_root_for_executable(exe) == expected_root
    assert macos_launchd.resources_dir_for_executable(exe) == Path(
        expected_root / "Contents" / "Resources"
    )


def test_make_layout_without_uid_fails_clearly_when_getuid_is_unavailable(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(macos_launchd.os, "getuid", None, raising=False)

    with pytest.raises(RuntimeError, match="macOS"):
        macos_launchd.make_layout(tmp_path / "CallPilot")


def test_build_plists_use_current_bundle_and_application_support_paths(tmp_path):
    exe = tmp_path / "Moved.app" / "Contents" / "MacOS" / "CallPilot"
    resources = tmp_path / "Moved.app" / "Contents" / "Resources"
    support = tmp_path / "Library" / "Application Support" / "CallPilot"
    layout = macos_launchd.make_layout(
        exe, uid=501, resources_dir=resources, support_dir=support, launch_dir=tmp_path
    )

    plists = macos_launchd.build_plists(layout)
    app = plists["com.agentcall.app"]
    bridge = plists["com.agentcall.bridge"]

    assert app["ProgramArguments"] == ["/usr/bin/caffeinate", "-s", str(exe.resolve()), "--service"]
    assert bridge["ProgramArguments"][:4] == [
        "/usr/bin/caffeinate",
        "-s",
        str(exe.resolve()),
        "--bridge",
    ]
    assert bridge["ProgramArguments"][-1] == str(support / "logs" / "ec20_usb_pty.log")
    assert app["WorkingDirectory"] == str(support)
    assert app["StandardOutPath"] == str(support / "logs" / "launchd-app.out.log")
    env = app["EnvironmentVariables"]
    assert env["PATH"].startswith(f"{resources / 'bin'}:")
    assert env["AGENTCALL_ENV_FILE"] == str(support / ".env")
    assert env["AGENTCALL_DATA_DIR"] == str(support / "data")
    assert env["CALL_LOG_DIR"] == str(support / "data" / "recordings")
    assert env["DYLD_LIBRARY_PATH"] == str(resources / "lib")


def test_plist_needs_update_detects_moved_app(tmp_path):
    launch_dir = tmp_path / "LaunchAgents"
    old_layout = macos_launchd.make_layout(
        tmp_path / "Old.app" / "Contents" / "MacOS" / "CallPilot",
        uid=501,
        support_dir=tmp_path / "Support",
        launch_dir=launch_dir,
    )
    new_layout = macos_launchd.make_layout(
        tmp_path / "New.app" / "Contents" / "MacOS" / "CallPilot",
        uid=501,
        support_dir=tmp_path / "Support",
        launch_dir=launch_dir,
    )
    launch_dir.mkdir()
    path = launch_dir / "com.agentcall.app.plist"
    path.write_bytes(plistlib.dumps(macos_launchd.build_plists(old_layout)["com.agentcall.app"]))

    assert macos_launchd.plist_needs_update(
        path, macos_launchd.build_plists(new_layout)["com.agentcall.app"]
    )


def test_install_launch_agents_writes_and_bootstraps_only_changed_plists(tmp_path):
    calls: list[list[str]] = []
    loaded: set[str] = set()

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "print":
            label = cmd[2].rsplit("/", 1)[-1]
            return subprocess.CompletedProcess(cmd, 0 if label in loaded else 1, "", "")
        if cmd[1] == "bootstrap":
            loaded.add(Path(cmd[-1]).stem)
        if cmd[1] == "bootout":
            loaded.discard(cmd[2].rsplit("/", 1)[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot",
        uid=123,
        support_dir=tmp_path / "Support",
        launch_dir=tmp_path / "LaunchAgents",
    )

    changed = macos_launchd.install_launch_agents(layout, runner=fake_run)

    assert changed == ["com.agentcall.bridge", "com.agentcall.app", "com.agentcall.tray"]
    assert (layout.support_dir / ".env").is_file()
    assert (layout.data_dir).is_dir()
    assert (layout.log_dir).is_dir()
    assert (layout.launch_agents_dir / "com.agentcall.bridge.plist").is_file()
    assert ["launchctl", "bootstrap", "gui/123", str(layout.launch_agents_dir / "com.agentcall.app.plist")] in calls

    calls.clear()
    assert macos_launchd.install_launch_agents(layout, runner=fake_run) == []
    assert calls == [
        ["launchctl", "print", "gui/123/com.agentcall.bridge"],
        ["launchctl", "print", "gui/123/com.agentcall.app"],
        ["launchctl", "print", "gui/123/com.agentcall.tray"],
    ]


def test_install_launch_agents_bootstraps_existing_plist_when_unit_not_loaded(tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        returncode = 1 if cmd[1] == "print" else 0
        return subprocess.CompletedProcess(cmd, returncode, "", "")

    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot",
        uid=123,
        support_dir=tmp_path / "Support",
        launch_dir=tmp_path / "LaunchAgents",
    )
    macos_launchd.install_launch_agents(
        layout,
        runner=lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    calls.clear()
    assert macos_launchd.install_launch_agents(layout, runner=fake_run) == [
        "com.agentcall.bridge",
        "com.agentcall.app",
        "com.agentcall.tray",
    ]
    assert ["launchctl", "bootstrap", "gui/123", str(layout.launch_agents_dir / "com.agentcall.bridge.plist")] in calls
    assert ["launchctl", "bootstrap", "gui/123", str(layout.launch_agents_dir / "com.agentcall.app.plist")] in calls
    assert ["launchctl", "bootstrap", "gui/123", str(layout.launch_agents_dir / "com.agentcall.tray.plist")] in calls


def test_install_launch_agents_waits_for_bootout_before_bootstrap(tmp_path):
    calls: list[list[str]] = []
    print_counts: dict[str, int] = {}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "print":
            label = cmd[2].rsplit("/", 1)[-1]
            print_counts[label] = print_counts.get(label, 0) + 1
            # Old launchd job lingers for two polls after bootout.
            returncode = 0 if print_counts[label] <= 2 else 1
            return subprocess.CompletedProcess(cmd, returncode, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot",
        uid=123,
        support_dir=tmp_path / "Support",
        launch_dir=tmp_path / "LaunchAgents",
    )
    layout.launch_agents_dir.mkdir()
    stale = macos_launchd.build_plists(layout)["com.agentcall.app"]
    stale["ProgramArguments"][-1] = "--old-service"
    (layout.launch_agents_dir / "com.agentcall.app.plist").write_bytes(plistlib.dumps(stale))

    result = macos_launchd.install_launch_agents(
        layout,
        runner=fake_run,
        sleep=lambda _seconds: None,
        unload_timeout=10,
        unload_interval=0.1,
    )

    assert result.failures == []
    app_bootout = ["launchctl", "bootout", "gui/123/com.agentcall.app"]
    app_bootstrap = ["launchctl", "bootstrap", "gui/123", str(layout.launch_agents_dir / "com.agentcall.app.plist")]
    assert calls.index(app_bootout) < calls.index(app_bootstrap)
    app_prints_before_bootstrap = [
        call for call in calls[: calls.index(app_bootstrap)]
        if call == ["launchctl", "print", "gui/123/com.agentcall.app"]
    ]
    assert len(app_prints_before_bootstrap) == 3


def test_install_launch_agents_retries_bootstrap_failures(tmp_path):
    calls: list[list[str]] = []
    bootstrap_attempts: dict[str, int] = {}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "print":
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[1] == "bootstrap":
            label = Path(cmd[-1]).stem
            bootstrap_attempts[label] = bootstrap_attempts.get(label, 0) + 1
            returncode = 1 if bootstrap_attempts[label] < 3 else 0
            return subprocess.CompletedProcess(cmd, returncode, "", "service already loaded")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot",
        uid=123,
        support_dir=tmp_path / "Support",
        launch_dir=tmp_path / "LaunchAgents",
    )

    result = macos_launchd.install_launch_agents(
        layout,
        runner=fake_run,
        sleep=lambda _seconds: None,
        bootstrap_attempts=3,
        bootstrap_retry_seconds=0.1,
    )

    assert result.failures == []
    assert bootstrap_attempts == {
        "com.agentcall.bridge": 3,
        "com.agentcall.app": 3,
        "com.agentcall.tray": 3,
    }


def test_install_launch_agents_reports_bootstrap_failure_details(tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[1] == "print":
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[1] == "bootstrap":
            return subprocess.CompletedProcess(cmd, 37, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot",
        uid=123,
        support_dir=tmp_path / "Support",
        launch_dir=tmp_path / "LaunchAgents",
    )

    result = macos_launchd.install_launch_agents(
        layout,
        runner=fake_run,
        sleep=lambda _seconds: None,
        bootstrap_attempts=2,
    )

    assert result.changed == []
    assert [failure.label for failure in result.failures] == [
        "com.agentcall.bridge",
        "com.agentcall.app",
        "com.agentcall.tray",
    ]
    assert all(failure.action == "bootstrap" for failure in result.failures)
    assert all(failure.returncode == 37 for failure in result.failures)
    assert all("boom" in failure.message for failure in result.failures)


def test_uninstall_launch_agents_boots_out_and_removes_plists(tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot",
        uid=123,
        support_dir=tmp_path / "Support",
        launch_dir=tmp_path / "LaunchAgents",
    )
    layout.launch_agents_dir.mkdir()
    for label in macos_launchd.LAUNCH_LABELS:
        (layout.launch_agents_dir / f"{label}.plist").write_text("{}", encoding="utf-8")

    removed = macos_launchd.uninstall_launch_agents(layout, runner=fake_run)

    assert removed == ["com.agentcall.tray", "com.agentcall.app", "com.agentcall.bridge"]
    assert calls == [
        ["launchctl", "bootout", "gui/123/com.agentcall.tray"],
        ["launchctl", "bootout", "gui/123/com.agentcall.app"],
        ["launchctl", "bootout", "gui/123/com.agentcall.bridge"],
    ]
    assert not any(layout.launch_agents_dir.glob("com.agentcall.*.plist"))


def test_build_plists_tray_unit_keepalive_and_no_caffeinate(tmp_path):
    exe = tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot"
    support = tmp_path / "Support"
    layout = macos_launchd.make_layout(
        exe, uid=501, support_dir=support, launch_dir=tmp_path / "LaunchAgents"
    )

    tray = macos_launchd.build_plists(layout)["com.agentcall.tray"]

    # 无参数入口=菜单栏；UI 进程不裹 caffeinate（防睡眠由 --service 承担）。
    assert tray["ProgramArguments"] == [str(exe.resolve())]
    # 崩溃（非零退出）自动拉起；单例让位/主动退出（码 0）不拉起——防重启风暴。
    assert tray["KeepAlive"] == {"SuccessfulExit": False}
    assert tray["RunAtLoad"] is True
    assert tray["StandardOutPath"] == str(support / "logs" / "launchd-tray.out.log")
    assert tray["StandardErrorPath"] == str(support / "logs" / "launchd-tray.err.log")


def test_install_no_restart_labels_updates_plist_without_bootout(tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # print 一律 0 = 全部单元都已加载（在跑）。
        return subprocess.CompletedProcess(cmd, 0, "", "")

    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot",
        uid=123,
        support_dir=tmp_path / "Support",
        launch_dir=tmp_path / "LaunchAgents",
    )
    layout.launch_agents_dir.mkdir()
    plists = macos_launchd.build_plists(layout)
    for label, data in plists.items():
        (layout.launch_agents_dir / f"{label}.plist").write_bytes(plistlib.dumps(data))
    # tray plist 过期（模拟升级期：安装方就是在跑的 tray 自己）。
    stale = dict(plists["com.agentcall.tray"])
    stale["ProgramArguments"] = ["/old/CallPilot"]
    tray_path = layout.launch_agents_dir / "com.agentcall.tray.plist"
    tray_path.write_bytes(plistlib.dumps(stale))

    result = macos_launchd.install_launch_agents(
        layout, runner=fake_run, no_restart_labels={"com.agentcall.tray"}
    )

    assert result.changed == ["com.agentcall.tray"]
    assert result.failures == []
    with tray_path.open("rb") as f:
        assert plistlib.load(f) == plists["com.agentcall.tray"]  # 原地写盘生效
    # 不 bootout 在跑实例（否则杀掉安装方自己）、已加载也不重复 bootstrap。
    assert ["launchctl", "bootout", "gui/123/com.agentcall.tray"] not in calls
    assert not any(cmd[1] == "bootstrap" and "tray" in cmd[-1] for cmd in calls)


def test_install_no_restart_labels_still_bootstraps_when_not_loaded(tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        returncode = 1 if cmd[1] == "print" else 0  # 均未加载
        return subprocess.CompletedProcess(cmd, returncode, "", "")

    layout = macos_launchd.make_layout(
        tmp_path / "CallPilot.app" / "Contents" / "MacOS" / "CallPilot",
        uid=123,
        support_dir=tmp_path / "Support",
        launch_dir=tmp_path / "LaunchAgents",
    )

    result = macos_launchd.install_launch_agents(
        layout, runner=fake_run, no_restart_labels={"com.agentcall.tray"}
    )

    assert "com.agentcall.tray" in result.changed
    assert ["launchctl", "bootout", "gui/123/com.agentcall.tray"] not in calls
    assert [
        "launchctl",
        "bootstrap",
        "gui/123",
        str(layout.launch_agents_dir / "com.agentcall.tray.plist"),
    ] in calls
