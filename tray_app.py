"""CallPilot 菜单栏托盘 App（macOS）。

产品形态：后台服务（app.py，由 launchd 常驻）+ 顶栏一个状态图标。
比整窗 App 更贴合本项目「服务常驻、随手查看」的真实用法——关掉面板窗口
不影响接打电话，托盘图标始终在顶栏显示服务状态、一键打开面板/重启/退出。

打包版菜单显示四态状态与详情，通话时提供挂断和旁听定位；
源码版保留原有控制台与重启入口。退出打包版时停止服务与桥。

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
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WEB_URL = "http://127.0.0.1:47100"
HEALTH_PROBE_INTERVAL = 3.0
CALL_PROBE_INTERVAL = 1.0
MENU_PROBE_INTERVAL = 0.5


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
        "quit": "Quit CallPilot",
        "open_app": "Open CallPilot",
        "hangup": "Hang up",
        "listen": "Listen live",
    },
    "zh": {
        "open": "打开控制台",
        "restart": "重启服务",
        "uninstall": "卸载常驻",
        "quit": "退出 CallPilot",
        "open_app": "打开 CallPilot",
        "hangup": "挂断",
        "listen": "旁听",
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


def request_hangup(url: str, timeout: float = 4.0) -> bool:
    """复用现有挂断 API；失败交回主线程显示，不假装已经挂断。"""
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/call/hangup", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300 and json.loads(resp.read()).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def update_status_icon(app, state: str) -> None:
    from agentcall.macos_shell import draw_tray_icon

    # rumps 的 icon 属性只收文件名；复用它的 NSImage 存储和刷新入口，不写临时素材。
    app._icon_nsimage = draw_tray_icon(icon_path(False), state)
    if hasattr(app, "_nsapp"):
        app._nsapp.setStatusBarIcon()


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


def _ensure_launchd_installed(user_initiated: bool = False):
    if not _is_frozen() or sys.platform != "darwin":
        from agentcall.macos_launchd import LaunchdInstallResult

        return LaunchdInstallResult(changed=[], failures=[], warnings=[])
    from agentcall import macos_launchd

    layout = macos_launchd.make_layout(sys.executable, resources_dir=_resources_dir())
    # tray 单元由本进程自装：声明 no_restart 防止 bootout 杀掉自己（见函数 docstring）。
    return macos_launchd.install_launch_agents(
        layout, no_restart_labels={macos_launchd.TRAY_LABEL}, user_initiated=user_initiated,
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
    """透传 --auto 或旧的显式桥参数，开发入口不自动改写。"""
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


def _bring_window_to_front(pid: int) -> None:
    """AppKit 按 pid 激活窗口，不发送 Apple Events、无需自动化授权。"""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplicationActivateAllWindows, NSApplicationActivateIgnoringOtherApps, NSRunningApplication

        application = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if application is not None:
            application.activateWithOptions_(NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps)
    except Exception as exc:  # noqa: BLE001
        logger.warning("激活控制台窗口失败: %s", exc)


def open_or_focus_dashboard(
    current_proc: "subprocess.Popen | None",
    *,
    popen=subprocess.Popen,
    bring_to_front=_bring_window_to_front,
    cmd_factory=dashboard_command,
) -> "subprocess.Popen":
    """打开控制台窗口；已有窗口进程存活则拉到前台并复用，避免每次点击都新开一个。

    返回当前应持有的窗口进程句柄（复用旧的或新开的），调用方负责保存以便下次判活。
    """
    if current_proc is not None and current_proc.poll() is None:
        bring_to_front(current_proc.pid)
        return current_proc
    return popen(cmd_factory(), cwd=str(PROJECT_ROOT))


def open_listen_dashboard(current_proc, support: Path | None = None):
    """复用窗口；定位请求由窗口消费，启动页等待服务时也不会丢失。"""
    from agentcall.macos_launchd import app_support_dir

    directory = support if support is not None else app_support_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "listen-window").touch(mode=0o600)
    proc = open_or_focus_dashboard(current_proc)
    (directory / "focus-window").touch(mode=0o600)
    return proc


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


def _install_reopen_handler(callback) -> None:
    import objc
    from rumps.rumps import NSApp

    def applicationShouldHandleReopen_hasVisibleWindows_(self, application, visible):
        callback()
        return False

    objc.classAddMethods(NSApp, [objc.selector(
        applicationShouldHandleReopen_hasVisibleWindows_, signature=b"Z@:@Z",
    )])


def _bundled_macos() -> bool:
    return sys.platform == "darwin" and _is_frozen()


def _install_tracking_hooks(app, rumps) -> None:
    from agentcall.macos_shell import install_menu_tracking

    callbacks = {getattr(callback, "__func__", callback) for callback in (app._requests, app._refresh)}
    timers = [timer for timer in rumps.timers()
              if getattr(timer.callback, "__func__", timer.callback) in callbacks]
    app._menu_delegate = install_menu_tracking(
        app.menu._menu, timers, app._menu_will_open, app._menu_did_close,
    )


def _start_background_services(user_initiated: bool = False) -> None:
    from agentcall import macos_launchd, macos_shell

    support = macos_launchd.app_support_dir() if _bundled_macos() else None
    lang = macos_shell.ui_language() if support is not None else "en"
    try:
        if support is not None:
            macos_shell.write_startup_failure(support, "")
        result = _ensure_launchd_installed(user_initiated=user_initiated)
        if result.failures:
            logger.error("launchd 自装失败: %s", result.failure_summary())
            if support is not None:
                macos_shell.write_startup_failure(support, macos_shell.launchd_failure_reason(result, lang))
    except Exception as exc:  # noqa: BLE001
        logger.exception("launchd 自装失败")
        if support is not None:
            macos_shell.write_startup_failure(support, macos_shell.ui_text(
                f"后台服务无法启动（{type(exc).__name__}）。请重试。",
                f"Background services could not start ({type(exc).__name__}). Please retry.", lang,
            ))


def main() -> None:
    _prepend_runtime_paths()
    if "--selftest" in sys.argv[1:]:
        # 打包后 import 自检（不起服务/不装 launchd）：验证关键模块真进了 bundle。
        import app
        raise SystemExit(app._selftest())
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if _is_frozen() and sys.platform == "darwin":
        from agentcall.macos_shell import configure_shell_logging

        configure_shell_logging("tray")
    # 单例：launchd 常驻实例已在跑时，双击再启的实例让位退出（码 0，见函数注释）。
    # 句柄须持有到进程结束，锁才不失效。
    singleton_lock = _acquire_tray_singleton_lock()
    if singleton_lock is None:
        if _bundled_macos() and "--login" not in sys.argv:
            from agentcall.macos_shell import send_command

            send_command("open")
        logger.info("已有 CallPilot 菜单栏实例在运行，本实例让位退出")
        return
    bundled = _bundled_macos()
    # 主动启动先拉窗口，连 rumps 导入和菜单构造都不挡住首屏。
    early_window = None
    if bundled and "--login" not in sys.argv:
        try:
            early_window = open_or_focus_dashboard(None)
        except OSError:
            logger.exception("提前打开控制台失败，将由菜单栏重试")
    rumps = _import_rumps()
    from agentcall.macos_shell import bundled_version, ui_language

    lang = ui_language()
    version = bundled_version(sys.executable) if bundled else ""
    if not bundled:
        _start_background_services()
        maybe_autoopen_dashboard(
            web_url(), wait=_env_float("AGENTCALL_TRAY_STARTUP_WAIT", 15.0),
            poll_interval=_env_float("AGENTCALL_TRAY_POLL_INTERVAL", 0.5),
        )

    class CallPilotTray(rumps.App):
        def __init__(self) -> None:
            # 图标模式（非 template，保留品牌绿）；title 留空只显图标。
            super().__init__(
                "CallPilot", title=None, icon=icon_path(False),
                template=False, quit_button=None,
            )
            self._window_proc: subprocess.Popen | None = early_window
            self._install_thread: threading.Thread | None = None
            self._enable_requested = False
            self._quitting = False
            self._probe_thread: threading.Thread | None = None
            self._last_probe_started: float | None = None
            self._started_at = time.monotonic()
            self._meta: dict = {}
            self._connecting_since: float | None = None
            self._displayed_state: str | None = None
            self._menu_open = False
            self._hangup_thread: threading.Thread | None = None
            self._hangup_result: bool | None = None
            self._status_item = rumps.MenuItem(status_label(False, lang))
            self._detail_item = rumps.MenuItem("")
            self._action_items = {
                key: rumps.MenuItem(menu_label(key, lang), callback=callback)
                for key, callback in (("hangup", self._hangup), ("listen", self._listen),
                                      ("open_app", self._open), ("uninstall", self._uninstall), ("quit", self._quit))
            } if bundled else {}
            self.menu = [self._status_item, self._detail_item] if bundled else [
                self._status_item,
                None,
                rumps.MenuItem(menu_label("open", lang), callback=self._open),
                rumps.MenuItem(menu_label("restart", lang), callback=self._restart),
                rumps.MenuItem(menu_label("uninstall", lang), callback=self._uninstall),
                None,
                rumps.MenuItem(menu_label("quit", lang), callback=self._quit),
            ]
            if bundled:
                from dotenv import dotenv_values

                from agentcall import macos_launchd, macos_shell

                layout = macos_launchd.make_layout(sys.executable)
                self._render_status()
                macos_shell.write_startup_failure(layout.support_dir, "")
                setup = str(dotenv_values(layout.env_file).get("SETUP_DONE", "")).lower() not in {"1", "true", "yes"}
                if early_window is None and macos_shell.should_open_window("--login" in sys.argv, setup):
                    self._open(None)
                # 先开本地窗口，再后台安装；不让 launchctl/健康轮询挡住 Cocoa 主循环。
                self._start_services(user_initiated="--login" not in sys.argv)
                _install_reopen_handler(self._reopen)
                self._request_observer = macos_shell.observe_commands(lambda: self._requests(None))
                # before_start 时 rumps 的原生定时器和菜单才全部就绪。
                rumps.events.before_start.register(lambda: _install_tracking_hooks(self, rumps))
            else:
                self._refresh(None)

        def _start_services(self, user_initiated: bool = True) -> None:
            if self._install_thread is None or not self._install_thread.is_alive():
                self._enable_requested = False
                self._install_thread = threading.Thread(
                    target=lambda: _start_background_services(user_initiated=user_initiated), daemon=True,
                )
                self._install_thread.start()
            elif user_initiated:
                # 登录安装尚未结束时收到主动打开，保留 enable 请求供下一轮消费。
                self._enable_requested = True

        def _reopen(self) -> None:
            if not self._quitting:
                self._open(None)
                self._start_services()

        @rumps.timer(0.2)
        def _requests(self, _sender) -> None:
            if not bundled or self._quitting:
                return
            from agentcall.macos_shell import take_commands

            commands = take_commands()
            if "quit" in commands:
                self._quit(None)
                return
            elif "open" in commands:
                self._reopen()
            if "retry" in commands:
                self._start_services()
            if self._enable_requested and self._install_thread is not None and not self._install_thread.is_alive():
                self._start_services()
            self._refresh(None)
            self._render_status()
            if self._hangup_result is not None:
                succeeded, self._hangup_result = self._hangup_result, None
                if not succeeded:
                    from agentcall.macos_shell import ui_text

                    rumps.alert(title=menu_label("hangup", lang), message=ui_text(
                        "挂断失败，请打开 CallPilot 查看当前通话后重试。",
                        "Could not hang up. Open CallPilot to check the call and retry.", lang,
                    ))

        def _menu_will_open(self) -> None:
            self._menu_open = True
            self._render_status(rebuild_menu=True)
            self._refresh(None, immediate=True)

        def _menu_did_close(self) -> None:
            self._menu_open = False

        def _render_status(self, *, rebuild_menu: bool = False) -> None:
            from agentcall.macos_shell import tray_status

            now = time.monotonic()
            hardware = self._meta.get("hardware") or {}
            if hardware.get("usb_online") and not hardware.get("modem_connected"):
                if self._connecting_since is None:
                    self._connecting_since = now
            else:
                self._connecting_since = None
            status = tray_status(
                {"version": version, **self._meta} if self._meta else None,
                uptime=now - self._started_at, now=time.time(), lang=lang,
                connecting_for=now - self._connecting_since if self._connecting_since is not None else 0,
            )
            if self._status_item.title != status.title:
                self._status_item.title = status.title
            if self._detail_item.title != status.detail:
                self._detail_item.title = status.detail
            if status.state != self._displayed_state:
                update_status_icon(self, status.state)
            # 开菜单或进出通话时重建；秒数变化仅改标题，不抖动图标和菜单。
            if rebuild_menu or self._displayed_state is None or (status.state == "in_call") != (
                self._displayed_state == "in_call"
            ):
                self.menu.clear()
                # rumps 的回调注册表强持有菜单项，复用实例避免每通累积对象。
                keys = (["hangup", "listen"] if status.state == "in_call" else [])
                keys += ["open_app", "uninstall", "quit"]
                self.menu = [self._status_item, self._detail_item, *[self._action_items[key] for key in keys]]
            self._displayed_state = status.state

        @rumps.timer(5)
        def _refresh(self, _sender, *, immediate: bool = False) -> None:
            if bundled:
                now = time.monotonic()
                # 缩短就绪态的发现窗口，不能等知道 call_active 后才加速发现来电。
                interval = MENU_PROBE_INTERVAL if self._menu_open else (
                    CALL_PROBE_INTERVAL if self._displayed_state in {"ready", "in_call"} else HEALTH_PROBE_INTERVAL
                )
                if (not immediate and self._last_probe_started is not None
                        and now - self._last_probe_started < interval):
                    return
                if self._probe_thread is None or not self._probe_thread.is_alive():
                    def probe():
                        from agentcall.macos_shell import fetch_meta

                        self._meta = fetch_meta(web_url(), tray=True)

                    self._probe_thread = threading.Thread(target=probe, daemon=True)
                    self._last_probe_started = now
                    self._probe_thread.start()
                return
            online = probe_online(web_url())
            self.icon = icon_path(online)
            self._status_item.title = status_label(online, lang)

        def _open(self, _sender) -> None:
            try:
                self._window_proc = open_or_focus_dashboard(self._window_proc)
                if bundled:
                    from agentcall.macos_launchd import app_support_dir

                    (app_support_dir() / "focus-window").touch(mode=0o600)
            except OSError as exc:
                logger.error("打开控制台失败: %s", exc)

        def _restart(self, _sender) -> None:
            request_restart(web_url())

        def _hangup(self, _sender) -> None:
            if self._hangup_thread is None or not self._hangup_thread.is_alive():
                def hangup():
                    self._hangup_result = request_hangup(web_url())

                self._hangup_thread = threading.Thread(target=hangup, daemon=True)
                self._hangup_thread.start()

        def _listen(self, _sender) -> None:
            try:
                self._window_proc = open_listen_dashboard(self._window_proc)
            except OSError:
                logger.exception("打开旁听位置失败")

        def _uninstall(self, _sender) -> None:
            try:
                removed = _uninstall_launchd()
                logger.info("launchd agents removed: %s", ", ".join(removed) or "none")
            except Exception as exc:  # noqa: BLE001
                logger.error("卸载常驻失败: %s", exc)

        def _quit(self, _sender) -> None:
            if not bundled:
                rumps.quit_application()
                return
            from agentcall import macos_launchd, macos_shell

            if self._quitting:
                return
            from AppKit import NSApplication

            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            meta = macos_shell.fetch_meta(web_url())
            confirmed = rumps.alert(
                title=macos_shell.ui_text("退出 CallPilot？", "Quit CallPilot?", lang),
                message=macos_shell.quit_message(meta, lang),
                ok=menu_label("quit", lang), cancel=macos_shell.ui_text("取消", "Cancel", lang),
            ) == 1
            if not confirmed:
                return
            self._quitting = True
            try:
                # 等待正在进行的安装完成，防止 bootout 后被安装线程再次 bootstrap。
                if self._install_thread is not None:
                    self._install_thread.join(timeout=1)
                    if self._install_thread.is_alive():
                        raise RuntimeError(macos_shell.ui_text(
                            "后台服务正在启动，请稍后再退出。",
                            "Background services are starting. Please try quitting again shortly.",
                            lang,
                        ))
                layout = macos_launchd.make_layout(sys.executable)

                def close_window():
                    from desktop_app import close_bundled_window

                    close_bundled_window(layout.support_dir)

                macos_shell.quit_services(True, layout, close_window, rumps.quit_application, lang=lang)
            except Exception as exc:  # noqa: BLE001
                rumps.alert(title=macos_shell.ui_text("尚未退出", "Could not quit", lang), message=str(exc),
                            ok=macos_shell.ui_text("知道了", "OK", lang))
            finally:
                self._quitting = False

    CallPilotTray().run()


if __name__ == "__main__":
    main()
