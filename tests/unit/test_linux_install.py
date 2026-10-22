"""Linux deployment contracts and sandboxed installer execution (no sudo/hardware)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
    for command in ("bash", "dirname", "sed", "mktemp", "install", "basename", "rm", "cp"):
        executable = shutil.which(command)
        assert executable is not None
        (bin_dir / command).symlink_to(executable)
    trace = tmp_path / "trace.jsonl"
    stub = f"""#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ['TRACE'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps([tool, *args]) + '\\n')
if tool == os.environ.get('FAIL_TOOL'):
    sys.exit(1)
if tool == 'uname':
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
    elif args[0] in ('usermod', 'true'):
        os.execv(executable, args)
"""
    for name in ("uname", "id", "getent", "sudo", "true", "uv", "ffmpeg", "systemctl", "udevadm", "usermod"):
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
    }

    def run(*args, **overrides):
        return subprocess.run(
            ["bash", str(root / "scripts" / "linux" / "install.sh"), *args],
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
