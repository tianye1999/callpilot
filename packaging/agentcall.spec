# -*- mode: python ; coding: utf-8 -*-
"""CallPilot 桌面壳打包配置（薄前端窗口，参考 poc packaging/agent_for_call.spec）。

只打包 desktop_app.py + pywebview 运行时；服务本体仍在仓库 venv 里跑，
项目根位置经内嵌 project_root.txt 传递（见 desktop_app._resolve_project_root）。

产物按构建机平台而异（PyInstaller 不做交叉编译，构建机平台 = 目标平台）：
    macOS   → dist/CallPilot.app（scripts/build_app.sh）
    Windows → dist/CallPilot/CallPilot.exe（scripts/windows/build_app.ps1，待硬件验证）
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

project_root = Path(os.environ["AGENTCALL_BUILD_ROOT"]).resolve()
project_root_file = Path(os.environ["AGENTCALL_BUILD_ROOT_FILE"]).resolve()

# 平台判断统一走 platforms 模块；agentcall 在 src/ 下，构建时不要求已 pip 安装
sys.path.insert(0, str(project_root / "src"))
from agentcall.platforms import IS_MACOS, IS_WINDOWS

datas = [(str(project_root_file), ".")]
binaries = []
# desktop_app 运行时经 sys.path 动态加载 agentcall.platforms，显式声明保底
hiddenimports = ["webview", "agentcall.platforms"]

# pywebview 的平台后端不同，按平台收集对应运行时
if IS_MACOS:
    # mac 后端 = cocoa（pyobjc 系列）
    hiddenimports += ["webview.platforms.cocoa", "objc", "Foundation", "AppKit", "WebKit"]
    gui_packages = ("webview", "objc", "Foundation", "AppKit", "WebKit")
elif IS_WINDOWS:
    # Windows 后端 = WinForms + EdgeChromium(WebView2)，经 pythonnet 的 clr 加载；
    # pythonnet/clr 的打包细节由 pyinstaller-hooks-contrib 兜底。【待硬件验证】
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr"]
    gui_packages = ("webview",)
else:
    gui_packages = ("webview",)

for package_name in gui_packages:
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package_name)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
    hiddenimports += collect_submodules(package_name)

a = Analysis(
    [str(project_root / "desktop_app.py")],
    pathex=[str(project_root), str(project_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        # 服务端依赖不进 App（App 只是窗口，服务在仓库 venv 里跑）
        "aiohttp", "dashscope", "numpy", "serial", "sounddevice",
        "usb", "websockets", "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Windows 图标需 .ico 格式（现仅有 mac 的 .icns），暂用 PyInstaller 默认图标
icon_path = str(project_root / "packaging" / "CallPilot.icns") if IS_MACOS else None

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="CallPilot",
    console=False,
    icon=icon_path,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CallPilot")

# .app BUNDLE 仅 macOS 有意义；其余平台产物即 COLLECT 目录
if IS_MACOS:
    app = BUNDLE(
        coll,
        name="CallPilot.app",
        icon=str(project_root / "packaging" / "CallPilot.icns"),
        bundle_identifier="ai.bondings.callpilot",
        info_plist={
            "CFBundleDisplayName": "CallPilot",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
        },
    )
