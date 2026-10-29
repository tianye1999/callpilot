"""Upgrade planning never uses real launchctl or the running development service."""

import json
import sys
from types import SimpleNamespace

import pytest

from agentcall import macos_launchd as launchd
from agentcall import macos_upgrade as upgrade


@pytest.fixture(autouse=True)
def fake_locks(monkeypatch):
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(flock=lambda *args: None, LOCK_EX=1))
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
        {"version": "26.10.1", "executable": str(layout.executable)}))


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


def test_matching_runtime_does_not_restart(tmp_path):
    layout = layout_at(tmp_path)
    prepare(layout)
    installs = []
    upgrade.reconcile(layout, lambda: {"version": "26.10.1", "call_active": False,
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
    upgrade.reconcile(layout, lambda: {"version": "26.10.1", "call_active": False, "upgrade_guard": True},
                      install=lambda *a, **kw: installs.append(kw))
    assert installs[0]["force_restart_labels"] == {launchd.APP_LABEL, launchd.BRIDGE_LABEL}


def test_legacy_service_fails_closed_without_installing(tmp_path):
    with pytest.raises(upgrade.LegacyUpgradeRequired):
        upgrade.reconcile(layout_at(tmp_path), lambda: {"call_active": False},
                          install=lambda *a, **kw: pytest.fail("Legacy service must not be stopped"))


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
