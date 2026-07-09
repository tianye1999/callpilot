# -*- mode: python ; coding: utf-8 -*-
"""CallPilot standalone macOS app packaging configuration.

产物按构建机平台而异（PyInstaller 不做交叉编译，构建机平台 = 目标平台）：
    macOS   → dist/CallPilot.app（packaging/build_installer.sh）
    Windows → dist/CallPilot/CallPilot.exe（scripts/windows/build_app.ps1，待硬件验证）
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

project_root = Path(os.environ["AGENTCALL_BUILD_ROOT"]).resolve()

# 平台判断统一走 platforms 模块；agentcall 在 src/ 下，构建时不要求已 pip 安装
sys.path.insert(0, str(project_root / "src"))
from agentcall.platforms import IS_MACOS, IS_WINDOWS

datas = []
binaries = []
hiddenimports = [
    "webview",
    "app",
    "desktop_app",
    "scripts.ec20_usb_pty",
    "usb.backend.libusb1",
]
hiddenimports += collect_submodules("agentcall")
datas += collect_data_files("agentcall")

# 菜单栏图标资源随包内嵌（tray_app.icon_path 经 _MEIPASS/menubar 解析）
_menubar_dir = project_root / "packaging" / "menubar"
if _menubar_dir.is_dir():
    for _png in _menubar_dir.glob("*.png"):
        datas.append((str(_png), "menubar"))

_seed_profiles = project_root / "data" / "number_profiles.example.json"
if _seed_profiles.is_file():
    datas.append((str(_seed_profiles), "seed"))

ffmpeg_path = os.environ.get("AGENTCALL_FFMPEG_PATH", "").strip()
if ffmpeg_path:
    datas.append((ffmpeg_path, "bin"))

libusb_path = os.environ.get("AGENTCALL_LIBUSB_PATH", "").strip()
if libusb_path:
    datas.append((libusb_path, "lib"))

# pywebview 的平台后端不同，按平台收集对应运行时
if IS_MACOS:
    # mac 后端 = cocoa（pyobjc 系列）+ rumps 菜单栏
    hiddenimports += ["webview.platforms.cocoa", "objc", "Foundation", "AppKit", "WebKit", "rumps"]
    gui_packages = ("webview", "objc", "Foundation", "AppKit", "WebKit", "rumps")
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
    # macOS 入口 = 菜单栏托盘 App（tray_app 惰性拉起 desktop_app 的面板窗口）；
    # 其余平台仍以窗口 desktop_app 为入口（无菜单栏概念）。
    [str(project_root / ("tray_app.py" if IS_MACOS else "desktop_app.py"))],
    pathex=[str(project_root), str(project_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest"],
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
            "CFBundleShortVersionString": "0.4.1",
            "NSMicrophoneUsageDescription": "CallPilot 需要采集通话对方的语音，供 AI 实时应答。",
            "NSHighResolutionCapable": True,
            # 菜单栏 App：不在 Dock 显示图标、无主窗口（LSUIElement）
            "LSUIElement": True,
        },
    )
