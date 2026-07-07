"""launchd 常驻方案单测：校验 plist 结构与 install.sh 语法。"""

from __future__ import annotations

import plistlib
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


def test_program_arguments_all_absolute(labeled_plist):
    _, data = labeled_plist
    args = data["ProgramArguments"]
    assert args, "ProgramArguments 不能为空"
    # 可执行文件与脚本路径必须是绝对路径；--map 的值形如 IFACE:LINK，
    # --log-file 的值也应是绝对路径。这里逐项检查：flag 本身跳过，
    # IFACE:LINK 检查 LINK 部分，其余一律要求绝对路径。
    for arg in args:
        if arg.startswith("--"):
            continue
        if ":" in arg and not arg.startswith("/"):
            _, link = arg.split(":", 1)
            assert link.startswith("/"), f"--map LINK 应为绝对路径：{arg}"
        else:
            assert arg.startswith("/"), f"应为绝对路径：{arg}"


def test_keepalive_and_runatload_true(labeled_plist):
    _, data = labeled_plist
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True


def test_log_paths_under_data_dir(labeled_plist):
    _, data = labeled_plist
    data_dir = PROJECT_ROOT / "data"
    for key in ("StandardOutPath", "StandardErrorPath"):
        log_path = Path(data[key])
        assert log_path.is_absolute(), f"{key} 应为绝对路径：{log_path}"
        assert data_dir in log_path.parents, f"{key} 应位于 data/ 下：{log_path}"


def test_common_keys(labeled_plist):
    _, data = labeled_plist
    assert data["WorkingDirectory"] == str(PROJECT_ROOT)
    assert data["ThrottleInterval"] == 10
    assert data["LimitLoadToSessionType"] == "Aqua"


def test_bridge_program_arguments_exact():
    data = load_plist(PLIST_SPECS["com.agentcall.bridge"])
    args = data["ProgramArguments"]
    assert args[0] == str(PROJECT_ROOT / ".venv" / "bin" / "python")
    assert args[1] == str(PROJECT_ROOT / "scripts" / "ec20_usb_pty.py")
    assert args.count("--map") == 3
    assert "2:/tmp/ec20-at" in args
    assert "1:/tmp/ec20-nmea" in args
    assert "3:/tmp/ec20-modem" in args
    assert "--log-file" in args


def test_app_program_arguments_exact():
    data = load_plist(PLIST_SPECS["com.agentcall.app"])
    args = data["ProgramArguments"]
    assert args == [
        str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        str(PROJECT_ROOT / "app.py"),
    ]


def test_install_sh_syntax():
    script = LAUNCHD_DIR / "install.sh"
    assert script.is_file(), f"缺少安装脚本：{script}"
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"install.sh 语法错误：{result.stderr}"
