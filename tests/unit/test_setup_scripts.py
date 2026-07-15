"""一键 setup 脚本静态检查（scripts/setup.sh 与 scripts/windows/setup.ps1）。

setup 脚本的运行时行为（建 venv、装依赖）无法在单测里执行，这里做静态断言：
- 文件存在、bash 语法（bash -n）、ps1 带 UTF-8 BOM（PS 5.1 兼容）；
- 无硬编码个人路径；
- setup.sh 实际执行的 pip install 不得硬编码镜像源（面向全球的开源项目），
  但失败时必须有「可用 -i 镜像重试」的提示行；
- README 快速开始段（中英双语）均以一键 setup 为首选路径。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = PROJECT_ROOT / "scripts" / "setup.sh"
SETUP_PS1 = PROJECT_ROOT / "scripts" / "windows" / "setup.ps1"
README = PROJECT_ROOT / "README.md"


@pytest.mark.parametrize("path", [SETUP_SH, SETUP_PS1], ids=lambda p: p.name)
def test_files_exist(path):
    assert path.is_file(), f"缺少文件：{path}"


@pytest.mark.parametrize("path", [SETUP_SH, SETUP_PS1], ids=lambda p: p.name)
def test_no_hardcoded_user_paths(path):
    text = path.read_text(encoding="utf-8")
    assert "/Users/" not in text, f"{path.name} 含硬编码 macOS 用户路径"
    assert "C:\\Users\\" not in text, f"{path.name} 含硬编码 Windows 用户路径"


# ---- setup.sh ----

def test_setup_sh_bash_syntax():
    bash = shutil.which("bash")
    if bash is None:
        # GitHub windows-latest 自带 Git Bash 会走到检查；裸 Windows 才跳过。
        pytest.skip("宿主无 bash，无法做 setup.sh 语法检查")
    # as_posix：Windows 反斜杠路径交给 bash 易被当转义处理，统一正斜杠。
    result = subprocess.run(
        [bash, "-n", SETUP_SH.as_posix()],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"setup.sh 语法错误：{result.stderr}"


def test_setup_sh_strict_mode_and_derived_root():
    text = SETUP_SH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "BASH_SOURCE" in text, "项目根应从脚本位置推导"


def test_setup_sh_no_hardcoded_pip_mirror():
    """实际执行的 pip install 行不得带 -i/--index-url（提示行里的镜像建议除外）。"""
    text = SETUP_SH.read_text(encoding="utf-8")
    exec_lines = [
        line
        for line in text.splitlines()
        if "pip install" in line
        and not line.lstrip().startswith("#")
        and "echo" not in line  # 失败提示行（echo 输出）不算执行
        and "info" not in line and "warn" not in line  # 日志输出行不算执行
    ]
    assert exec_lines, "setup.sh 应包含实际执行的 pip install"
    for line in exec_lines:
        assert " -i " not in line, f"pip install 硬编码了镜像源：{line.strip()}"
        assert "--index-url" not in line, f"pip install 硬编码了镜像源：{line.strip()}"
    # 失败时的镜像重试建议必须存在（一行 echo 提示）
    assert " -i https://" in text, "缺少「pip 失败可用 -i 镜像重试」的提示行"


def test_setup_sh_covers_essentials():
    text = SETUP_SH.read_text(encoding="utf-8")
    assert "(3, 12)" in text, "应检查 Python >= 3.12"
    assert "ffmpeg" in text, "应检查 ffmpeg 在 PATH"
    assert "brew install ffmpeg" in text, "macOS 缺 ffmpeg 应给 brew 安装命令"
    assert "apt install ffmpeg" in text, "Linux 缺 ffmpeg 应给 apt 安装命令"
    assert ".env.example" in text and ".env" in text, "应复制 .env.example → .env"
    assert "OPENAI_API_KEY" in text, "下一步应提示切换到 OpenAI 所需的 API key"
    assert "AGENT_PROVIDER=openai" in text, "下一步应说明如何切换到 OpenAI"
    assert "DASHSCOPE_API_KEY" in text, "下一步应提示默认 Qwen 所需的 API key"
    assert "ec20_usb_pty.py" in text, "下一步应提示（仅 mac）启动 USB 桥"
    assert "app.py" in text, "下一步应提示启动服务"


# ---- setup.ps1 ----

def test_setup_ps1_has_utf8_bom():
    """PowerShell 5.1 对无 BOM 脚本按 ANSI 代码页解码，中文字面量全部乱码。"""
    raw = SETUP_PS1.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "setup.ps1 缺 UTF-8 BOM"


def test_setup_ps1_covers_essentials():
    text = SETUP_PS1.read_text(encoding="utf-8")
    assert "(3, 12)" in text, "应检查 Python >= 3.12"
    assert "ffmpeg" in text, "应检查 ffmpeg 在 PATH"
    assert ".env.example" in text, "应复制 .env.example → .env"
    assert "OPENAI_API_KEY" in text, "下一步应提示切换到 OpenAI 所需的 API key"
    assert "AGENT_PROVIDER=openai" in text, "下一步应说明如何切换到 OpenAI"
    assert "DASHSCOPE_API_KEY" in text, "下一步应提示默认 Qwen 所需的 API key"
    assert "Quectel" in text, "应提示安装 Quectel 官方驱动"
    assert "MODEM_PORT=auto" in text, "应提示 Windows 用 MODEM_PORT=auto"
    assert "ec20_usb_pty" not in text, "Windows 无需 USB→PTY 桥"
    assert "$PSScriptRoot" in text, "项目根应从脚本位置推导"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="本机无 PowerShell（pwsh）")
def test_setup_ps1_syntax_parses():
    check = (
        "$errs = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SETUP_PS1}', [ref]$null, [ref]$errs) | Out-Null; "
        "if ($errs) { $errs | Out-String | Write-Host; exit 1 }"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", check],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"setup.ps1 语法错误：{result.stdout}{result.stderr}"


# ---- README.md ----

def test_readme_quickstart_uses_setup_scripts():
    """中英双语的快速开始都应以一键 setup 为首选路径。"""
    text = README.read_text(encoding="utf-8")
    assert text.count("bash scripts/setup.sh") >= 2, "英/中 Quick start 均应给出 setup.sh"
    assert text.count("scripts\\windows\\setup.ps1") >= 2, "英/中 Windows 段均应给出 setup.ps1"
