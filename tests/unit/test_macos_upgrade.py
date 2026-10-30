"""Upgrade planning never uses real launchctl or the running development service."""

import json
import sys
from types import SimpleNamespace

import pytest

from agentcall import macos_launchd as launchd
from agentcall import macos_upgrade as upgrade
from agentcall.app_version import VERSION


@pytest.fixture(autouse=True)
def fake_locks(monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(flock=lambda *args: None, LOCK_EX=1, LOCK_NB=2))
    monkeypatch.setattr(upgrade, "_bridge_running", lambda pid: True)


def layout_at(tmp_path):
    return launchd.make_layout(tmp_path / "CallPilot", uid=123,
                              support_dir=tmp_path / "support", launch_dir=tmp_path / "agents")


def prepare(layout):
    layout.launch_agents_dir.mkdir(parents=True)
    for label, data in launchd.build_plists(layout).items():
        (layout.launch_agents_dir / f"{label}.plist").write_bytes(launchd.plist_bytes(data))
    layout.support_dir.mkdir(exist_ok=True)
    (layout.support_dir / "bridge-version.json").write_text(json.dumps(
        {"version": VERSION, "executable": str(layout.executable)}))


def test_version_mismatch_restarts_pair_after_call_and_queue_end(tmp_path):
    layout = layout_at(tmp_path)
    prepare(layout)
    samples = iter([
        {"version": "26.9.1", "call_active": True},
        {"version": "26.9.1", "call_active": False, "dial_queue_busy": True},
        {"version": "26.9.1", "call_active": False, "upgrade_guard": True},
    ])
    installs, waits = [], []
    upgrade.reconcile(layout, lambda: next(samples), install=lambda *a, **kw: installs.append(kw),
                      sleep=waits.append)
    assert waits == [1, 1]
    assert installs[0]["force_restart_labels"] == {launchd.APP_LABEL, launchd.BRIDGE_LABEL}


def test_sm_login_bootstrapped_idle_jobs_do_not_block_reconcile(monkeypatch, tmp_path):
    from agentcall import macos_service_management as sm

    layout = layout_at(tmp_path)
    monkeypatch.setattr(sm, "uses_service_management", lambda _: True)
    monkeypatch.setattr(sm, "agent_running", lambda *args: False)
    installs = []
    upgrade.reconcile(layout, lambda: {}, install=lambda *a, **kw: installs.append(kw),
                      sleep=lambda _: pytest.fail("Dormant registered jobs must not block login"))
    assert installs[0]["force_restart_labels"] == {launchd.APP_LABEL, launchd.BRIDGE_LABEL}


def test_background_recovery_defers_busy_without_leaving_install_thread_waiting(tmp_path):
    result = upgrade.reconcile(layout_at(tmp_path), lambda: {"call_active": True}, wait_when_busy=False,
                               install=lambda *a, **kw: pytest.fail("busy must not install"),
                               sleep=lambda _: pytest.fail("background poll must return, not wait"))
    assert result.changed == [] and result.failures == []


def test_background_recovery_defers_lease_race(tmp_path):
    def install(*args, **kwargs):
        kwargs["before_restart"]()
        pytest.fail("lease was not granted")
    result = upgrade.reconcile(layout_at(tmp_path), lambda: {"call_active": False, "upgrade_guard": True},
                               wait_when_busy=False, acquire=lambda: None, install=install,
                               sleep=lambda _: pytest.fail("lease contention must return to tray"))
    assert result.changed == [] and result.failures == []


def test_background_recovery_does_not_wait_for_another_upgrade(monkeypatch, tmp_path):
    def busy_lock(fd, flags):
        assert flags == 3
        raise BlockingIOError()

    monkeypatch.setattr(sys.modules["fcntl"], "flock", busy_lock)
    result = upgrade.reconcile(layout_at(tmp_path), lambda: pytest.fail("must not inspect during other upgrade"),
                               wait_when_busy=False)
    assert result.changed == [] and result.failures == []


def test_matching_runtime_does_not_restart(tmp_path):
    layout = layout_at(tmp_path)
    prepare(layout)
    installs = []
    upgrade.reconcile(layout, lambda: {"version": VERSION, "call_active": False,
                                       "executable": str(layout.executable)},
                      install=lambda *a, **kw: installs.append(kw))
    assert not installs[0]["force_restart_labels"]


def test_unknown_call_state_never_restarts_loaded_service(tmp_path):
    layout = layout_at(tmp_path)
    samples = iter([{}, {"version": "old"}, {"version": "old", "call_active": False, "upgrade_guard": True}])
    waits, installs = [], []
    upgrade.reconcile(layout, lambda: next(samples), loaded=lambda *a: True,
                      install=lambda *a, **kw: installs.append(kw), sleep=waits.append)
    assert waits == [1, 1]
    assert len(installs) == 1


def test_changed_bridge_or_program_path_restarts_pair(tmp_path):
    layout = layout_at(tmp_path)
    prepare(layout)
    (layout.support_dir / "bridge-version.json").write_text('{"version":"26.9.1"}')
    installs = []
    upgrade.reconcile(layout, lambda: {"version": VERSION, "call_active": False, "upgrade_guard": True},
                      install=lambda *a, **kw: installs.append(kw))
    assert installs[0]["force_restart_labels"] == {launchd.APP_LABEL, launchd.BRIDGE_LABEL}


def test_legacy_service_fails_closed_without_installing(tmp_path):
    with pytest.raises(upgrade.LegacyUpgradeRequired):
        upgrade.reconcile(layout_at(tmp_path), lambda: {"call_active": False},
                          install=lambda *a, **kw: pytest.fail("Legacy service must not be stopped"))


def test_running_bundle_upgrade_preserves_legacy_plists(tmp_path):
    from agentcall import macos_service_management as sm

    layout = launchd.make_layout(tmp_path / "CallPilot.app/Contents/MacOS/CallPilot", uid=123,
                                 support_dir=tmp_path / "support", launch_dir=tmp_path / "agents")
    sm.embedded_directory(layout).mkdir(parents=True)
    layout.launch_agents_dir.mkdir()
    paths = []
    for label in (launchd.APP_LABEL, launchd.BRIDGE_LABEL):
        path = layout.launch_agents_dir / f"{label}.plist"
        path.write_bytes(launchd.plist_bytes({"Label": label}))
        paths.append(path)
    with pytest.raises(upgrade.LegacyUpgradeRequired):
        upgrade.reconcile(layout, lambda: {"version": "0.6", "call_active": False},
                          loaded=lambda *a: True,
                          install=lambda *a, **kw: pytest.fail("Running legacy jobs must not be cleaned"))
    assert all(path.exists() for path in paths)


def test_busy_lease_retries_and_releases_after_install_failure(tmp_path):
    layout = layout_at(tmp_path)
    released, attempts, waits = [], [], []
    leases = iter([None, lambda: released.append(True)])
    def install(*args, **kwargs):
        attempts.append(True)
        kwargs["before_restart"]()
        raise OSError("fake bootstrap failure")
    with pytest.raises(OSError):
        upgrade.reconcile(layout, lambda: {"call_active": False, "upgrade_guard": True},
                          acquire=lambda: next(leases), install=install, sleep=waits.append)
    assert attempts == [True, True] and waits == [1] and released == [True]


def test_stale_bridge_marker_is_not_a_running_bridge(tmp_path, monkeypatch):
    layout = layout_at(tmp_path)
    prepare(layout)
    monkeypatch.setattr(upgrade, "_bridge_running", lambda pid: False)
    assert not upgrade.bridge_current(layout)
