"""legacy launchd template checks plus install.sh syntax."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAUNCHD_DIR = PROJECT_ROOT / "scripts" / "launchd"

PLIST_SPECS = {
    "com.agentcall.bridge": LAUNCHD_DIR / "com.agentcall.bridge.plist",
    "com.agentcall.app": LAUNCHD_DIR / "com.agentcall.app.plist",
}


def load_plist(path: Path) -> dict:
    with path.open("rb") as f:
        return plistlib.load(f)


@pytest.fixture(params=sorted(PLIST_SPECS.items()), ids=lambda p: p[0])
def labeled_plist(request):
    label, path = request.param
    assert path.is_file(), f"缺少 plist 文件：{path}"
    return label, load_plist(path)


def test_label_matches_filename(labeled_plist):
    label, data = labeled_plist
    assert data["Label"] == label


def test_program_arguments_use_bundled_entrypoint(labeled_plist):
    label, data = labeled_plist
    args = data["ProgramArguments"]
    assert args[:3] == [
        "/usr/bin/caffeinate",
        "-s",
        "/Applications/CallPilot.app/Contents/MacOS/CallPilot",
    ]
    expected_mode = "--bridge" if label.endswith(".bridge") else "--service"
    assert args[3] == expected_mode


def test_keepalive_and_runatload_true(labeled_plist):
    _, data = labeled_plist
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True


def test_template_points_writes_to_application_support(labeled_plist):
    _, data = labeled_plist
    assert "Application Support/CallPilot" in data["WorkingDirectory"]
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert "Application Support/CallPilot/logs" in data[key]
    env = data["EnvironmentVariables"]
    assert "Application Support/CallPilot/.env" in env["AGENTCALL_ENV_FILE"]
    assert "Application Support/CallPilot/data" in env["AGENTCALL_DATA_DIR"]


def test_common_keys(labeled_plist):
    _, data = labeled_plist
    assert data["ThrottleInterval"] == 10
    assert data["LimitLoadToSessionType"] == "Aqua"


def test_templates_do_not_embed_local_checkout_path():
    for path in PLIST_SPECS.values():
        text = path.read_text(encoding="utf-8")
        assert "/Users/example/" not in text
        assert "/Users/" not in text


def test_bridge_program_arguments_exact():
    data = load_plist(PLIST_SPECS["com.agentcall.bridge"])
    args = data["ProgramArguments"]
    assert args.count("--map") == 3
    assert "2:/tmp/ec20-at" in args
    assert "1:/tmp/ec20-nmea" in args
    assert "3:/tmp/ec20-modem" in args
    assert "--log-file" in args


def test_app_program_arguments_exact():
    data = load_plist(PLIST_SPECS["com.agentcall.app"])
    assert data["ProgramArguments"] == [
        "/usr/bin/caffeinate",
        "-s",
        "/Applications/CallPilot.app/Contents/MacOS/CallPilot",
        "--service",
    ]


def test_install_sh_syntax():
    script = LAUNCHD_DIR / "install.sh"
    assert script.is_file(), f"缺少安装脚本：{script}"
    bash = shutil.which("bash")
    if bash is None:
        # GitHub windows-latest 自带 Git Bash 会走到检查；裸 Windows 才跳过。
        pytest.skip("宿主无 bash，无法做 install.sh 语法检查")
    # as_posix：Windows 反斜杠路径交给 bash 易被当转义处理，统一正斜杠。
    result = subprocess.run(
        [bash, "-n", script.as_posix()],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"install.sh 语法错误：{result.stderr}"


def test_plists_provide_path_with_homebrew():
    """The reference template keeps system paths after the bundled bin path."""
    for label in PLIST_SPECS:
        data = load_plist(PLIST_SPECS[label])
        path = data.get("EnvironmentVariables", {}).get("PATH", "")
        assert "/usr/local/bin" in path and "/opt/homebrew/bin" in path, label
