"""launchd 常驻方案单测：校验 plist 结构与 install.sh 语法。

plist 是部署机上物化的产物（install.sh 原样 cp，不做占位符替换），其中的
绝对路径属于部署机而非跑测试的 checkout，故路径断言只做「同一安装根」的
自洽性校验，不与本机 PROJECT_ROOT 比对。plist 的目标平台固定是 macOS，
路径一律按 PurePosixPath 解析，让测试在 Windows/Linux CI 上同样可跑。
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path, PurePosixPath

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


def install_root(data: dict) -> PurePosixPath:
    """plist 声明的安装根目录（WorkingDirectory），其余路径都应挂在其下。"""
    root = PurePosixPath(data["WorkingDirectory"])
    assert root.is_absolute(), f"WorkingDirectory 应为绝对路径：{root}"
    return root


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
        if arg.startswith("-"):
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
    data_dir = install_root(data) / "data"
    for key in ("StandardOutPath", "StandardErrorPath"):
        log_path = PurePosixPath(data[key])
        assert log_path.is_absolute(), f"{key} 应为绝对路径：{log_path}"
        assert data_dir in log_path.parents, f"{key} 应位于安装根 data/ 下：{log_path}"


def test_common_keys(labeled_plist):
    _, data = labeled_plist
    install_root(data)  # WorkingDirectory 存在且为绝对路径
    assert data["ThrottleInterval"] == 10
    assert data["LimitLoadToSessionType"] == "Aqua"


def test_all_plists_share_same_install_root():
    roots = {
        label: install_root(load_plist(path)) for label, path in PLIST_SPECS.items()
    }
    assert len(set(roots.values())) == 1, f"两个 plist 的安装根不一致：{roots}"


def test_bridge_program_arguments_exact():
    data = load_plist(PLIST_SPECS["com.agentcall.bridge"])
    root = install_root(data)
    args = data["ProgramArguments"]
    # caffeinate -s 包裹：进程存活期间阻止系统睡眠（USB 掉线风暴的首要诱因）
    assert args[0] == "/usr/bin/caffeinate"
    assert args[1] == "-s"
    assert args[2] == str(root / ".venv/bin/python")
    assert args[3] == str(root / "scripts/ec20_usb_pty.py")
    assert args.count("--map") == 3
    assert "2:/tmp/ec20-at" in args
    assert "1:/tmp/ec20-nmea" in args
    assert "3:/tmp/ec20-modem" in args
    assert "--log-file" in args


def test_app_program_arguments_exact():
    data = load_plist(PLIST_SPECS["com.agentcall.app"])
    root = install_root(data)
    assert data["ProgramArguments"] == [
        "/usr/bin/caffeinate",
        "-s",
        str(root / ".venv/bin/python"),
        str(root / "app.py"),
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
    """launchd 不继承 shell PATH，ffmpeg（Homebrew 安装）必须能被找到。"""
    for label in PLIST_SPECS:
        data = load_plist(PLIST_SPECS[label])
        path = data.get("EnvironmentVariables", {}).get("PATH", "")
        assert "/usr/local/bin" in path and "/opt/homebrew/bin" in path, label
