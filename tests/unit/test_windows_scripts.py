"""Windows 部署脚本静态检查。

PowerShell 在 macOS 开发机上通常不可执行，这里做两层校验：
- 静态断言：文件存在、四个子命令齐全、Task Scheduler 关键调用在、无硬编码用户路径；
- 若本机装了 pwsh，则额外跑官方 Parser 做语法检查（没装则跳过）。
脚本的运行时行为标注「待硬件验证」，见 scripts/windows/README.md。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_DIR = PROJECT_ROOT / "scripts" / "windows"
INSTALL_PS1 = WINDOWS_DIR / "install.ps1"
BUILD_PS1 = WINDOWS_DIR / "build_app.ps1"
README = WINDOWS_DIR / "README.md"
SPEC = PROJECT_ROOT / "packaging" / "agentcall.spec"

ALL_FILES = [INSTALL_PS1, BUILD_PS1, README]


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_files_exist(path):
    assert path.is_file(), f"缺少文件：{path}"


@pytest.mark.parametrize("path", ALL_FILES + [SPEC], ids=lambda p: p.name)
def test_no_hardcoded_user_paths(path):
    text = path.read_text(encoding="utf-8")
    # GitHub 仓库 URL（github.com/<owner>/…）是合法引用，只禁本地用户目录
    assert "/Users/" not in text, f"{path.name} 含硬编码 macOS 用户路径"
    assert "C:\\Users\\" not in text, f"{path.name} 含硬编码 Windows 用户路径"


# ---- install.ps1 ----

def test_install_ps1_has_four_subcommands():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    for cmd in ("install", "uninstall", "status", "restart"):
        assert f'"{cmd}"' in text, f"install.ps1 缺 {cmd} 分支"


def test_install_ps1_uses_task_scheduler_with_restart():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "Register-ScheduledTask" in text
    assert "Unregister-ScheduledTask" in text
    assert "-AtLogOn" in text, "应为登录触发（LogonTrigger）"
    assert "-RestartCount" in text, "应配置失败自动重启"
    assert "ExecutionTimeLimit" in text, "应关掉计划任务默认 72 小时强杀"


def test_install_ps1_paths_derived_not_hardcoded():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "$PSScriptRoot" in text, "项目根应从脚本位置推导"
    assert "Scripts\\python.exe" in text, "venv 解释器应为 Scripts\\python.exe"


def test_install_ps1_no_usb_bridge():
    """Windows 有官方驱动，不需要 macOS 的 USB→PTY 桥任务。"""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "ec20_usb_pty" not in text


# ---- build_app.ps1 ----

def test_build_ps1_invokes_pyinstaller_with_spec():
    text = BUILD_PS1.read_text(encoding="utf-8")
    assert "PyInstaller" in text
    assert "agentcall.spec" in text
    assert "$PSScriptRoot" in text
    # spec 依赖这两个环境变量定位项目根（与 build_app.sh 同约定）
    assert "AGENTCALL_BUILD_ROOT" in text
    assert "AGENTCALL_BUILD_ROOT_FILE" in text


# ---- README.md ----

def test_readme_covers_deploy_essentials():
    text = README.read_text(encoding="utf-8")
    assert "Quectel" in text, "应提示装官方驱动"
    assert "auto" in text, "应说明 MODEM_PORT=auto 自动扫 COM 口"
    assert "install.ps1" in text
    assert "待硬件验证" in text, "未经真机验证必须明示"


# ---- packaging/agentcall.spec 平台条件化 ----

def test_spec_is_valid_python():
    """spec 无法脱离 PyInstaller 执行，但至少语法必须合法。"""
    source = SPEC.read_text(encoding="utf-8")
    compile(source, str(SPEC), "exec")


def test_spec_bundle_only_on_macos():
    text = SPEC.read_text(encoding="utf-8")
    assert "from agentcall.platforms import" in text, "平台判断应经 platforms 模块"
    # BUNDLE(.app) 必须包在 IS_MACOS 分支里（缩进 4 空格），Windows 构建不执行
    assert "if IS_MACOS:\n    app = BUNDLE(" in text


# ---- pwsh 语法检查（本机有 pwsh 才跑）----

@pytest.mark.skipif(shutil.which("pwsh") is None, reason="本机无 PowerShell（pwsh）")
@pytest.mark.parametrize("script", [INSTALL_PS1, BUILD_PS1], ids=lambda p: p.name)
def test_ps1_syntax_parses(script):
    check = (
        "$errs = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$errs) | Out-Null; "
        "if ($errs) { $errs | Out-String | Write-Host; exit 1 }"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", check],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{script.name} 语法错误：{result.stdout}{result.stderr}"


def test_ps1_files_have_utf8_bom():
    """PowerShell 5.1 对无 BOM 脚本按 ANSI 代码页解码，中文字面量全部乱码。"""
    for name in ("install.ps1", "build_app.ps1"):
        raw = (WINDOWS_DIR / name).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{name} 缺 UTF-8 BOM"
