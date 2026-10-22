"""Linux deployment contracts and sandboxed installer execution (no sudo/hardware)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from dotenv import dotenv_values

from agentcall import config

ROOT = Path(__file__).resolve().parents[2]
LINUX = ROOT / "scripts" / "linux"


def test_linux_udev_rules_match_device_then_interface():
    ignore = (LINUX / "udev" / "77-callpilot-mm-ignore-sim7600.rules").read_text(encoding="utf-8")
    assert ignore.strip() == (
        'ACTION!="remove", SUBSYSTEMS=="usb", ATTRS{idVendor}=="1e0e", '
        'ATTRS{idProduct}=="9001", ENV{ID_MM_DEVICE_IGNORE}="1"'
    )
    rules = (LINUX / "udev" / "99-callpilot-sim7600.rules").read_text(encoding="utf-8")
    lines = [line for line in rules.splitlines() if line and not line.startswith("#")]
    assert 'ATTRS{idVendor}=="1e0e"' in lines[0]
    assert 'ATTRS{idProduct}=="9001"' in lines[0]
    for line, interface, name in zip(lines[1:], ["02", "04"], ["at", "pcm"], strict=True):
        assert 'SUBSYSTEM=="tty"' in line and 'KERNEL=="ttyUSB*"' in line
        assert f'ATTRS{{bInterfaceNumber}}=="{interface}"' in line
        assert f'SYMLINK+="callpilot-sim7600-{name}"' in line
        assert 'GROUP="dialout"' in line and 'MODE="0660"' in line
        # VID/PID and interface attributes live on different sysfs ancestors.
        assert "idVendor" not in line and 'ENV{.CALLPILOT_SIM7600}=="1"' in line


def test_linux_env_example_and_shared_default_isolation():
    values = dotenv_values(LINUX / ".env.example")
    expected = {
        "MODEM_VENDOR": "simcom", "MODEM_PORT": "/dev/callpilot-sim7600-at",
        "MODEM_AUDIO_MODE": "nmea", "MODEM_PCM_PORT": "/dev/callpilot-sim7600-pcm",
        "MODEM_PCM_BAUD": "115200", "MODEM_PCM_PIN_8K": "true", "WEB_OPEN_BROWSER": "false",
        "MODEM_RESET_AFTER_CALL": "true",
        "WEB_HOST": "127.0.0.1", "WEB_PORT": "47100",
    }
    for key, value in expected.items():
        assert values[key] == value
        config.get_spec(key)
    shared = dotenv_values(ROOT / ".env.example")
    assert "MODEM_PCM_PIN_8K" not in shared  # Do not override the Linux platform default.
    assert "MODEM_RESET_AFTER_CALL" not in shared
    assert shared["WEB_OPEN_BROWSER"] == "true"
    assert shared["MODEM_PCM_BAUD"] == "921600"


@pytest.mark.parametrize("repo", ["/home/edge/callpilot", "/home/edge/callpilot with spaces"])
def test_rendered_unit_uses_setting_specific_path_quoting(repo):
    template = (LINUX / "systemd" / "callpilot-edge.service.in").read_text(encoding="utf-8")
    unit = template.replace("@REPO@", repo).replace("@USER@", "edge")
    working_directory = next(
        line.removeprefix("WorkingDirectory=")
        for line in unit.splitlines() if line.startswith("WorkingDirectory=")
    )
    assert working_directory.startswith("/")
    assert '"' not in working_directory and "'" not in working_directory
    assert working_directory == repo
    assert f'ExecStart="{repo}/.venv/bin/python" app.py' in unit.splitlines()
    assert f'Environment="PATH={repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin"' in unit.splitlines()


@pytest.fixture
def installer(tmp_path):
    if os.name == "nt" or shutil.which("bash") is None:
        pytest.skip("Installer execution requires a POSIX host with bash")
    root = tmp_path / "repo with spaces"
    shutil.copytree(LINUX, root / "scripts" / "linux")
    shutil.copy2(ROOT / "scripts" / "setup.sh", root / "scripts" / "setup.sh")
    shutil.copy2(ROOT / ".env.example", root / ".env.example")
    (root / ".env").write_text("KEEP_EXISTING=secret-placeholder\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Keep discovery deterministic: never find a real host uv or use the user's HOME.
    for command in (
        "bash", "dirname", "sed", "mktemp", "install", "basename", "rm", "cp",
        "mkdir", "rmdir", "mv", "tar", "sleep",
    ):
        executable = shutil.which(command)
        assert executable is not None
        (bin_dir / command).symlink_to(executable)
    trace = tmp_path / "trace.jsonl"
    stub = f"""#!{sys.executable} -S
import json, os, shutil, sys
from pathlib import Path
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ['TRACE'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps([tool, *args]) + '\\n')
if tool == os.environ.get('FAIL_TOOL'):
    sys.exit(1)
if tool == 'git' and os.environ.get('TEST_FAIL_CHECKOUT') == '1' and 'checkout-index' in args:
    checkout = args.index('checkout-index')
    paths = sys.stdin.buffer.read().split(b'\\0')
    import subprocess
    subprocess.run([os.environ['REAL_GIT'], *args[:checkout + 1], '--force', '--',
                    os.fsdecode(next(path for path in paths if path))], check=True)
    sys.exit(1)
if tool == 'systemctl' and args[:1] == ['is-active'] and os.environ.get('TEST_SERVICE_ACTIVE') == '0':
    sys.exit(3)
if tool == 'git':
    if os.environ.get('TEST_NON_GIT') == '1' and args[:1] == ['rev-parse']:
        sys.exit(128)
    if 'remote' in args and 'add' in args and os.environ.get('TEST_ADOPT_REMOTE'):
        args[-1] = os.environ['TEST_ADOPT_REMOTE']
    os.execv(os.environ['REAL_GIT'], ['git', *args])
elif tool == 'curl':
    url = args[-1]
    if url.endswith('/api/meta'):
        if '--write-out' in args:
            print(os.environ.get('TEST_HEALTH_CODE', '200'), end='')
            sys.exit(int(os.environ.get('TEST_HEALTH_EXIT', '0')))
        print(os.environ.get('TEST_META', '{{"call_active": false, "dial_queue_busy": false}}'))
        sys.exit(int(os.environ.get('TEST_META_EXIT', '0')))
    elif url.endswith('/api/call/queue'):
        busy_after = int(os.environ.get('TEST_BUSY_AFTER', '0'))
        lines = Path(os.environ['TRACE']).read_text(encoding='utf-8').splitlines()
        count = sum('/api/call/queue' in line for line in lines)
        if busy_after and count >= busy_after:
            print('{{"ok": true, "active": true, "pending": [], "current": null}}')
        else:
            print(os.environ.get('TEST_QUEUE', '{{"ok": true, "active": false, "pending": [], "current": null}}'))
    elif url.endswith('/api/remote_dialer/status'):
        print(os.environ.get('TEST_REMOTE', '{{"ok": true, "active": false}}'))
    else:
        sys.exit(1)
elif tool == 'python':
    os.execv(sys.executable, [sys.executable, *args])
elif tool == 'uname':
    print(os.environ.get('TEST_OS', 'Linux'))
elif tool == 'id':
    print(os.environ.get('TEST_UID', '1000') if args == ['-u'] else 'edge')
elif tool == 'uv' and args == ['python', 'find', '3.13']:
    print(sys.executable)
elif tool == 'sudo' and args == ['-v'] and os.environ.get('TEST_SUDO_VERIFYPW_ALL') == '1':
    print('sudo: a password is required', file=sys.stderr)
    sys.exit(1)
elif tool == 'sudo' and args != ['-v']:
    executable = shutil.which(args[0], path=os.environ['SUDO_PATH'])
    if executable is None:
        print('sudo: ' + args[0] + ': command not found', file=sys.stderr)
        sys.exit(1)
    if args[0] == 'install':
        target = Path(os.environ['SYSTEM_ROOT']) / args[-1].lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args[-2], target)
    elif args[0] in ('usermod', 'true', 'systemctl'):
        os.execv(executable, args)
"""
    for name in (
        "uname", "id", "getent", "sudo", "true", "uv", "ffmpeg", "systemctl",
        "udevadm", "usermod", "git", "curl",
    ):
        path = bin_dir / name
        path.write_text(stub, encoding="utf-8")
        path.chmod(0o755)
    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True)
    for name in ("python", "pip"):
        path = venv / name
        path.write_text(stub, encoding="utf-8")
        path.chmod(0o755)
    env = {
        **os.environ, "PATH": str(bin_dir), "HOME": str(tmp_path / "home"), "TRACE": str(trace),
        "SYSTEM_ROOT": str(tmp_path / "system"), "SUDO_PATH": str(bin_dir),
        "REAL_GIT": shutil.which("git") or "", "CALLPILOT_UPDATE_HEALTH_TIMEOUT": "1",
    }

    def run(*args, script="install.sh", **overrides):
        return subprocess.run(
            ["bash", str(root / "scripts" / "linux" / script), *args],
            env={**env, **overrides}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )

    return root, trace, Path(env["SYSTEM_ROOT"]), bin_dir, run


def test_install_is_repeatable_and_now_is_explicit(installer):
    root, trace, system_root, _bin, run = installer
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert ["uv", "python", "install", "3.13"] in calls
    assert ["uv", "python", "find", "3.13"] in calls
    assert ["sudo", "systemctl", "enable", "callpilot-edge.service"] in calls
    assert not any("--now" in call or "start" in call or "restart" in call for call in calls)
    assert ["sudo", "udevadm", "control", "--reload-rules"] in calls
    assert any(call[:3] == ["sudo", "udevadm", "trigger"] for call in calls)
    assert any(call[:2] == ["pip", "install"] for call in calls)  # Real setup.sh, fake pip.
    unit_path = system_root / "etc" / "systemd" / "system" / "callpilot-edge.service"
    unit = unit_path.read_text(encoding="utf-8")
    assert "User=edge\n" in unit and f"WorkingDirectory={root}" in unit.splitlines()
    assert f'ExecStart="{root}/.venv/bin/python" app.py' in unit
    for line in (
        "After=network-online.target", "Wants=network-online.target", "Restart=on-failure",
        "RestartSec=5", "StandardOutput=journal", "StandardError=journal",
        "SupplementaryGroups=dialout", "Environment=WEB_OPEN_BROWSER=false", "KillSignal=SIGINT",
    ):
        assert line in unit
    result = run("--now")
    assert result.returncode == 0, result.stdout + result.stderr
    assert unit_path.read_text(encoding="utf-8") == unit
    assert (root / ".env").read_text(encoding="utf-8") == "KEEP_EXISTING=secret-placeholder\n"
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert ["sudo", "systemctl", "enable", "--now", "callpilot-edge.service"] in calls
    assert f"Using uv: {_bin / 'uv'}" in result.stdout


def test_install_finds_uv_in_local_bin_outside_path(installer, tmp_path):
    _root, trace, _system_root, bin_dir, run = installer
    home = tmp_path / "home with spaces"
    uv_path = home / ".local" / "bin" / "uv"
    uv_path.parent.mkdir(parents=True)
    shutil.move(bin_dir / "uv", uv_path)

    result = run(HOME=str(home))

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Using uv: {uv_path}" in result.stdout
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert ["uv", "python", "install", "3.13"] in calls
    assert ["uv", "python", "find", "3.13"] in calls


def test_install_missing_uv_only_prints_guidance(installer):
    _root, trace, system_root, bin_dir, run = installer
    (bin_dir / "uv").unlink()
    result = run()
    assert result.returncode != 0
    assert "https://docs.astral.sh/uv/getting-started/installation/" in result.stderr
    assert not system_root.exists()
    assert not any(json.loads(line)[0] == "sudo" for line in trace.read_text(encoding="utf-8").splitlines())


def test_install_uses_command_auth_when_validation_requires_password(installer):
    _root, trace, system_root, _bin_dir, run = installer

    result = run(TEST_SUDO_VERIFYPW_ALL="1")

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert ["sudo", "-v"] not in calls
    assert calls.index(["sudo", "true"]) < next(
        index for index, call in enumerate(calls) if call[:2] == ["sudo", "install"]
    )
    assert (system_root / "etc/systemd/system/callpilot-edge.service").exists()


def test_install_sudo_auth_failure_reports_guidance_before_system_changes(installer):
    _root, trace, system_root, _bin_dir, run = installer

    result = run(FAIL_TOOL="sudo")

    assert result.returncode != 0
    assert "ERROR: sudo command authentication failed" in result.stderr
    assert "interactive terminal" in result.stderr
    assert not system_root.exists()
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [call for call in calls if call[0] == "sudo"] == [["sudo", "true"]]


@pytest.mark.parametrize("command", ["usermod", "install", "udevadm", "systemctl"])
def test_install_resolves_privileged_commands_only_in_sudo_path(installer, tmp_path, command):
    _root, trace, _system_root, bin_dir, run = installer
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    shutil.move(bin_dir / command, sbin / command)
    assert shutil.which(command, path=str(bin_dir)) is None

    result = run(SUDO_PATH=f"{sbin}:{bin_dir}")

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert any(call[:2] == ["sudo", command] for call in calls)
    assert calls.index(["sudo", "true"]) < calls.index(["sudo", "usermod", "-aG", "dialout", "edge"])
    assert ["usermod", "-aG", "dialout", "edge"] in calls


def test_install_reports_missing_usermod_from_sudo(installer):
    _root, trace, _system_root, bin_dir, run = installer
    (bin_dir / "usermod").unlink()

    result = run()

    assert result.returncode != 0
    assert "sudo: usermod: command not found" in result.stderr
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert ["sudo", "usermod", "-aG", "dialout", "edge"] in calls
    assert ["sudo", "systemctl", "daemon-reload"] not in calls


@pytest.mark.parametrize("overrides", [{"TEST_OS": "Darwin"}, {"TEST_UID": "0"}, {"FAIL_TOOL": "uv"}])
def test_install_preflight_failure_does_not_touch_system(installer, overrides):
    _root, _trace, system_root, _bin_dir, run = installer
    result = run(**overrides)
    assert result.returncode != 0
    assert not system_root.exists()


def _git(repo, *args, input=None):
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Updater execution requires git")
    return subprocess.run(
        [executable, "-C", str(repo), *args], input=input, text=True, capture_output=True,
        check=True, env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                         "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"},
    ).stdout.strip()


@pytest.fixture
def updater(installer, tmp_path):
    root, trace, system, bin_dir, run = installer
    shutil.copy2(ROOT / ".gitignore", root / ".gitignore")
    (root / "data").mkdir()
    (root / "data" / "private.json").write_text('{"keep": true}\n', encoding="utf-8")
    (root / "data" / "number_profiles.example.json").write_text("original seed\n", encoding="utf-8")
    (root / "data" / "call_playbooks.example.json").write_text("original playbook\n", encoding="utf-8")
    (root / "recordings").mkdir()
    (root / "recordings" / "private.wav").write_bytes(b"private audio")
    (root / "tracked.txt").write_text("old code\n", encoding="utf-8")
    _git(root, "init", "-b", "product/physical-sim")
    _git(root, "add", ".")
    tree = _git(root, "write-tree")
    old = _git(root, "commit-tree", tree, input="Initial test snapshot\n")
    _git(root, "update-ref", "HEAD", old)
    remote = tmp_path / "remote"
    _git(tmp_path, "clone", "--bare", str(root), str(remote))
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "fetch", "origin")
    _git(root, "branch", "--set-upstream-to=origin/product/physical-sim")
    blob = _git(remote, "hash-object", "-w", "--stdin", input="new code\n")
    _git(remote, "read-tree", old)
    _git(remote, "update-index", "--add", "--cacheinfo", f"100644,{blob},tracked.txt")
    new_tree = _git(remote, "write-tree")
    new = _git(remote, "commit-tree", new_tree, "-p", old, input="Update test snapshot\n")
    _git(remote, "update-ref", "refs/heads/product/physical-sim", new)

    def update(*args, **overrides):
        return run(*args, script="update.sh", **overrides)

    return root, trace, system, bin_dir, update, old, new, remote


def _calls(trace):
    return [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]


def test_update_non_git_requires_adopt(updater):
    _root, trace, _system, _bin, run, *_ = updater
    result = run(TEST_NON_GIT="1")
    assert result.returncode != 0
    assert "--adopt --branch" in result.stderr
    assert not any("fetch" in call or call[0] == "sudo" for call in _calls(trace))


@pytest.mark.parametrize("staged", [False, True])
def test_update_refuses_tracked_local_changes(updater, staged):
    root, trace, _system, _bin, run, *_ = updater
    (root / "tracked.txt").write_text("local changes\n", encoding="utf-8")
    if staged:
        _git(root, "add", "tracked.txt")
    result = run("--force")
    assert result.returncode != 0
    assert "tracked.txt" in result.stderr
    assert not any("merge" in call or call[0] == "uv" for call in _calls(trace))
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "local changes\n"


def test_update_refuses_non_fast_forward(updater):
    root, trace, _system, _bin, run, old, new, remote = updater
    unrelated = _git(remote, "commit-tree", f"{new}^{{tree}}", input="Unrelated history\n")
    _git(remote, "update-ref", "refs/heads/product/physical-sim", unrelated)
    result = run("--force")
    assert result.returncode != 0
    assert "fast-forward" in result.stderr
    assert _git(root, "rev-parse", "HEAD") == old
    assert not any("merge" in call or call[0] == "uv" for call in _calls(trace))


@pytest.mark.parametrize("status", [
    {"TEST_QUEUE": '{"ok":true,"active":true,"pending":[],"current":null}'},
    {"TEST_QUEUE": '{"ok":true,"active":false,"pending":["test"],"current":null}'},
    {"TEST_QUEUE": '{"ok":true,"active":false,"pending":[],"current":"test"}'},
    {"TEST_REMOTE": '{"ok":true,"active":true}'},
    {"TEST_META": '{"call_active":true,"dial_queue_busy":false}'},
    {"TEST_META": '{"call_active":false,"dial_queue_busy":true}'},
])
@pytest.mark.parametrize("args", [[], ["--force"]])
def test_update_refuses_known_busy_even_with_force(updater, status, args):
    _root, trace, _system, _bin, run, *_ = updater
    result = run(*args, **status)
    assert result.returncode != 0
    assert "busy" in result.stderr
    assert not any("fetch" in call or call[0] == "sudo" for call in _calls(trace))


@pytest.mark.parametrize("overrides", [
    {"TEST_META": "{}"}, {"FAIL_TOOL": "curl"}, {"TEST_META": "invalid json"},
    {"TEST_META": '{"call_active":"false","dial_queue_busy":false}'},
    {"TEST_META_EXIT": "7"},
    {"TEST_META": '{"call_active":false}', "TEST_QUEUE": "invalid json"},
])
def test_update_unverifiable_local_call_requires_force(updater, overrides):
    _root, trace, _system, _bin, run, *_ = updater
    result = run(**overrides)
    assert result.returncode != 0
    assert "--force" in result.stderr
    assert not any("fetch" in call or call[0] == "sudo" for call in _calls(trace))


def test_update_happy_path_order_config_and_sha(updater):
    root, trace, _system, _bin, run, old, new, _remote = updater
    original_env = (root / ".env").read_bytes()
    result = run(TEST_SUDO_VERIFYPW_ALL="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{old} -> {new}" in result.stdout
    assert "WEB_PORT" in result.stdout and "MODEM_PCM_PIN_8K" in result.stdout
    assert "secret-placeholder" not in result.stdout + result.stderr
    assert (root / ".env").read_bytes() == original_env
    assert (root / "data" / "private.json").read_text(encoding="utf-8") == '{"keep": true}\n'
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "new code\n"
    calls = _calls(trace)
    fetch = next(i for i, call in enumerate(calls) if call[0] == "git" and "fetch" in call)
    merge = next(i for i, call in enumerate(calls) if call[0] == "git" and "merge" in call)
    setup = next(i for i, call in enumerate(calls) if call[:2] == ["pip", "install"])
    restart = calls.index(["sudo", "systemctl", "restart", "callpilot-edge.service"])
    health = next(i for i, call in enumerate(calls) if call[0] == "curl" and "--write-out" in call)
    assert fetch < merge < setup < restart < health
    assert calls.index(["sudo", "true"]) < merge
    assert ["sudo", "-v"] not in calls
    assert ["uv", "python", "install", "3.13"] in calls
    assert ["uv", "python", "find", "3.13"] in calls
    assert ["systemctl", "is-active", "--quiet", "callpilot-edge.service"] in calls


@pytest.mark.parametrize("overrides", [
    {"TEST_HEALTH_CODE": "503"}, {"TEST_SERVICE_ACTIVE": "0"},
    {"TEST_HEALTH_EXIT": "18"},
    {"FAIL_TOOL": "systemctl"}, {"FAIL_TOOL": "uv"},
])
def test_update_failure_prints_logs_and_rollback(updater, overrides):
    root, _trace, _system, _bin, run, old, _new, _remote = updater
    result = run("--force", **overrides)
    assert result.returncode != 0
    assert "journalctl -u callpilot-edge.service" in result.stderr
    assert f"git checkout {old}" in result.stderr
    assert "systemctl restart callpilot-edge.service" in result.stderr
    assert not (root / ".callpilot-update.lock").exists()


@pytest.mark.parametrize("seed", ["number_profiles.example.json", "call_playbooks.example.json"])
def test_update_fast_forwards_tracked_data_seed_without_touching_runtime(updater, seed):
    root, _trace, _system, _bin, run, _old, new, remote = updater
    runtime = (root / "data" / "private.json").read_bytes()
    blob = _git(remote, "hash-object", "-w", "--stdin", input="changed seed\n")
    _git(remote, "update-index", "--cacheinfo", f"100644,{blob},data/{seed}")
    tree = _git(remote, "write-tree")
    target = _git(remote, "commit-tree", tree, "-p", new, input="Seed change\n")
    _git(remote, "update-ref", "refs/heads/product/physical-sim", target)
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(root, "rev-parse", "HEAD") == target
    assert (root / "data" / seed).read_text(encoding="utf-8") == "changed seed\n"
    assert (root / "data" / "private.json").read_bytes() == runtime


def test_update_legacy_edge_requires_force_but_can_bootstrap(updater):
    root, _trace, _system, _bin, run, _old, new, _remote = updater
    result = run("--force", TEST_META="{}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unknown" in result.stdout
    assert _git(root, "rev-parse", "HEAD") == new


def test_update_rechecks_busy_before_restart(updater):
    root, trace, _system, _bin, run, _old, new, _remote = updater
    result = run("--force", TEST_BUSY_AFTER="3")
    assert result.returncode != 0
    assert "busy" in result.stderr
    assert _git(root, "rev-parse", "HEAD") == new
    assert not any(call[:3] == ["sudo", "systemctl", "restart"] for call in _calls(trace))


def test_update_explicit_branch_and_quoted_env_port(updater):
    root, trace, _system, _bin, run, _old, new, _remote = updater
    (root / ".env").write_text("export WEB_PORT='47202' # local only\n", encoding="utf-8")
    _git(root, "config", "--unset", "branch.product/physical-sim.remote")
    result = run("--branch", "product/physical-sim", "--force")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(root, "rev-parse", "HEAD") == new
    assert all(call[-1].startswith("http://127.0.0.1:47202/") for call in _calls(trace) if call[0] == "curl")


@pytest.mark.parametrize("relative", ["local.wav", "data/private.json"])
def test_update_does_not_overwrite_ignored_untracked_file(updater, relative):
    root, _trace, _system, _bin, run, old, new, remote = updater
    (root / relative).write_bytes(b"private runtime data")
    blob = _git(remote, "hash-object", "-w", "--stdin", input="remote recording\n")
    _git(remote, "update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}")
    tree = _git(remote, "write-tree")
    target = _git(remote, "commit-tree", tree, "-p", new, input="Ignored collision\n")
    _git(remote, "update-ref", "refs/heads/product/physical-sim", target)
    result = run("--force")
    assert result.returncode != 0
    assert _git(root, "rev-parse", "HEAD") == old
    assert (root / relative).read_bytes() == b"private runtime data"


def test_adopt_backs_up_preserves_runtime_and_can_restore(updater, tmp_path):
    root, trace, _system, _bin, run, _old, new, remote = updater
    shutil.rmtree(root / ".git")
    protected = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    (root / "tracked.txt").write_text("snapshot version\n", encoding="utf-8")
    for seed in ("number_profiles.example.json", "call_playbooks.example.json"):
        (root / "data" / seed).write_text("snapshot seed\n", encoding="utf-8")
    (root / "scripts" / "linux" / "install.sh").unlink()
    result = run("--adopt", "--branch", "product/physical-sim", "--force", TEST_ADOPT_REMOTE=str(remote))
    assert result.returncode == 0, result.stdout + result.stderr
    backups = list((tmp_path / "home").glob("callpilot-adopt-backup-*.tgz"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with tarfile.open(backups[0]) as archive:
        member = archive.extractfile("tracked.txt")
        assert member is not None and member.read() == b"snapshot version\n"
        for seed in ("number_profiles.example.json", "call_playbooks.example.json"):
            member = archive.extractfile(f"data/{seed}")
            assert member is not None and member.read() == b"snapshot seed\n"
        assert "data/private.json" not in archive.getnames()
        assert not any(name.startswith((".venv/", "recordings/")) or name == ".env"
                       for name in archive.getnames())
    for path, content in protected.items():
        if (path in (root / ".env", root / "data" / "private.json")
                or root / ".venv" in path.parents or root / "recordings" in path.parents):
            assert path.read_bytes() == content
    assert (root / "data" / "number_profiles.example.json").read_text(encoding="utf-8") == "original seed\n"
    assert (root / "data" / "call_playbooks.example.json").read_text(encoding="utf-8") == "original playbook\n"
    assert not _git(root, "status", "--porcelain", "--untracked-files=no")
    assert _git(root, "rev-parse", "HEAD") == new
    assert _git(root, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/product/physical-sim"
    assert "run" in result.stdout.lower() and "update.sh" in result.stdout
    assert not any(call[0] in ("sudo", "uv", "pip") for call in _calls(trace))
    with tarfile.open(backups[0]) as archive:
        restore = archive.extractfile("callpilot-adopt-rollback.sh")
        assert restore is not None
        restore_script = restore.read().decode("utf-8")
        metadata_backup = Path(str(backups[0]) + ".git")
        metadata_backup.mkdir()
        refused = subprocess.run(
            ["bash", "-s", "--", str(root), str(backups[0])], input=restore_script,
            text=True, capture_output=True, timeout=30,
        )
        assert refused.returncode != 0
        assert "destination exists" in refused.stderr
        assert (root / ".git").is_dir() and not list(metadata_backup.iterdir())
        metadata_backup.rmdir()
        rollback = subprocess.run(
            ["bash", "-s", "--", str(root), str(backups[0])], input=restore_script,
            text=True, capture_output=True, timeout=30,
        )
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    assert not (root / ".git").exists()
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "snapshot version\n"
    assert not (root / "scripts" / "linux" / "install.sh").exists()
    assert (root / ".env").read_bytes() == protected[root / ".env"]
    for seed in ("number_profiles.example.json", "call_playbooks.example.json"):
        assert (root / "data" / seed).read_text(encoding="utf-8") == "snapshot seed\n"


def test_adopt_old_seeds_then_update_without_dirty_worktree(updater):
    root, _trace, _system, _bin, run, _old, new, remote = updater
    shutil.rmtree(root / ".git")
    (root / "data" / "number_profiles.example.json").write_text("old snapshot seed\n", encoding="utf-8")
    result = run("--adopt", "--branch", "product/physical-sim", "--force",
                 TEST_ADOPT_REMOTE=str(remote), TEST_META="{}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _git(root, "status", "--porcelain", "--untracked-files=no")
    # The still-running Edge is old until this first restart.
    result = run("--force", TEST_META="{}")
    assert result.returncode == 0, result.stdout + result.stderr
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(root, "rev-parse", "HEAD") == new


def test_adopt_checkout_failure_restores_original_snapshot(updater, tmp_path):
    root, _trace, _system, _bin, run, _old, _new, remote = updater
    shutil.rmtree(root / ".git")
    (root / "tracked.txt").write_text("snapshot version\n", encoding="utf-8")
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = run("--adopt", "--branch", "product/physical-sim", "--force",
                 TEST_ADOPT_REMOTE=str(remote), TEST_FAIL_CHECKOUT="1")
    assert result.returncode != 0
    assert "restoring the snapshot" in result.stderr
    assert list((tmp_path / "home").glob("callpilot-adopt-backup-*.tgz"))
    after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after
    assert not (root / ".git").exists()


def test_adopt_refuses_symlink_parent_without_touching_target(updater, tmp_path):
    root, _trace, _system, _bin, run, _old, _new, remote = updater
    shutil.rmtree(root / ".git")
    outside = tmp_path / "outside"
    shutil.move(root / "scripts" / "linux", outside)
    (root / "scripts" / "linux").symlink_to(outside, target_is_directory=True)
    before = {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()}
    result = run("--adopt", "--branch", "product/physical-sim", "--force", TEST_ADOPT_REMOTE=str(remote))
    assert result.returncode != 0
    assert "Unsafe parent" in result.stderr or "ignored paths" in result.stderr
    assert {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()} == before
    assert not (root / ".git").exists()


@pytest.mark.parametrize("args", [[], ["--adopt", "--branch", "product/physical-sim"]])
def test_update_dry_run_has_no_side_effects(updater, tmp_path, args):
    root, trace, system, _bin, run, old, _new, _remote = updater
    if args:
        shutil.rmtree(root / ".git")
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = run(*args, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY RUN" in result.stdout
    after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after
    assert not system.exists() and not (tmp_path / "home").exists()
    assert not (root / ".callpilot-update.lock").exists()
    assert not any(call[0] in ("sudo", "uv", "curl", "pip") or "fetch" in call for call in _calls(trace))
    if not args:
        assert _git(root, "rev-parse", "HEAD") == old
