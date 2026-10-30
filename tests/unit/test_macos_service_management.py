"""SMAppService lifecycle tests: no native registrations or running jobs touched."""

import plistlib
import subprocess

import pytest

from agentcall import macos_launchd as ld
from agentcall import macos_service_management as sm


class Services:
    def __init__(self, events):
        self.events = events
        self.registered = set()

    def register(self, label):
        self.events.append(("register", label))
        self.registered.add(label)

    def unregister(self, label):
        self.events.append(("unregister", label))
        self.registered.discard(label)

    def check_approval(self, **kwargs):
        pass


def fixture(tmp_path):
    layout = ld.make_layout(tmp_path / "CallPilot.app/Contents/MacOS/CallPilot", uid=123,
                            support_dir=tmp_path / "support", launch_dir=tmp_path / "agents")
    events = []

    def runner(cmd, **kwargs):
        events.append((cmd[1], cmd[-1].split("/")[-1]))
        if cmd[1] == "print":
            label = cmd[-1].split("/")[-1]
            return subprocess.CompletedProcess(cmd, 113, "", f'Could not find service "{label}"')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return layout, events, runner, Services(events)


def test_embedded_plists_are_relocatable_and_do_not_start_services_before_reconcile():
    plists = sm.bundled_plists()
    assert set(plists) == set(ld.LAUNCH_LABELS)
    for label, value in plists.items():
        assert value["Label"] == label
        assert value["BundleProgram"] == "Contents/MacOS/CallPilot"
        assert "/usr/bin/caffeinate" not in value["ProgramArguments"]
        assert "WorkingDirectory" not in value
        assert "EnvironmentVariables" not in value
        assert value["RunAtLoad"] == (label == ld.TRAY_LABEL)
    # launchd respawn could execute an overwritten bundle beside an old peer.
    assert plists[ld.APP_LABEL]["KeepAlive"] is False
    assert plists[ld.BRIDGE_LABEL]["KeepAlive"] is False


def test_replacement_lease_stop_order_bridge_verification_and_no_self_unregister(tmp_path):
    layout, events, runner, services = fixture(tmp_path)

    def tray_loaded(cmd, **kwargs):
        if cmd[1] == "print" and cmd[-1].endswith(ld.TRAY_LABEL):
            return subprocess.CompletedProcess(cmd, 0, "state = running", "")
        return runner(cmd, **kwargs)

    result = sm.install(layout, tray_loaded, services=services,
                        force_restart_labels={ld.APP_LABEL, ld.BRIDGE_LABEL},
                        no_restart_labels={ld.TRAY_LABEL},
                        before_restart=lambda: events.append(("lease", "")),
                        verify_bridge=lambda: events.append(("verify", "")) or True)
    assert not result.failures
    assert events.index(("lease", "")) < events.index(("unregister", ld.APP_LABEL))
    assert events.index(("unregister", ld.APP_LABEL)) < events.index(("unregister", ld.BRIDGE_LABEL))
    assert events.index(("register", ld.BRIDGE_LABEL)) < events.index(("verify", ""))
    assert events.index(("verify", "")) < events.index(("register", ld.APP_LABEL))
    assert ("unregister", ld.TRAY_LABEL) not in events
    assert not layout.launch_agents_dir.exists()


def test_failed_bridge_never_starts_service(tmp_path):
    layout, events, runner, services = fixture(tmp_path)
    result = sm.install(layout, runner, services=services,
                        force_restart_labels={ld.APP_LABEL, ld.BRIDGE_LABEL}, verify_bridge=lambda: False)
    assert result.failures
    assert ("register", ld.APP_LABEL) not in events


def test_lease_rejection_changes_nothing(tmp_path):
    layout, events, runner, services = fixture(tmp_path)

    def busy():
        raise RuntimeError("busy")

    with pytest.raises(RuntimeError, match="busy"):
        sm.install(layout, runner, services=services, before_restart=busy,
                   force_restart_labels={ld.APP_LABEL, ld.BRIDGE_LABEL})
    assert events == []


def test_legacy_cleanup_allowlist_idempotence_and_log(tmp_path, caplog):
    layout, events, runner, _ = fixture(tmp_path)
    layout.launch_agents_dir.mkdir()
    for label in (*ld.LAUNCH_LABELS, "com.agentcall.other", "com.other.app"):
        (layout.launch_agents_dir / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label}))
    with caplog.at_level("INFO"):
        assert sm.cleanup_legacy(layout, runner) is True
        assert sm.cleanup_legacy(layout, runner) is False
    assert len([e for e in events if e[0] == "bootout"]) == 3
    assert (layout.launch_agents_dir / "com.agentcall.other.plist").exists()
    assert "Removed legacy" in caplog.text
    assert events.index(("bootout", ld.APP_LABEL)) < events.index(("bootout", ld.BRIDGE_LABEL))


def test_legacy_cleanup_refuses_symlinks_wrong_labels_and_unknown_query_errors(tmp_path):
    layout, _, runner, _ = fixture(tmp_path)
    layout.launch_agents_dir.mkdir()
    path = layout.launch_agents_dir / f"{ld.APP_LABEL}.plist"
    path.write_bytes(plistlib.dumps({"Label": "com.other.app"}))
    with pytest.raises(RuntimeError, match="label"):
        sm.cleanup_legacy(layout, runner)
    path.unlink()
    try:
        path.symlink_to(tmp_path / "missing")
    except OSError:
        pytest.skip("Host does not permit symlinks; macOS/Linux exercise this migration guard")
    with pytest.raises(RuntimeError, match="symlink"):
        sm.cleanup_legacy(layout, runner)


def test_autostart_changes_only_future_tray_scheduling(tmp_path):
    layout, events, runner, _ = fixture(tmp_path)
    sm.set_login_enabled(layout, False, runner)
    assert events == [("disable", ld.TRAY_LABEL)]
    events.clear()
    sm.set_login_enabled(layout, True, runner)
    assert events == [("enable", ld.TRAY_LABEL)]


@pytest.mark.parametrize("output, running", [
    ("state = not running", False), ("state = waiting", False),
    ("state = running\n\tpid = 42", True), ("", True), ("state = spawn scheduled", True),
])
def test_idle_bootstrapped_jobs_are_not_unknown_running_services(tmp_path, output, running):
    layout, _, _, _ = fixture(tmp_path)
    assert sm.agent_running(ld.APP_LABEL, layout, lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, output, "")) is running


def test_unknown_launchctl_failure_is_not_treated_as_unloaded(tmp_path):
    layout, _, _, _ = fixture(tmp_path)
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 5, "", "permission denied")
    with pytest.raises(RuntimeError, match="Cannot inspect"):
        sm.agent_running(ld.APP_LABEL, layout, runner)
    with pytest.raises(RuntimeError, match="Cannot confirm"):
        sm.wait_unloaded(ld.APP_LABEL, layout, runner)


def test_native_approval_never_registers_or_unregisters_behind_user_back(monkeypatch):
    native = object.__new__(sm.NativeServices)
    class Service:
        def status(self):
            return 2

        def registerAndReturnError_(self, error):
            pytest.fail("must not register when user approval is required")
    monkeypatch.setattr(native, "_service", lambda label: Service())
    with pytest.raises(sm.ApprovalRequired, match="Login Items"):
        native.register(ld.APP_LABEL)


class NativeService:
    def __init__(self, status):
        self.state = status
        self.calls = []

    def status(self):
        return self.state

    def registerAndReturnError_(self, error):
        self.calls.append("register")
        self.state = 1
        return True, None

    def unregisterAndReturnError_(self, error):
        self.calls.append("unregister")
        self.state = 0
        return True, None


@pytest.mark.parametrize("status", [0, 3])
def test_native_absent_service_skips_unregister_and_can_register(monkeypatch, status):
    native = object.__new__(sm.NativeServices)
    service = NativeService(status)
    monkeypatch.setattr(native, "_service", lambda label: service)
    native.unregister(ld.APP_LABEL)
    assert service.calls == []
    native.register(ld.APP_LABEL)
    assert service.calls == ["register"]
    assert service.state == 1


@pytest.mark.parametrize("status", [0, 1, 3])
def test_native_approval_accepts_nonblocked_status_without_wait(monkeypatch, status):
    native = object.__new__(sm.NativeServices)
    monkeypatch.setattr(native, "_service", lambda label: NativeService(status))
    native.check_approval(sleep=lambda _: pytest.fail("No approval wait needed"))


def test_native_approval_without_cleanup_fails_immediately(monkeypatch):
    native = object.__new__(sm.NativeServices)
    checks = []

    def service(label):
        checks.append(label)
        return NativeService(2)

    monkeypatch.setattr(native, "_service", service)
    with pytest.raises(sm.ApprovalRequired):
        native.check_approval(sleep=lambda _: pytest.fail("Must not wait without legacy cleanup"))
    assert len(checks) == 1


def test_native_approval_timeout_is_bounded_and_actionable(monkeypatch):
    native = object.__new__(sm.NativeServices)
    service = NativeService(2)
    monkeypatch.setattr(native, "_service", lambda label: service)
    waits = []
    with pytest.raises(sm.ApprovalRequired) as error:
        native.check_approval(wait=True, sleep=waits.append)
    assert waits == [0.5] * 20
    assert "登录项与扩展" in str(error.value)
    assert "Login Items & Extensions" in str(error.value)
    assert "caffeinate" in str(error.value)
    assert "重试" in str(error.value) and "retry" in str(error.value)
    assert service.calls == []


@pytest.mark.parametrize("cleared_status", [0, 3])
@pytest.mark.parametrize("loaded_idle", [False, True])
def test_install_cleans_idle_legacy_before_waiting_for_btm(monkeypatch, tmp_path, cleared_status, loaded_idle):
    layout, events, absent_runner, _ = fixture(tmp_path)
    paths = []
    for directory in (layout.launch_agents_dir, layout.support_dir / "LaunchAgents"):
        directory.mkdir(parents=True)
        for label in ld.LAUNCH_LABELS:
            path = directory / f"{label}.plist"
            path.write_bytes(plistlib.dumps({"Label": label}))
            if label != ld.TRAY_LABEL:
                paths.append(path)
    idle = {ld.APP_LABEL, ld.BRIDGE_LABEL} if loaded_idle else set()

    def runner(cmd, **kwargs):
        label = cmd[-1].split("/")[-1]
        if cmd[1] == "print" and label in idle:
            return subprocess.CompletedProcess(cmd, 0, "state = not running", "")
        if cmd[1] == "bootout":
            idle.discard(label)
            if not loaded_idle:
                return subprocess.CompletedProcess(cmd, 3, "", "No such process")
        return absent_runner(cmd, **kwargs)

    native = object.__new__(sm.NativeServices)
    agents = {label: NativeService(2) for label in ld.LAUNCH_LABELS}

    def service(label):
        assert all(not path.exists() for path in paths), "Approval checked before legacy cleanup"
        return agents[label]

    monkeypatch.setattr(native, "_service", service)
    waits = []

    def sleep(delay):
        waits.append(delay)
        for agent in agents.values():
            agent.state = cleared_status

    result = sm.install(layout, runner, services=native, sleep=sleep)
    assert not result.failures
    assert waits == [0.5]
    assert all(agent.calls == ["register"] for agent in agents.values())
    assert ("bootout", ld.TRAY_LABEL) not in events
    assert (layout.launch_agents_dir / f"{ld.TRAY_LABEL}.plist").exists()


def test_install_without_legacy_cleanup_does_not_wait_for_approval(monkeypatch, tmp_path):
    layout, _, runner, _ = fixture(tmp_path)
    native = object.__new__(sm.NativeServices)
    monkeypatch.setattr(native, "_service", lambda label: NativeService(2))
    result = sm.install(layout, runner, services=native,
                        sleep=lambda _: pytest.fail("Must not wait without legacy cleanup"))
    assert result.failures[0].action == "approval-required"


def test_install_does_not_preclean_running_legacy_job(monkeypatch, tmp_path):
    layout, events, absent_runner, _ = fixture(tmp_path)
    layout.launch_agents_dir.mkdir()
    path = layout.launch_agents_dir / f"{ld.APP_LABEL}.plist"
    path.write_bytes(plistlib.dumps({"Label": ld.APP_LABEL}))

    def runner(cmd, **kwargs):
        if cmd[1] == "print" and cmd[-1].endswith(ld.APP_LABEL):
            return subprocess.CompletedProcess(cmd, 0, "state = running\npid = 42", "")
        return absent_runner(cmd, **kwargs)

    native = object.__new__(sm.NativeServices)
    monkeypatch.setattr(native, "_service", lambda label: NativeService(2))
    result = sm.install(layout, runner, services=native,
                        sleep=lambda _: pytest.fail("Running legacy job was not cleaned; must not wait"))
    assert result.failures and "caffeinate" in result.failures[0].stderr
    assert result.failures[0].action == "approval-required"
    assert path.exists()
    assert not any(action == "bootout" for action, _ in events)


def test_nonapproval_check_error_is_not_classified_as_approval(tmp_path):
    layout, _, runner, services = fixture(tmp_path)

    def broken_check(**kwargs):
        raise RuntimeError("native status lookup failed")

    services.check_approval = broken_check
    result = sm.install(layout, runner, services=services)
    assert result.failures[0].action != "approval-required"
    assert "native status lookup failed" in result.failures[0].stderr
