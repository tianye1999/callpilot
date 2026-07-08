"""CallPilot 菜单栏托盘 App（macOS）。

产品形态：后台服务（app.py，由 launchd 常驻）+ 顶栏一个状态图标。
比整窗 App 更贴合本项目「服务常驻、随手查看」的真实用法——关掉面板窗口
不影响接打电话，托盘图标始终在顶栏显示服务状态、一键打开面板/重启/退出。

菜单：
    ● CallPilot            （标题，图标随服务状态：🟢 在线 / 🔴 离线）
    打开控制台             （开 pywebview 面板窗口，即 desktop_app.py 子进程）
    重启服务               （POST /api/restart，原地重启后端）
    ——
    退出                   （仅退出托盘；后端服务仍由 launchd 常驻）

用法：
    .venv/bin/python tray_app.py

依赖 rumps（macOS 专属，pip install 'callpilot[mac]' 或 .[dev]）；
纯逻辑（状态探测/标签/动作）抽为可测函数，rumps 胶水层薄且惰性导入。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WEB_URL = "http://127.0.0.1:47100"


def icon_path(online: bool) -> str:
    """菜单栏图标文件路径：品牌手柄（在线磷光绿 / 离线灰）。

    打包（frozen）时图标随 _MEIPASS 内嵌（spec datas 收进 menubar/）；
    源码运行时在 packaging/menubar/ 下。
    """
    name = "menubar_on.png" if online else "menubar_off.png"
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return str(Path(base) / "menubar" / name)
    return str(PROJECT_ROOT / "packaging" / "menubar" / name)

_STATUS_LABELS = {
    "en": {"online": "Service: running", "offline": "Service: stopped"},
    "zh": {"online": "服务：运行中", "offline": "服务：已停止"},
}
_MENU_LABELS = {
    "en": {
        "open": "Open dashboard",
        "restart": "Restart service",
        "uninstall": "Uninstall background services",
        "quit": "Quit",
    },
    "zh": {
        "open": "打开控制台",
        "restart": "重启服务",
        "uninstall": "卸载常驻",
        "quit": "退出",
    },
}


# ---- 纯逻辑（可单测，不碰 rumps/GUI）----

def web_url() -> str:
    """服务地址：AGENTCALL_WEB_URL 覆盖，否则默认端口。"""
    return os.getenv("AGENTCALL_WEB_URL", DEFAULT_WEB_URL).rstrip("/")


def probe_online(url: str, timeout: float = 2.0) -> bool:
    """探测 ``<url>/api/meta`` 是否 2xx（服务是否在线）。任何错误视为离线。"""
    try:
        with urllib.request.urlopen(f"{url}/api/meta", timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def status_label(online: bool, lang: str = "zh") -> str:
    """服务状态菜单项文案。"""
    lang = "en" if lang == "en" else "zh"
    return _STATUS_LABELS[lang]["online" if online else "offline"]


def menu_label(key: str, lang: str = "zh") -> str:
    """动作菜单项文案（open/restart/quit）。"""
    lang = "en" if lang == "en" else "zh"
    return _MENU_LABELS[lang][key]


def request_restart(url: str, timeout: float = 4.0) -> bool:
    """POST ``<url>/api/restart`` 请求后端原地重启；网络错误返回 False。"""
    req = urllib.request.Request(
        f"{url}/api/restart", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


def _resources_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    exe = Path(sys.executable).resolve()
    parts = exe.parts
    for index, part in enumerate(parts):
        if part.endswith(".app"):
            return Path(*parts[: index + 1]) / "Contents" / "Resources"
    return PROJECT_ROOT


def _prepend_runtime_paths() -> None:
    resources = _resources_dir()
    bin_dir = resources / "bin"
    lib_dir = resources / "lib"
    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    os.environ.setdefault("DYLD_LIBRARY_PATH", str(lib_dir))


def _ensure_launchd_installed():
    if not _is_frozen() or sys.platform != "darwin":
        from agentcall.macos_launchd import LaunchdInstallResult

        return LaunchdInstallResult(changed=[], failures=[], warnings=[])
    from agentcall import macos_launchd

    layout = macos_launchd.make_layout(sys.executable, resources_dir=_resources_dir())
    return macos_launchd.install_launch_agents(layout)


def _uninstall_launchd() -> list[str]:
    from agentcall import macos_launchd

    layout = macos_launchd.make_layout(sys.executable, resources_dir=_resources_dir())
    return macos_launchd.uninstall_launch_agents(layout)


def _run_service() -> None:
    _prepend_runtime_paths()
    import app

    app.main()


def _run_bridge(argv: list[str]) -> int:
    _prepend_runtime_paths()
    from scripts import ec20_usb_pty

    original_argv = sys.argv[:]
    try:
        sys.argv = [original_argv[0], *argv]
        return ec20_usb_pty.main()
    finally:
        sys.argv = original_argv


def dashboard_command(python_exe: str | None = None, frozen: bool | None = None) -> list[str]:
    """构造打开面板窗口的子进程命令（pywebview 窗口，单独进程避免与 rumps
    抢占 Cocoa 主线程 runloop）。

    - 打包后（frozen）：复用本可执行文件 + ``--window`` 分支（无独立 python）；
    - 源码运行：``python desktop_app.py``。
    """
    if frozen is None:
        frozen = _is_frozen()
    if frozen:
        return [sys.executable, "--window"]
    py = python_exe or sys.executable
    return [py, str(PROJECT_ROOT / "desktop_app.py")]


# ---- rumps 菜单栏胶水（薄层，惰性导入，仅 macOS）----

def _import_rumps():
    try:
        import rumps
    except ImportError:
        print(
            "未安装 rumps，无法启动菜单栏 App（仅 macOS）。\n"
            "请先安装: .venv/bin/pip install rumps\n"
            f"或直接用浏览器访问 {web_url()}",
            file=sys.stderr,
        )
        sys.exit(1)
    return rumps


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _prepend_runtime_paths()
    if "--service" in sys.argv[1:]:
        _run_service()
        return
    if "--bridge" in sys.argv[1:]:
        args = [arg for arg in sys.argv[1:] if arg != "--bridge"]
        raise SystemExit(_run_bridge(args))
    # 打包后「打开控制台」复用本可执行文件 + --window 分支拉起 pywebview 面板窗口
    # （避免 pywebview 与 rumps 抢占 Cocoa 主线程 runloop）。
    if "--window" in sys.argv[1:]:
        import desktop_app
        desktop_app.main()
        return
    rumps = _import_rumps()
    lang = "en" if os.getenv("AGENT_LANGUAGE", "zh").strip().lower() == "en" else "zh"
    try:
        install_result = _ensure_launchd_installed()
        if install_result.changed:
            logger.info("launchd agents installed/updated: %s", ", ".join(install_result.changed))
        if install_result.failures:
            summary = install_result.failure_summary()
            logger.error("launchd 自装失败: %s", summary)
            rumps.notification("CallPilot", "Background services failed", summary)
    except Exception as exc:  # noqa: BLE001
        logger.error("launchd 自装失败: %s", exc)
        rumps.notification("CallPilot", "Background services failed", str(exc))

    class CallPilotTray(rumps.App):
        def __init__(self) -> None:
            # 图标模式（非 template，保留品牌绿）；title 留空只显图标。
            super().__init__(
                "CallPilot", title=None, icon=icon_path(False),
                template=False, quit_button=None,
            )
            self._status_item = rumps.MenuItem(status_label(False, lang))
            self.menu = [
                self._status_item,
                None,
                rumps.MenuItem(menu_label("open", lang), callback=self._open),
                rumps.MenuItem(menu_label("restart", lang), callback=self._restart),
                rumps.MenuItem(menu_label("uninstall", lang), callback=self._uninstall),
                None,
                rumps.MenuItem(menu_label("quit", lang), callback=self._quit),
            ]
            self._refresh(None)

        @rumps.timer(5)
        def _refresh(self, _sender) -> None:
            online = probe_online(web_url())
            self.icon = icon_path(online)
            self._status_item.title = status_label(online, lang)

        def _open(self, _sender) -> None:
            try:
                subprocess.Popen(dashboard_command(), cwd=str(PROJECT_ROOT))
            except OSError as exc:
                logger.error("打开控制台失败: %s", exc)

        def _restart(self, _sender) -> None:
            request_restart(web_url())

        def _uninstall(self, _sender) -> None:
            try:
                removed = _uninstall_launchd()
                logger.info("launchd agents removed: %s", ", ".join(removed) or "none")
            except Exception as exc:  # noqa: BLE001
                logger.error("卸载常驻失败: %s", exc)

        def _quit(self, _sender) -> None:
            rumps.quit_application()

    CallPilotTray().run()


if __name__ == "__main__":
    main()
