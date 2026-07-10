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

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是合法数字，使用默认值 %s", name, raw, default)
        return default


def probe_online(url: str, timeout: float = 2.0) -> bool:
    """探测 ``<url>/api/meta`` 是否 2xx（服务是否在线）。任何错误视为离线。"""
    try:
        with urllib.request.urlopen(f"{url}/api/meta", timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def fetch_setup_required(url: str, timeout: float = 2.0) -> bool | None:
    """读取 ``<url>/api/meta`` 的 setup_required；探测不到时返回 None。"""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/meta", timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or "setup_required" not in data:
        return None
    return bool(data["setup_required"])


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
    # tray 单元由本进程自装：声明 no_restart 防止 bootout 杀掉自己（见函数 docstring）。
    return macos_launchd.install_launch_agents(
        layout, no_restart_labels={macos_launchd.TRAY_LABEL}
    )


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


def maybe_autoopen_dashboard(
    url: str,
    *,
    wait: float,
    poll_interval: float,
    probe=probe_online,
    fetch_setup_required_func=fetch_setup_required,
    popen=subprocess.Popen,
    dashboard_cmd_factory=dashboard_command,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> bool:
    """首次未配置时自动打开一次控制台窗口；返回是否真的触发开窗。"""
    deadline = monotonic() + max(wait, 0.0)
    while True:
        if probe(url):
            setup_required = fetch_setup_required_func(url)
            if setup_required is not True:
                return False
            try:
                popen(dashboard_cmd_factory(), cwd=str(PROJECT_ROOT))
            except OSError as exc:
                logger.error("自动打开控制台失败: %s", exc)
                return False
            return True

        now = monotonic()
        if now >= deadline:
            return False
        sleep_for = min(max(poll_interval, 0.0), deadline - now)
        if sleep_for <= 0:
            return False
        sleep(sleep_for)


# ---- rumps 菜单栏胶水（薄层，惰性导入，仅 macOS）----

def _acquire_tray_singleton_lock(lock_path: Path | None = None) -> "IO[str] | object | None":
    """菜单栏单例文件锁：返回持锁句柄；已有实例在跑时返回 None（调用方让位退出 0）。

    launchd 常驻实例（com.agentcall.tray）与用户双击/LaunchServices 拉起的实例
    可能并存——靠 flock 让后来者主动让位；配合 tray launchd 单元的
    ``KeepAlive={"SuccessfulExit": False}``，让位退出（码 0）不会被反复拉起。
    句柄须由调用方持有到进程结束，锁随进程退出自动释放（含崩溃）。
    非 POSIX 平台无 fcntl：跳过单例（tray 形态本就 macOS-only），返回占位对象。
    """
    try:
        import fcntl
    except ImportError:
        return object()
    if lock_path is None:
        lock_path = Path(tempfile.gettempdir()) / f"callpilot-tray-{os.getuid()}.lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


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
    # 单例：launchd 常驻实例已在跑时，双击再启的实例让位退出（码 0，见函数注释）。
    # 句柄须持有到进程结束，锁才不失效。
    singleton_lock = _acquire_tray_singleton_lock()
    if singleton_lock is None:
        logger.info("已有 CallPilot 菜单栏实例在运行，本实例让位退出")
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

    maybe_autoopen_dashboard(
        web_url(),
        wait=_env_float("AGENTCALL_TRAY_STARTUP_WAIT", 15.0),
        poll_interval=_env_float("AGENTCALL_TRAY_POLL_INTERVAL", 0.5),
    )

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
