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

    def check_approval(self):
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
        sm.cleanup_legacy(layout, runner)
        sm.cleanup_legacy(layout, runner)
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
