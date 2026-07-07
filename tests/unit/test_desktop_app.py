"""desktop_app 桌面窗口单测：fake webview/subprocess/urllib，不开真窗口、不起真进程。"""

from __future__ import annotations

import sys
import types
import urllib.error
from pathlib import Path

import pytest

# desktop_app.py 位于项目根（不在 src 包内），手动加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import desktop_app

_ENV_VARS = [
    "AGENTCALL_WEB_URL",
    "AGENTCALL_PROBE_TIMEOUT",
    "AGENTCALL_STARTUP_WAIT",
    "AGENTCALL_POLL_INTERVAL",
    "AGENTCALL_PYTHON",
    "AGENTCALL_APP_SCRIPT",
    "AGENTCALL_CONSOLE_LOG",
    "AGENTCALL_WINDOW_WIDTH",
    "AGENTCALL_WINDOW_HEIGHT",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """清掉宿主环境可能带入的 AGENTCALL_* 变量，保证测试确定性。"""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---- 测试替身 ----

class FakeResponse:
    """模拟 urlopen 返回的 2xx 响应（支持 with 语法）。"""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_fake_webview() -> types.ModuleType:
    """构造可注入 sys.modules 的 fake webview 模块，记录开窗调用。"""
    mod = types.ModuleType("webview")
    mod.windows = []
    mod.start_count = 0

    def create_window(title, url=None, html=None, **kwargs):
        mod.windows.append({"title": title, "url": url, "html": html, "kwargs": kwargs})

    def start(*args, **kwargs):
        mod.start_count += 1

    mod.create_window = create_window
    mod.start = start
    return mod


def _forbid_popen(monkeypatch):
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("不应调用 subprocess.Popen")

    monkeypatch.setattr(desktop_app.subprocess, "Popen", boom)


def _no_sleep(monkeypatch):
    monkeypatch.setattr(desktop_app.time, "sleep", lambda _s: None)


# ---- probe_service ----

def test_probe_service_ok(monkeypatch):
    seen = {}

    def fake_urlopen(url, timeout=None):
        seen["url"] = url
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(desktop_app.urllib.request, "urlopen", fake_urlopen)
    assert desktop_app.probe_service("http://127.0.0.1:8000/api/meta", timeout=2.0)
    assert seen == {"url": "http://127.0.0.1:8000/api/meta", "timeout": 2.0}


def test_probe_service_down(monkeypatch):
    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(desktop_app.urllib.request, "urlopen", fake_urlopen)
    assert not desktop_app.probe_service("http://127.0.0.1:8000/api/meta")


# ---- wait_service_ready ----

def test_wait_service_ready_becomes_ready(monkeypatch):
    results = iter([False, False, True])
    monkeypatch.setattr(
        desktop_app, "probe_service", lambda url, timeout=2.0: next(results)
    )
    _no_sleep(monkeypatch)
    assert desktop_app.wait_service_ready("http://x/api/meta", max_wait=10.0, interval=0)


def test_wait_service_ready_timeout(monkeypatch):
    monkeypatch.setattr(desktop_app, "probe_service", lambda url, timeout=2.0: False)
    _no_sleep(monkeypatch)
    assert not desktop_app.wait_service_ready("http://x/api/meta", max_wait=0)


# ---- ensure_service_running ----

def test_ensure_already_running(monkeypatch):
    monkeypatch.setattr(desktop_app, "probe_service", lambda url, timeout=2.0: True)
    _forbid_popen(monkeypatch)
    assert desktop_app.ensure_service_running("http://x/api/meta") == "already"


def test_ensure_starts_service(monkeypatch, tmp_path):
    state = {"up": False}
    monkeypatch.setattr(
        desktop_app, "probe_service", lambda url, timeout=2.0: state["up"]
    )
    _no_sleep(monkeypatch)

    popen_calls = []

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, start_new_session=False):
        popen_calls.append(
            {
                "argv": argv,
                "cwd": cwd,
                "stdout_name": getattr(stdout, "name", None),
                "stderr": stderr,
                "start_new_session": start_new_session,
            }
        )
        state["up"] = True
        return types.SimpleNamespace(pid=4242)

    monkeypatch.setattr(desktop_app.subprocess, "Popen", fake_popen)

    log = tmp_path / "console.log"
    result = desktop_app.ensure_service_running(
        "http://x/api/meta",
        python_exe="/fake/.venv/bin/python",
        app_script="/fake/app.py",
        log_path=log,
        max_wait=5.0,
    )

    assert result == "started"
    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["argv"] == ["/fake/.venv/bin/python", "/fake/app.py"]
    assert call["cwd"] == str(desktop_app.PROJECT_ROOT)
    assert call["stdout_name"] == str(log)  # stdout 重定向到日志文件
    assert call["stderr"] == desktop_app.subprocess.STDOUT
    assert call["start_new_session"] is True
    assert log.exists()


def test_ensure_popen_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_app, "probe_service", lambda url, timeout=2.0: False)

    def fake_popen(*args, **kwargs):
        raise FileNotFoundError("no such interpreter")

    monkeypatch.setattr(desktop_app.subprocess, "Popen", fake_popen)
    result = desktop_app.ensure_service_running(
        "http://x/api/meta", log_path=tmp_path / "console.log"
    )
    assert result == "failed"


def test_ensure_wait_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_app, "probe_service", lambda url, timeout=2.0: False)
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        desktop_app.subprocess,
        "Popen",
        lambda *a, **kw: types.SimpleNamespace(pid=1),
    )
    result = desktop_app.ensure_service_running(
        "http://x/api/meta", log_path=tmp_path / "console.log", max_wait=0
    )
    assert result == "failed"


# ---- main() 三分支 ----

def test_main_service_already_running(monkeypatch):
    """分支一：服务已在运行 → 直接开窗指向服务地址，不拉起进程。"""
    fake = make_fake_webview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(
        desktop_app.urllib.request, "urlopen", lambda url, timeout=None: FakeResponse()
    )
    _forbid_popen(monkeypatch)

    desktop_app.main()

    assert len(fake.windows) == 1
    win = fake.windows[0]
    assert win["title"] == "AgentCall — 数字分身"
    assert win["url"] == "http://127.0.0.1:8000"
    assert win["html"] is None
    assert win["kwargs"] == {"width": 1100, "height": 780}
    assert fake.start_count == 1


def test_main_starts_service_then_opens(monkeypatch, tmp_path):
    """分支二：服务未运行 → 拉起 app.py → 就绪后开窗。"""
    fake = make_fake_webview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setenv("AGENTCALL_CONSOLE_LOG", str(tmp_path / "console.log"))
    _no_sleep(monkeypatch)

    state = {"up": False}

    def fake_urlopen(url, timeout=None):
        if not state["up"]:
            raise urllib.error.URLError("connection refused")
        return FakeResponse()

    monkeypatch.setattr(desktop_app.urllib.request, "urlopen", fake_urlopen)

    popen_calls = []

    def fake_popen(argv, **kwargs):
        popen_calls.append(argv)
        state["up"] = True
        return types.SimpleNamespace(pid=4242)

    monkeypatch.setattr(desktop_app.subprocess, "Popen", fake_popen)

    desktop_app.main()

    assert len(popen_calls) == 1
    assert popen_calls[0][1].endswith("app.py")
    assert len(fake.windows) == 1
    win = fake.windows[0]
    assert win["url"] == "http://127.0.0.1:8000"
    assert win["html"] is None
    assert fake.start_count == 1


def test_main_startup_timeout_shows_error_window(monkeypatch, tmp_path):
    """分支三：拉起后一直不就绪 → 开错误提示窗（html=），含手动启动命令。"""
    fake = make_fake_webview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setenv("AGENTCALL_CONSOLE_LOG", str(tmp_path / "console.log"))
    monkeypatch.setenv("AGENTCALL_STARTUP_WAIT", "0")
    _no_sleep(monkeypatch)

    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(desktop_app.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        desktop_app.subprocess,
        "Popen",
        lambda *a, **kw: types.SimpleNamespace(pid=1),
    )

    desktop_app.main()

    assert len(fake.windows) == 1
    win = fake.windows[0]
    assert win["title"] == "AgentCall — 服务未启动"
    assert win["url"] is None
    assert win["html"] is not None
    assert "app.py" in win["html"]  # 提示了手动启动命令
    assert "console.log" in win["html"]  # 提示了日志位置
    assert fake.start_count == 1


# ---- pywebview 缺失 ----

def test_import_webview_missing(monkeypatch, capsys):
    """sys.modules['webview']=None 会让 import 失败 → 中文提示 + exit(1)。"""
    monkeypatch.setitem(sys.modules, "webview", None)
    with pytest.raises(SystemExit) as exc_info:
        desktop_app._import_webview()
    assert exc_info.value.code == 1
    assert "pywebview" in capsys.readouterr().err
