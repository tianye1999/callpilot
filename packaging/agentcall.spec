# -*- mode: python ; coding: utf-8 -*-
"""AgentCall.app 打包配置（薄前端窗口，参考 poc packaging/agent_for_call.spec）。

只打包 desktop_app.py + pywebview/Cocoa 运行时；服务本体仍在仓库 venv 里跑，
项目根位置经内嵌 project_root.txt 传递（见 desktop_app._resolve_project_root）。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

project_root = Path(os.environ["AGENTCALL_BUILD_ROOT"]).resolve()
project_root_file = Path(os.environ["AGENTCALL_BUILD_ROOT_FILE"]).resolve()

datas = [(str(project_root_file), ".")]
binaries = []
hiddenimports = [
    "webview",
    "webview.platforms.cocoa",
    "objc",
    "Foundation",
    "AppKit",
    "WebKit",
]

for package_name in ("webview", "objc", "Foundation", "AppKit", "WebKit"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package_name)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
    hiddenimports += collect_submodules(package_name)

a = Analysis(
    [str(project_root / "desktop_app.py")],
    pathex=[str(project_root)],
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

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="CallPilot",
    console=False,
    icon=str(project_root / "packaging" / "CallPilot.icns"),
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CallPilot")

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
