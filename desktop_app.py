"""AgentCall 桌面窗口：pywebview 薄前端，"打开查看"网页仪表盘。

产品架构：桥+服务由系统常驻机制（macOS launchd / Windows 计划任务）托管，
本窗口只是查看入口，关窗不影响服务。
启动流程：
    1. 探测 ``<AGENTCALL_WEB_URL>/api/meta`` 判断服务是否已在运行；
    2. 在跑 → 直接开窗指向服务地址；
    3. 没跑 → 用项目 venv 的 python 拉起 app.py（stdout/stderr 追加到
       ``data/app_console.log``），轮询等待就绪后开窗；
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
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    webview = _import_webview()

    web_url = os.getenv("AGENTCALL_WEB_URL", DEFAULT_WEB_URL).rstrip("/")
    meta_url = f"{web_url}/api/meta"
    width = _env_int("AGENTCALL_WINDOW_WIDTH", 1100)
    height = _env_int("AGENTCALL_WINDOW_HEIGHT", 780)

    status = ensure_service_running(meta_url)
    if status == "failed":
        python_exe, app_script, log_path = _launch_config()
        # Windows PowerShell 5.1 不认 &&，且路径可能含空格；分号 + 引号两平台通吃
        start_cmd = f'cd "{PROJECT_ROOT}"; "{python_exe}" "{app_script}"'
        webview.create_window(
            ERROR_WINDOW_TITLE,
            html=build_error_html(web_url, log_path, start_cmd),
            width=width,
            height=height,
        )
    else:
        webview.create_window(WINDOW_TITLE, web_url, width=width, height=height)
    webview.start()


if __name__ == "__main__":
    main()
