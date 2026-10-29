"""打包版 macOS 外壳的可测试逻辑，不在导入时操作系统服务。"""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Callable, NamedTuple

from agentcall import macos_launchd

Geometry = tuple[int, int, int, int]
COMMAND_NOTIFICATION = "ai.bondings.callpilot.shell-request"


def ui_language(languages: list[str] | None = None) -> str:
    """界面跟随系统首选语言，不读取 AI 通话语言 AGENT_LANGUAGE。"""
    if languages is None:
        languages = []
        if sys.platform == "darwin":
            from Foundation import NSLocale

            languages = list(NSLocale.preferredLanguages())
    return "zh" if any(str(language).lower().startswith("zh") for language in languages) else "en"


def ui_text(zh: str, en: str, lang: str | None = None) -> str:
    return zh if (lang or ui_language()) == "zh" else en


@lru_cache(maxsize=1)
def _tray_menu_delegate_class():
    from Foundation import NSObject

    class CallPilotMenuDelegate(NSObject):
        def menuWillOpen_(self, menu):
            self.on_open()

        def menuDidClose_(self, menu):
            self.on_close()

    return CallPilotMenuDelegate


def install_menu_tracking(menu, timers, on_open, on_close):
    """rumps 启动完定时器后补注册 common modes，不修改其全局 Timer 实现。"""
    from Foundation import NSRunLoop, NSRunLoopCommonModes

    for timer in timers:
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer._nstimer, NSRunLoopCommonModes)
    delegate = _tray_menu_delegate_class().alloc().init()
    delegate.on_open = on_open
    delegate.on_close = on_close
    menu.setDelegate_(delegate)
    return delegate  # NSMenu 不强持有 delegate，由托盘实例保存。


class TrayStatus(NamedTuple):
    state: str
    title: str
    detail: str


def tray_status(
    meta: dict | None, *, uptime: float, now: float, connecting_for: float = 0,
    startup_grace: float = 45, lang: str = "zh",
) -> TrayStatus:
    """纯状态判定；时间由调用方注入，不读系统、不做 I/O。"""
    def text(zh, en):
        return ui_text(zh, en, lang)

    if not meta:
        if uptime <= startup_grace:
            return TrayStatus("transient", text("正在启动…", "Starting…"), "")
        return TrayStatus("attention", text("服务没有运行：打开 CallPilot 查看原因",
                                            "Service is not running: open CallPilot to check why"), "")
    hardware = meta.get("hardware") or {}
    sim = meta.get("sim") or {}
    details = []
    carrier = sim.get("carrier")
    if carrier and carrier != "未知":
        details.append(text(carrier, {"美国运营商": "US carrier", "中国移动": "China Mobile",
                                     "中国联通": "China Unicom", "中国电信": "China Telecom",
                                     "中国广电": "China Broadnet"}.get(carrier, carrier)))
    if sim:
        details.append(text("已注册", "Registered") if sim.get("registered") else (
            text("仅 LTE 已注册", "LTE registered only") if sim.get("eps_registered") else
            text("搜网中…", "Searching…")
        ))
    level = (hardware.get("signal") or {}).get("level")
    if level:
        signals = {"excellent": ("很好", "Excellent"), "good": ("良好", "Good"),
                   "fair": ("一般", "Fair"), "weak": ("弱", "Weak"), "unknown": ("未知", "Unknown")}
        details.append(text(*signals.get(level, signals["unknown"])))
    if meta.get("version"):
        details.append(str(meta["version"]))
    detail = " · ".join(details)

    def status(state, zh, en):
        return TrayStatus(state, text(zh, en), detail)

    if meta.get("call_active"):
        call = meta.get("call") or {}
        started = call.get("started_at")
        seconds = max(0, int(now - started)) if isinstance(started, (int, float)) else 0
        suffix = re.sub(r"[^0-9]", "", str(call.get("number_suffix") or ""))[-4:]
        parts = [text("通话中", "In call")]
        if suffix:
            parts.append(text(f"尾号 {suffix}", f"ending {suffix}"))
        parts.append(f"{seconds // 60:02d}:{seconds % 60:02d}" if started is not None else "--:--")
        return TrayStatus("in_call", " · ".join(parts), detail)

    # 未连上 AT 时 SIM 缓存可能是 UNKNOWN_SIM，不据此判定缺卡。
    connected = hardware.get("modem_connected")
    resetting = hardware.get("modem_resetting")
    if hardware.get("usb_online") is False:
        return status("attention", "没检测到模组：检查 USB 线，换个 USB 口",
                      "No modem detected: Check the USB cable or try another port")
    if hardware.get("usb_online") and not connected and connecting_for > 60:
        return status("attention", "未能连接模组：重新插拔 USB，在窗口里查看原因",
                      "Cannot connect to modem: reconnect USB and open CallPilot to check why")
    if connected and sim.get("present") is False:
        return status("attention", "没检测到 SIM 卡：插好卡后在窗口里点『重新读卡』",
                      "No SIM detected: insert the SIM, then choose Read SIM again in CallPilot")
    if (connected and not sim.get("network_attached")
            and sim.get("reg_status") == sim.get("eps_status") == "注册被拒"):
        return status("attention", "卡被网络拒绝：确认卡已开通、未欠费",
                      "SIM rejected by network: check activation and account balance")
    if (meta.get("credentials") or {}).get("ok") is False:
        return status("attention", "还没填写模型 Key：打开 CallPilot 完成设置",
                      "Missing model key: open CallPilot to finish setup")
    if resetting:
        return status("transient", "模组恢复中，约 30 秒", "Modem recovering, about 30 seconds")
    if not connected:
        return status("transient", "正在连接模组…", "Connecting to modem…")
    if sim.get("network_attached"):
        return status("ready", "可以接打电话", "Ready to make and receive calls")
    return status("transient", "正在搜网，一般 30 秒内完成", "Searching for network, usually within 30 seconds")


def tray_icon_color(state: str) -> tuple[float, float, float]:
    return {"ready": (0.15, 0.75, 0.35), "in_call": (0.1, 0.55, 1.0),
            "transient": (1.0, 0.75, 0.1), "attention": (0.95, 0.2, 0.2)}[state]


def bundled_version(executable: str) -> str:
    import plistlib

    bundle = macos_launchd.bundle_root_for_executable(executable)
    if bundle is not None:
        try:
            with (bundle / "Contents" / "Info.plist").open("rb") as stream:
                return str(plistlib.load(stream).get("CFBundleShortVersionString", ""))
        except (OSError, ValueError):
            pass
    return ""


def draw_tray_icon(path: str, state: str):
    """在现有品牌图标上叠点；黑白双描边在明暗菜单栏上都保留边界。"""
    from AppKit import NSBezierPath, NSColor, NSCompositingOperationSourceOver, NSImage, NSMakeRect

    base = NSImage.alloc().initWithContentsOfFile_(path)
    image = NSImage.alloc().initWithSize_((22, 22))
    image.lockFocus()
    try:
        base.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(1, 2, 18, 18), NSMakeRect(0, 0, 0, 0), NSCompositingOperationSourceOver, 1.0,
        )
        for rect, color in [((13, 0, 9, 9), NSColor.blackColor()),
                            ((14, 1, 7, 7), NSColor.whiteColor()),
                            ((15, 2, 5, 5), NSColor.colorWithCalibratedRed_green_blue_alpha_(
                                *tray_icon_color(state), 1.0))]:
            color.setFill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(*rect)).fill()
    finally:
        image.unlockFocus()
    image.setTemplate_(False)
    return image


def configure_shell_logging(
    process: str, log_dir: Path | None = None, *, max_bytes: int = 5 * 1024 * 1024, backup_count: int = 3,
) -> None:
    """为打包版外壳保留有界日志；重复初始化不叠加 handler 或异常钩子。"""
    directory = log_dir if log_dir is not None else macos_launchd.app_support_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{process}.log"
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) and h.baseFilename == str(path.resolve()) for h in root.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    previous_stderr = sys.stderr
    stderr_state = threading.local()

    class ShellStderr(io.TextIOBase):
        # rumps 会捕获回调异常再 print_exc；直接写 handler，避免 StreamHandler 递归。
        def write(self, text: str) -> int:
            if previous_stderr is not None:
                try:
                    previous_stderr.write(text)
                except (OSError, ValueError):
                    pass
            if not getattr(stderr_state, "writing", False):
                stderr_state.writing = True
                try:
                    for offset in range(0, len(text), 1024):
                        message = text[offset:offset + 1024].rstrip()
                        if message:
                            handler.handle(logging.LogRecord("stderr", logging.ERROR, "", 0, message, (), None))
                finally:
                    stderr_state.writing = False
            return len(text)

        def flush(self) -> None:
            if previous_stderr is not None:
                try:
                    previous_stderr.flush()
                except (OSError, ValueError):
                    pass

    sys.stderr = ShellStderr()
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def uncaught(exc_type, exc_value, traceback):
        root.critical("外壳未捕获异常", exc_info=(exc_type, exc_value, traceback))
        previous_hook(exc_type, exc_value, traceback)

    def thread_uncaught(args):
        root.critical("外壳线程未捕获异常", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread_hook(args)

    sys.excepthook = uncaught
    threading.excepthook = thread_uncaught


def launchd_failure_reason(result: macos_launchd.LaunchdInstallResult, lang: str = "zh") -> str:
    details = []
    for failure in result.failures:
        first_line = (failure.stderr or failure.stdout).strip().splitlines()
        details.append(
            f"{failure.label}: launchctl {failure.action} "
            + ui_text("退出码", "exit code", lang) + f" {failure.returncode}: "
            + (first_line[0][:500] if first_line else ui_text("未提供错误详情", "No error details provided", lang))
        )
    return ui_text("后台服务无法启动。请重试。", "Background services could not start. Please retry.", lang) + (
        "\n" + "\n".join(details)
    ) if details else ""


def write_startup_failure(support: Path, reason: str) -> None:
    path = support / "startup-failure.json"
    if not reason:
        path.unlink(missing_ok=True)
        return
    support.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump({"reason": reason, "time": time.time()}, stream, ensure_ascii=False)
    temporary.replace(path)


def read_startup_failure(support: Path) -> str:
    try:
        value = json.loads((support / "startup-failure.json").read_text(encoding="utf-8"))
        if isinstance(value, dict):
            reason = value.get("reason", "")
            return reason if isinstance(reason, str) else ""
    except (OSError, ValueError, TypeError):
        pass
    return ""


def should_open_window(login: bool, setup_required: bool) -> bool:
    return not login or setup_required


def send_command(command: str, support: Path | None = None) -> None:
    if command not in {"open", "quit", "retry"}:
        raise ValueError("未知外壳命令")
    directory = (support or macos_launchd.app_support_dir()) / "shell-requests"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # 同一动作合并；主线程消费，避免 reopen 与第二实例同时开出多个窗口。
    (directory / command).touch(mode=0o600)
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        try:
            from Foundation import NSDistributedNotificationCenter

            NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
                COMMAND_NOTIFICATION, str(directory.parent), None, True,
            )
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("唤醒菜单栏失败，将由定时器消费请求")


@lru_cache(maxsize=1)
def _command_observer_class():
    from Foundation import NSObject

    class CallPilotShellObserver(NSObject):
        def receive_(self, notification):
            self.callback()

    return CallPilotShellObserver


def observe_commands(callback, support: Path | None = None):
    from Foundation import NSDistributedNotificationCenter, NSNotificationSuspensionBehaviorDeliverImmediately

    observer = _command_observer_class().alloc().init()
    observer.callback = callback
    NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(
        observer, "receive:", COMMAND_NOTIFICATION, str(support or macos_launchd.app_support_dir()),
        NSNotificationSuspensionBehaviorDeliverImmediately,
    )
    return observer


def take_commands(support: Path | None = None) -> set[str]:
    directory = (support or macos_launchd.app_support_dir()) / "shell-requests"
    commands = set()
    for command in ("open", "quit", "retry"):
        path = directory / command
        try:
            fresh = time.time() - path.stat().st_mtime < 60
            path.unlink()
            if fresh:
                commands.add(command)
        except FileNotFoundError:
            pass
    return commands


def fetch_meta(url: str, *, tray: bool = False) -> dict:
    try:
        suffix = "?tray=1" if tray else ""
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/meta{suffix}", timeout=0.5) as response:
            data = json.loads(response.read(65536))
        # 不能把占用端口的任意 HTTP 200 当作 CallPilot。
        if isinstance(data, dict) and isinstance(data.get("setup_required"), bool) and "hardware" in data:
            return data
    except (OSError, ValueError):
        pass
    return {}


def quit_message(meta: dict, lang: str = "zh") -> str:
    warning = ui_text(
        "正在通话中，退出会结束当前通话。\n", "A call is in progress. Quitting will end it.\n", lang,
    ) if meta.get("call_active") else ""
    if not meta:
        warning = ui_text("暂时无法确认通话状态，可能有通话正在进行。\n",
                          "Call status is unavailable. A call may be in progress.\n", lang)
    return warning + ui_text(
        "退出后将不再接打电话。", "CallPilot will no longer make or receive calls after quitting.", lang,
    )


def quit_services(
    confirmed: bool,
    layout: macos_launchd.LaunchdLayout,
    close_window: Callable[[], None],
    quit_tray: Callable[[], None],
    runner: Callable = subprocess.run,
    *, lang: str = "zh",
) -> bool:
    if not confirmed:
        return False
    for label in (macos_launchd.APP_LABEL, macos_launchd.BRIDGE_LABEL):
        target = f"{layout.gui_domain}/{label}"
        macos_launchd._run_launchctl(["bootout", target], runner)
        # bootout 移除任务后 KeepAlive 无法拉回；确认任务确实已移除。
        deadline = time.monotonic() + 3
        while True:
            state = macos_launchd._run_launchctl(["print", target], runner)
            if state.returncode:
                if f'Could not find service "{label}"' in (state.stderr or ""):
                    break
                raise RuntimeError(ui_text(
                    "无法确认后台服务已经停止；菜单栏将保留，请重试。",
                    "Could not confirm the services stopped. CallPilot will stay open. Please retry.", lang,
                ))
            if time.monotonic() >= deadline:
                raise RuntimeError(ui_text("后台服务尚未停止，请稍后重试。",
                                           "Background services have not stopped yet. Please retry shortly.", lang))
            time.sleep(0.1)
    close_window()
    quit_tray()
    return True


def save_geometry(path: Path, geometry: Geometry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(geometry), encoding="utf-8")
    temporary.replace(path)


def restore_geometry(path: Path, screens: list[Geometry]) -> Geometry:
    if not screens:
        screens = [(0, 0, 1440, 900)]
    sx, sy, sw, sh = screens[0]
    geometry = (sx + max(0, (sw - 1100) // 2), sy + max(0, (sh - 780) // 2), 1100, 780)
    try:
        value = json.loads(path.read_text())
        if isinstance(value, list) and len(value) == 4 and all(type(v) is int for v in value):
            if value[2] > 0 and value[3] > 0:
                geometry = tuple(value)  # type: ignore[assignment]
    except (OSError, ValueError):
        pass
    x, y, w, h = geometry
    # 选择交叠面积最大的可用屏幕；屏幕拔掉后回收到主屏。
    sx, sy, sw, sh = max(screens, key=lambda s: (
        max(0, min(x + w, s[0] + s[2]) - max(x, s[0]))
        * max(0, min(y + h, s[1] + s[3]) - max(y, s[1]))
    ))
    w, h = min(max(w, 480), sw), min(max(h, 360), sh)
    return max(sx, min(x, sx + sw - w)), max(sy, min(y, sy + sh - h)), w, h


def failure_reason(logs: str, lang: str = "zh") -> str:
    lower = logs.lower()
    if "address already in use" in lower or "errno 48" in lower or "errno 98" in lower:
        return ui_text("最近日志记录了服务端口被占用。请检查是否有其他程序占用端口，然后重试。",
                       "Recent logs show the service port is in use. Close the other program and retry.", lang)
    if "traceback (most recent call last)" in lower:
        return ui_text("最近日志记录了服务错误退出。请重试或把诊断信息交给支持人员。",
                       "Recent logs show the service crashed. Retry or share diagnostics with support.", lang)
    if any(word in lower for word in ("usberror", "未找到 usb", "未检测到 usb", "另一个 ec20", "no backend")):
        return ui_text("最近日志记录了 USB 桥异常或设备未连接。请检查 USB 连接后重试。",
                       "Recent logs show a USB bridge or connection problem. Check the USB connection and retry.", lang)
    return ui_text("服务未能及时启动，可能是端口占用、服务异常退出或 USB 桥异常。请重试。",
                   "The service did not start in time. The port may be in use, the service may have crashed, "
                   "or the USB bridge may be unavailable. Please retry.", lang)


def diagnostic_report(version: str, macos: str, states: dict, logs: dict, meta: dict) -> str:
    # 日志可能混入 provider 原文/任意密钥；只输出尾部每行的安全分类，绝不复制原文。
    safe_states = {}
    for label, state in states.items():
        safe_states[label] = re.findall(
            r"(?m)^\s*(?:state = (running|waiting|not running)|pid = (\d+)|last exit code = (\d+))\s*$",
            str(state),
        ) or "未加载或无法读取"
    safe_logs = {}
    for name, tail in logs.items():
        safe_logs[name] = [
            failure_reason(line) if failure_reason(line) != failure_reason("") else "[日志内容已隐去]"
            for line in str(tail).splitlines()[-30:]
        ]
    hardware = meta.get("hardware", {})
    # 原始 meta 还含向导令牌、provider 信息和设备标识，不可直接序列化。
    safe_hardware = {key: hardware[key] for key in (
        "usb_online", "modem_connected", "modem_resetting", "phone_connected",
    ) if isinstance(hardware, dict) and type(hardware.get(key)) is bool}
    return json.dumps({
        "app_version": version, "macOS": macos, "launchd": safe_states,
        "recent_log_tail_sanitized": safe_logs, "hardware": safe_hardware,
    }, ensure_ascii=False, indent=2)


def collect_diagnostics(
    layout: macos_launchd.LaunchdLayout, meta: dict, runner: Callable = subprocess.run,
    *, lang: str = "zh",
) -> tuple[str, str]:
    import plistlib

    version = "unknown"
    bundle = macos_launchd.bundle_root_for_executable(layout.executable)
    if bundle is not None:
        try:
            with (bundle / "Contents" / "Info.plist").open("rb") as stream:
                version = str(plistlib.load(stream).get("CFBundleShortVersionString", "unknown"))
        except (OSError, ValueError):
            pass

    states = {}
    for label in macos_launchd.LAUNCH_LABELS:
        try:
            result = macos_launchd._run_launchctl(["print", f"{layout.gui_domain}/{label}"], runner)
            states[label] = result.stdout or ""
        except (OSError, subprocess.SubprocessError):
            states[label] = ""
    logs = {}
    for name in ("launchd-app.err.log", "launchd-app.out.log", "launchd-bridge.err.log", "modem_usb_pty.log",
                 "launchd-tray.err.log", "app.log", "tray.log", "window.log"):
        try:
            with (layout.log_dir / name).open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 8192))
                logs[name] = stream.read(8192).decode("utf-8", "replace")
        except OSError:
            logs[name] = ""
    return failure_reason("\n".join(logs.values()), lang), diagnostic_report(
        version, platform.mac_ver()[0], states, logs, meta,
    )
