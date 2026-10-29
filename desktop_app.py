"""AgentCall 桌面窗口：pywebview 薄前端，"打开查看"网页仪表盘。

产品架构：桥+服务由系统常驻机制（macOS launchd / Windows 计划任务）托管，
本窗口只是查看入口，关窗不影响服务。
启动流程：
    打包版 macOS：立即显示本地启动页，后台等待 launchd；失败页可重试和复制脱敏诊断。
    其他运行方式保留以下流程：
    1. 探测 ``<AGENTCALL_WEB_URL>/api/meta`` 判断服务是否已在运行；
    2. 在跑 → 直接开窗指向服务地址；
    3. 没跑 → 打包版 macOS 等待 launchd 拉起；其他运行方式用项目 venv 的
       python 拉起 app.py（stdout/stderr 追加到 ``data/app_console.log``）；
    4. 拉起失败或等待超时 → 开一个错误提示窗，说明手动启动命令。

用法：
    macOS / Linux:  .venv/bin/python desktop_app.py
    Windows:        .venv\\Scripts\\python desktop_app.py

可配置环境变量（均有默认值）：
    AGENTCALL_WEB_URL        服务地址，默认 http://127.0.0.1:47100
    AGENTCALL_PROBE_TIMEOUT  单次探测超时秒数，默认 2
    AGENTCALL_STARTUP_WAIT   拉起后等待就绪的最长秒数，默认 15
    AGENTCALL_POLL_INTERVAL  就绪轮询间隔秒数，默认 0.5
    AGENTCALL_PYTHON         拉起服务用的 Python 解释器，默认项目 venv 内解释器
                             （POSIX=.venv/bin/python，Windows=.venv\\Scripts\\python.exe）
    AGENTCALL_APP_SCRIPT     服务入口脚本，默认 <项目>/app.py
    AGENTCALL_CONSOLE_LOG    服务控制台日志文件，默认 <项目>/data/app_console.log
    AGENTCALL_WINDOW_WIDTH   窗口宽度，默认 1100
    AGENTCALL_WINDOW_HEIGHT  窗口高度，默认 780
"""

from __future__ import annotations

import html as html_escape
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def _resolve_project_root() -> Path:
    """定位仓库根：PyInstaller 打包后优先读内嵌 project_root.txt（poc 同款）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        marker = Path(meipass) / "project_root.txt"
        if marker.exists():
            return Path(marker.read_text(encoding="utf-8").strip())
    return Path(__file__).resolve().parent


PROJECT_ROOT = _resolve_project_root()

# agentcall 包在 src/ 下（src 布局）：为了未 pip install -e 时也能直跑本入口
# 脚本，显式把 src 加进 sys.path；冻结（PyInstaller）时包已内嵌进可执行文件
# （spec 的 pathex 含 src），跳过插入以免误引仓库源码。
if not getattr(sys, "_MEIPASS", None):
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
from agentcall import config, platforms  # noqa: E402

WINDOW_TITLE = "CallPilot"
ERROR_WINDOW_TITLE = "CallPilot — 服务未启动"
DEFAULT_WEB_URL = "http://127.0.0.1:47100"

ServiceStatus = Literal["already", "started", "failed"]


# ---- 环境变量读取 ----

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是合法数字，使用默认值 %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是合法整数，使用默认值 %s", name, raw, default)
        return default


def _launch_config() -> tuple[str, str, str]:
    """返回拉起服务所需的 (python 解释器, 入口脚本, 控制台日志路径)。"""
    if getattr(sys, "_MEIPASS", None) or getattr(sys, "frozen", False):
        return (
            os.getenv("AGENTCALL_PYTHON", sys.executable),
            os.getenv("AGENTCALL_APP_SCRIPT", "--service"),
            os.getenv("AGENTCALL_CONSOLE_LOG", str(config.log_dir() / "app_console.log")),
        )
    python_exe = os.getenv(
        "AGENTCALL_PYTHON", str(platforms.venv_python(PROJECT_ROOT))
    )
    app_script = os.getenv("AGENTCALL_APP_SCRIPT", str(PROJECT_ROOT / "app.py"))
    log_path = os.getenv(
        "AGENTCALL_CONSOLE_LOG", str(PROJECT_ROOT / "data" / "app_console.log")
    )
    return python_exe, app_script, log_path


def _bundled_macos_service_is_system_managed() -> bool:
    """Return whether launchd, rather than this window, owns the service."""
    frozen = bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))
    return platforms.IS_MACOS and frozen


# ---- 纯逻辑（可单测）----

def probe_service(url: str, timeout: float = 2.0) -> bool:
    """探测 ``url`` 是否返回 2xx，判断服务是否已在运行。

    任何网络错误 / 非 2xx 响应都视为「服务不在」，返回 False。
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            return 200 <= int(status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_service_ready(
    url: str,
    max_wait: float = 15.0,
    interval: float = 0.5,
    probe_timeout: float = 2.0,
) -> bool:
    """轮询 ``url`` 直到服务就绪或超时；就绪返回 True，超时返回 False。

    ``max_wait <= 0`` 时只探测一次。
    """
    deadline = time.monotonic() + max_wait
    while True:
        if probe_service(url, timeout=probe_timeout):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def ensure_service_running(
    meta_url: str,
    *,
    python_exe: str | Path | None = None,
    app_script: str | Path | None = None,
    log_path: str | Path | None = None,
    max_wait: float | None = None,
    probe_timeout: float | None = None,
    poll_interval: float | None = None,
) -> ServiceStatus:
    """确保后端服务在运行；未运行则尝试拉起并等待就绪。

    返回值：
        ``"already"``  服务本来就在运行；
        ``"started"``  本函数拉起服务且已等到就绪；
        ``"failed"``   拉起进程失败，或等待就绪超时。

    参数为 None 时从对应环境变量读取（见模块 docstring）。
    """
    if probe_timeout is None:
        probe_timeout = _env_float("AGENTCALL_PROBE_TIMEOUT", 2.0)
    if max_wait is None:
        max_wait = _env_float("AGENTCALL_STARTUP_WAIT", 15.0)
    if poll_interval is None:
        poll_interval = _env_float("AGENTCALL_POLL_INTERVAL", 0.5)
    default_python, default_script, default_log = _launch_config()
    if python_exe is None:
        python_exe = default_python
    if app_script is None:
        app_script = default_script
    if log_path is None:
        log_path = default_log

    if probe_service(meta_url, timeout=probe_timeout):
        logger.info("服务已在运行: %s", meta_url)
        return "already"

    # The bundled tray installs a KeepAlive launchd job before opening this
    # window. Spawning another --service process during launchd startup/restart
    # races both instances for WEB_PORT and can leave the old configuration live.
    if _bundled_macos_service_is_system_managed():
        logger.info("服务暂未就绪，等待 launchd 托管实例启动: %s", meta_url)
        return (
            "started"
            if wait_service_ready(
                meta_url,
                max_wait=max_wait,
                interval=poll_interval,
                probe_timeout=probe_timeout,
            )
            else "failed"
        )

    log_file = Path(log_path)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # Windows 下 GUI 进程（打包后 console=False）拉起 python.exe 会弹出
        # 黑色控制台窗口，用户误关即杀死服务；CREATE_NO_WINDOW 抑制分配。
        # start_new_session 在 Windows 被 stdlib 静默忽略，无需分支。
        popen_kwargs: dict = {}
        if platforms.IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        with log_file.open("ab") as out:
            proc = subprocess.Popen(
                [str(python_exe), str(app_script)],
                cwd=str(config.app_support_dir() if getattr(sys, "frozen", False) else PROJECT_ROOT),
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                **popen_kwargs,
            )
    except OSError as exc:
        logger.error("拉起服务进程失败: %s", exc)
        return "failed"

    logger.info(
        "已拉起服务进程 (pid=%s)，等待就绪（最多 %.0f 秒）…",
        getattr(proc, "pid", "?"),
        max_wait,
    )
    if wait_service_ready(
        meta_url, max_wait=max_wait, interval=poll_interval, probe_timeout=probe_timeout
    ):
        return "started"
    logger.error("等待服务就绪超时（%.0f 秒），控制台日志见 %s", max_wait, log_file)
    return "failed"


def build_error_html(web_url: str, log_path: str, start_cmd: str) -> str:
    """生成「服务启动失败」错误提示窗的 HTML（含手动启动命令）。"""
    esc = html_escape.escape
    return f"""
<div style="font-family: -apple-system, 'PingFang SC', sans-serif;
            max-width: 620px; margin: 60px auto; padding: 0 24px; color: #333;">
  <h1 style="font-size: 22px;">AgentCall 服务未能启动</h1>
  <p>桌面窗口尝试自动拉起后端服务，但等待超时或拉起失败。</p>
  <p>请在终端手动启动服务后重新打开本窗口：</p>
  <pre style="background: #f4f4f4; padding: 12px 16px; border-radius: 8px;
              overflow-x: auto; font-size: 13px;">{esc(start_cmd)}</pre>
  <p>启动报错可查看控制台日志：</p>
  <pre style="background: #f4f4f4; padding: 12px 16px; border-radius: 8px;
              overflow-x: auto; font-size: 13px;">{esc(log_path)}</pre>
  <p style="color: #888; font-size: 13px;">
    服务就绪后也可直接用浏览器访问 <a href="{esc(web_url)}">{esc(web_url)}</a>。
  </p>
</div>
"""


# ---- 窗口入口 ----

def activate_macos_app() -> bool:
    if not platforms.IS_MACOS:
        return False
    try:
        from AppKit import NSApplication

        application = NSApplication.sharedApplication()
        if _bundled_macos_service_is_system_managed():
            from AppKit import NSApplicationActivationPolicyRegular

            application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        application.activateIgnoringOtherApps_(True)
        return True
    except Exception as exc:  # noqa: BLE001 - AppKit may be absent in tests/Linux builds
        logger.warning("激活桌面窗口失败: %s", exc)
        return False


def _activate_on_window_shown(window, activate=activate_macos_app) -> None:
    events = getattr(window, "events", None)
    shown = getattr(events, "shown", None)
    if shown is None:
        return
    shown += activate


def _import_webview():
    """导入 pywebview；未安装时打印中文提示并退出。"""
    try:
        import webview
    except ImportError:
        pip_hint = (
            r".venv\Scripts\pip" if platforms.IS_WINDOWS else ".venv/bin/pip"
        )
        print(
            "未安装 pywebview，无法打开桌面窗口。\n"
            f"请先安装依赖: {pip_hint} install \"pywebview>=5.0\"\n"
            f"或直接用浏览器访问 {os.getenv('AGENTCALL_WEB_URL', DEFAULT_WEB_URL)}",
            file=sys.stderr,
        )
        sys.exit(1)
    return webview


def startup_html(reason: str | None = None, lang: str | None = None) -> str:
    from agentcall.macos_shell import ui_language, ui_text

    lang = lang or ui_language()
    title = (ui_text("服务未能启动", "Service could not start", lang) if reason
             else ui_text("正在启动…", "Starting…", lang))
    buttons = f"""<p><button onclick="pywebview.api.retry()">{ui_text('重试', 'Retry', lang)}</button>
        <button onclick="pywebview.api.copy_diagnostics().then(() => {{
          document.getElementById('copied').textContent = '{ui_text('诊断信息已复制', 'Diagnostics copied', lang)}';
        }})">{ui_text('复制诊断信息', 'Copy diagnostics', lang)}</button></p>
        <p id="copied" role="status"></p>""" if reason else ""
    message = html_escape.escape(reason or ui_text(
        '正在连接电话服务，请稍候。', 'Connecting to the phone service. Please wait.', lang,
    ))
    return f"""<!doctype html><html lang="{lang}"><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>CallPilot</title><style>
      body {{font:16px -apple-system,'PingFang SC',sans-serif;color:#242424;background:#fafafa;
             max-width:640px;margin:80px auto;padding:24px}}
      h1 {{font-size:28px}} h2 {{font-size:20px}} p {{line-height:1.6}}
      button {{font:inherit;padding:10px 16px;margin:8px 8px 0 0;border:1px solid #aaa;
               border-radius:6px;background:white;cursor:pointer}}
      </style><h1>CallPilot</h1><h2>{title}</h2>
      <p>{message}</p>{buttons}</html>"""


def _window_lock(support: Path):
    from tray_app import _acquire_tray_singleton_lock

    support.mkdir(parents=True, exist_ok=True)
    return _acquire_tray_singleton_lock(support / "window.lock")


def close_bundled_window(support: Path) -> None:
    """让窗口正常关闭以保存几何；持锁到进程收尾，避免退出后遗留 Dock 图标。"""
    lock = _window_lock(support)
    if lock is not None:
        lock.close()
        return
    (support / "close-window").touch(mode=0o600)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        lock = _window_lock(support)
        if lock is not None:
            lock.close()
            return
        time.sleep(0.05)
    from agentcall.macos_shell import ui_text

    raise RuntimeError(ui_text(
        "窗口尚未关闭，请重试退出。", "The window has not closed yet. Please try quitting again.",
    ))


def _install_window_hooks(window, support: Path, splash=None) -> None:
    import objc
    from AppKit import NSApplication, NSApplicationActivationPolicyRegular, NSMakeRect, NSScreen
    from PyObjCTools import AppHelper

    # 部分 PyObjC 版本把此枚举标成 unsigned int；在 Cocoa delegate 创建前修正为 NSUInteger。
    objc.registerMetaDataForSelector(b"NSObject", b"applicationShouldTerminate:", {"retval": {"type": b"Q"}})
    from webview.platforms.cocoa import BrowserView

    from agentcall import macos_shell

    def applicationShouldTerminate_(self, application):
        macos_shell.send_command("quit", support)
        return 0  # NSTerminateCancel：由菜单栏确认并停止服务后再正常关窗。

    objc.classAddMethods(BrowserView.AppDelegate, [objc.selector(
        # 后端若已被其他代码导入，必须匹配已注册的 ABI，不能原地把 I 替换为 Q。
        applicationShouldTerminate_, signature=BrowserView.AppDelegate.applicationShouldTerminate_.signature,
    )])
    path = support / "window.json"

    def rect_tuple(rect):
        return tuple(int(v) for v in (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height))

    def restore():
        native = BrowserView.instances[window.uid].window
        if splash is not None and splash.closed:
            native.close()
            return
        screens = [rect_tuple(screen.visibleFrame()) for screen in NSScreen.screens()]
        x, y, w, h = macos_shell.restore_geometry(path, screens)
        if splash is not None:
            x, y, w, h = rect_tuple(splash.native.frame())
        # 使用 Cocoa 全局坐标保存/恢复，避免 pywebview 多屏坐标转换不一致。
        native.setFrame_display_(NSMakeRect(x, y, w, h), True)
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        if splash is not None:
            splash.native.orderOut_(None)
        native.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

    def shown():
        AppHelper.callAfter(restore)

    def closing():
        native = BrowserView.instances[window.uid].window
        macos_shell.save_geometry(path, rect_tuple(native.frame()))

    window.events.shown += shown
    window.events.closing += closing


class BundledStartup:
    """后台等待服务；本地页面立即显示，重试与关闭不阻塞 Cocoa。"""

    def __init__(self, window, url: str, layout) -> None:
        from agentcall.macos_shell import ui_language

        self.lang = ui_language()
        self.window = window
        self.url = url
        self.layout = layout
        self.closed = threading.Event()
        self.retry_requested = threading.Event()
        self.diagnostics = ""
        self.diagnostic_results: queue.Queue = queue.Queue()
        self.generation = 0

    def retry(self):
        from agentcall.macos_shell import send_command, write_startup_failure

        write_startup_failure(self.layout.support_dir, "")
        send_command("retry", self.layout.support_dir)
        self.retry_requested.set()

    def download_update(self):
        import webbrowser

        from agentcall.app_updates import https_url
        from agentcall.macos_shell import fetch_meta

        current, trusted = urlsplit(self.window.get_current_url() or ""), urlsplit(self.url)
        if (trusted.hostname not in {"127.0.0.1", "localhost", "::1"}
                or (current.scheme, current.netloc) != (trusted.scheme, trusted.netloc)):
            return False
        # The renderer cannot supply an arbitrary URL to the native bridge.
        meta = fetch_meta(self.url, tray=True)
        url = (meta.get("update") or {}).get("downloadUrl")
        return bool(https_url(url) and webbrowser.open(url))

    def copy_diagnostics(self):
        from AppKit import NSPasteboard, NSPasteboardTypeString
        from PyObjCTools import AppHelper

        def copy():
            pasteboard = NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            pasteboard.setString_forType_(self.diagnostics, NSPasteboardTypeString)

        AppHelper.callAfter(copy)

    def run(self):
        from agentcall import macos_shell

        deadline = time.monotonic() + 30
        waiting = True
        diagnosing = False
        displayed_failure = ""
        close_request = self.layout.support_dir / "close-window"
        focus_request = self.layout.support_dir / "focus-window"
        listen_request = self.layout.support_dir / "listen-window"
        while not self.closed.wait(0.1):
            if close_request.exists():
                close_request.unlink(missing_ok=True)
                self.window.destroy()
                return
            if focus_request.exists():
                focus_request.unlink(missing_ok=True)
                self.window.restore()
                self.window.show()
            if self.retry_requested.is_set():
                self.retry_requested.clear()
                self.window.load_html(startup_html(lang=self.lang))
                deadline = time.monotonic() + 30
                waiting = True
                diagnosing = False
                displayed_failure = ""
                self.generation += 1
            startup_failure = macos_shell.read_startup_failure(self.layout.support_dir)
            if startup_failure and startup_failure != displayed_failure:
                # 安装结果可能晚于超时诊断；具体错误始终优先，不能被 diagnosing 挡住。
                self.window.load_html(startup_html(startup_failure, self.lang))
                displayed_failure = startup_failure
            try:
                generation, reason, diagnostics = self.diagnostic_results.get_nowait()
                if generation == self.generation and waiting:
                    self.diagnostics = diagnostics
                    self.window.load_html(startup_html(startup_failure or reason, self.lang))
            except queue.Empty:
                pass
            if not waiting:
                if not startup_failure:
                    target = macos_shell.take_notification_target(self.layout.support_dir)
                    if target is not None:
                        current = urlsplit(self.window.get_current_url() or "")
                        trusted = urlsplit(self.url)
                        if (current.scheme, current.netloc) != (trusted.scheme, trusted.netloc):
                            self.window.load_url(self.url.rstrip("/") + "/" + target)
                        else:
                            self.window.evaluate_js(
                                f"if (location.hash !== {json.dumps(target)}) {{ "
                                f"location.hash = {json.dumps(target)}; }} "
                                "else { window.dispatchEvent(new Event('hashchange')); }"
                            )
                    if listen_request.exists():
                        # 重复点击仍定位；只改锚点，不刷新页面或触发音频播放。
                        self.window.evaluate_js(
                            "if (location.hash !== '#listen') { location.hash = 'listen'; } "
                            "else { window.dispatchEvent(new Event('hashchange')); }"
                        )
                        listen_request.unlink(missing_ok=True)
                    continue
                # app 可能先健康、tray 稍后安装失败；进入管理页后仍接收最终失败结果。
                waiting = True
                diagnosing = False
            meta = macos_shell.fetch_meta(self.url)
            if meta and not startup_failure:
                target = self.url
                if listen_request.exists():
                    target = self.url.rstrip("/") + "/#listen"
                    listen_request.unlink(missing_ok=True)
                notification_target = macos_shell.take_notification_target(self.layout.support_dir)
                if notification_target is not None:
                    target = self.url.rstrip("/") + "/" + notification_target
                self.window.load_url(target)
                waiting = False
            elif (startup_failure or time.monotonic() >= deadline) and not diagnosing:
                diagnosing = True
                # launchctl 诊断再慢也不能挡住关窗或重试；过期结果不覆盖新一轮启动。
                def diagnose(generation, startup_failure):
                    try:
                        reason, report = macos_shell.collect_diagnostics(self.layout, {}, lang=self.lang)
                    except Exception:  # noqa: BLE001
                        reason = macos_shell.failure_reason("", self.lang)
                        report = macos_shell.ui_text("诊断信息暂时无法读取", "Diagnostics are unavailable", self.lang)
                    self.diagnostic_results.put((generation, startup_failure or reason, report))

                threading.Thread(target=diagnose, args=(self.generation, startup_failure), daemon=True).start()


def _bundled_main(webview, web_url: str) -> None:
    from agentcall import macos_launchd
    from tray_app import _bring_window_to_front

    layout = macos_launchd.make_layout(sys.executable)
    lock = _window_lock(layout.support_dir)
    if lock is None:
        try:
            pid = int((layout.support_dir / "window.lock").read_text())
            _bring_window_to_front(pid)
        except (OSError, ValueError):
            pass
        return
    splash = None
    try:
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        (layout.support_dir / "close-window").unlink(missing_ok=True)
        if webview is None:
            from agentcall.macos_window import show_startup_window

            splash = show_startup_window(layout.support_dir)
            webview = _import_webview()
            if splash.closed:
                return
        window = webview.create_window(
            WINDOW_TITLE, html=startup_html(), width=1100, height=780, **({"hidden": True} if splash else {}),
        )
        startup = BundledStartup(window, web_url, layout)
        from agentcall.macos_notifications import NotificationBridge

        notifications = NotificationBridge(window.get_current_url, web_url)
        window.expose(startup.retry, startup.copy_diagnostics, startup.download_update,
                      notifications.notification_status, notifications.request_notifications,
                      notifications.open_notification_settings)
        window.events.closed += startup.closed.set
        _install_window_hooks(window, layout.support_dir, splash)
        webview.start(lambda: threading.Thread(target=startup.run, daemon=True).start())
        startup.closed.set()
    finally:
        if splash is not None:
            splash.close()
        lock.close()


def main() -> None:
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        from agentcall.macos_shell import configure_shell_logging

        configure_shell_logging("window")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    web_url = os.getenv("AGENTCALL_WEB_URL", DEFAULT_WEB_URL).rstrip("/")
    if _bundled_macos_service_is_system_managed():
        _bundled_main(None, web_url)
        return
    webview = _import_webview()
    meta_url = f"{web_url}/api/meta"
    width = _env_int("AGENTCALL_WINDOW_WIDTH", 1100)
    height = _env_int("AGENTCALL_WINDOW_HEIGHT", 780)

    status = ensure_service_running(meta_url)
    if status == "failed":
        python_exe, app_script, log_path = _launch_config()
        # Windows PowerShell 5.1 不认 &&，且路径可能含空格；分号 + 引号两平台通吃
        start_cmd = f'cd "{PROJECT_ROOT}"; "{python_exe}" "{app_script}"'
        window = webview.create_window(
            ERROR_WINDOW_TITLE,
            html=build_error_html(web_url, log_path, start_cmd),
            width=width,
            height=height,
        )
    else:
        window = webview.create_window(WINDOW_TITLE, web_url, width=width, height=height)
    if window is not None:
        _activate_on_window_shown(window)
    webview.start()


if __name__ == "__main__":
    main()
