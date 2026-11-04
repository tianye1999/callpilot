"""pytest 共享配置：让测试能导入 tests/fakes，并隔离会写盘/联网的默认配置。"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

_SPEED_SWITCHES = (
    "LOCAL_TTS_STREAMING", "LOCAL_ASR_SEGMENTED", "LOCAL_FAST_OPENING", "LOCAL_LLM_PREWARM",
    "LOCAL_SPECULATIVE_LLM", "LOCAL_LATENCY_METRICS", "PARALLEL_AGENT_CONNECT",
)

_GUARDED_CALLPILOT_FILES = (
    "focus-window",
    "tray-version.json",
    "listen-window",
    "shell-requests",
    "startup-failure.json",
    "uninstall-ready",
    "legacy-cleanup.json",
)


def _snapshot_paths(root: Path, names) -> dict[str, int | None]:
    snapshot: dict[str, int | None] = {}
    for name in names:
        path = root / name
        try:
            snapshot[name] = path.stat().st_mtime_ns
        except FileNotFoundError:
            snapshot[name] = None
    return snapshot


@pytest.fixture(scope="session", autouse=True)
def _guard_real_callpilot_data_dir():
    """会话结束校验真实 CallPilot 数据目录未被测试改写；目录不存在则跳过。"""
    root = Path.home() / "Library" / "Application Support" / "CallPilot"
    if not root.is_dir():
        yield
        return
    before = _snapshot_paths(root, _GUARDED_CALLPILOT_FILES)
    yield
    after = _snapshot_paths(root, _GUARDED_CALLPILOT_FILES)
    changed = [name for name in _GUARDED_CALLPILOT_FILES if before[name] != after[name]]
    assert not changed, "测试会话改写了真实 CallPilot 数据目录: " + ", ".join(changed)


@pytest.fixture(autouse=True)
def _explicit_legacy_latency_mode(monkeypatch):
    """旧行为回归显式关闭；不让新默认提前触发真实 LLM/模型加载。

    功能专项测试可单独开启；产品默认集成测试必须用 product_latency_defaults 清除覆盖。
    """
    for key in _SPEED_SWITCHES:
        monkeypatch.setenv(key, "false")
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER_SECONDS", "0")
    # 管线测试用假的本地模型；产品默认 TTS 已改千问云（owner 11-04），默认值由注册表测试单独把关。
    monkeypatch.setenv("LOCAL_TTS_PROVIDER", "local")


@pytest.fixture
def product_latency_defaults(monkeypatch, _explicit_legacy_latency_mode):
    """验证实际注册表默认值，而非测试隔离层的旧行为设置。"""
    for key in (*_SPEED_SWITCHES, "LOCAL_TTS_MODEL", "LOCAL_TTS_SPEAKER_ID", "LOCAL_TURN_SILENCE",
                "LOCAL_SLOW_REPLY_FILLER_SECONDS", "LOCAL_SLOW_REPLY_FILLER", "LOCAL_TTS_PROVIDER"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_side_effects(tmp_path, monkeypatch):
    """全局隔离：通话记录写进临时目录；摘要默认关避免单测触网。

    需要覆盖的测试可自行 monkeypatch.setenv/delenv（测试级优先于本 fixture）。
    """
    monkeypatch.setenv("CALL_LOG_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(tmp_path / "number_profiles.json"))
    # 呼叫情报库（WIL-129）：路径隔离 + 默认关闭，防止单测误触 learner 的真实模型调用
    monkeypatch.setenv("CALL_PLAYBOOKS_FILE", str(tmp_path / "call_playbooks.json"))
    monkeypatch.setenv("CALL_PLAYBOOKS_ENABLED", "false")
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    monkeypatch.setenv("UPDATE_CHECK_ENABLED", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    # API 测试也会走 USB 检测；默认假后端，专项测试再自行注入设备。
    import usb.core
    from serial.tools import list_ports

    from agentcall import modem_hardware
    from agentcall.web import server

    monkeypatch.setattr(usb.core, "find", lambda **kwargs: [] if kwargs.get("find_all") else None)
    monkeypatch.setattr(list_ports, "comports", lambda: [])
    monkeypatch.setattr(server, "_detect_quectel_usb_system_profiler", lambda: False)
    monkeypatch.setattr(modem_hardware, "STATE_PATH", tmp_path / "modem-state.json")
    for key in ("CALLPILOT_BRIDGE_RESET_ON_START", "CALLPILOT_BRIDGE_HOLD_ON_PCM_STALL",
                "MODEM_PCM_UPLINK_HOLD_MS"):
        monkeypatch.delenv(key, raising=False)
    from agentcall.agents.local_agent import reset_shared_speech_models
    from agentcall.rate_limit import reset_sms_rate_limit_state

    reset_sms_rate_limit_state()
    reset_shared_speech_models()
    yield
    reset_sms_rate_limit_state()
    reset_shared_speech_models()


@pytest.fixture(autouse=True)
def _isolate_macos_runtime_dirs(tmp_path, monkeypatch):
    """把 bundled macOS 用户目录入口重定向到本次测试的临时目录。

    打包版分支经 ``macos_launchd.app_support_dir()`` / ``launch_agents_dir()`` 解析
    ``~/Library/...``；不重定向会在本机真实 CallPilot 目录写 focus-window 等文件。
    ``app_support_dir`` 走项目已有 ``AGENTCALL_APP_SUPPORT_DIR`` 覆写，显式清空该变量
    的测试（如 display_branding）恢复原始默认解析；``launch_agents_dir`` 无覆写变量，
    直接打桩到 tmp，并保留显式 ``home`` 参数的语义。
    """
    from agentcall import macos_launchd

    support = tmp_path / "app-support"
    monkeypatch.setenv("AGENTCALL_APP_SUPPORT_DIR", str(support))

    original_launch_agents = macos_launchd.launch_agents_dir

    def scoped_launch_agents(home=None):
        if home is not None:
            return original_launch_agents(home)
        return tmp_path / "LaunchAgents"

    monkeypatch.setattr(macos_launchd, "launch_agents_dir", scoped_launch_agents)


@pytest.fixture
def dtmf_judge_lifecycle(monkeypatch):
    """Drain real judge threads and check the shared slots, without replacing the semaphore."""
    from agentcall import dtmf_judge

    def judge_threads():
        return [thread for thread in threading.enumerate() if thread.name.startswith("dtmf-judge-")]

    def assert_idle():
        assert not judge_threads(), "DTMF judge threads leaked across tests"
        acquired = 0
        try:
            for _ in range(dtmf_judge._MAX_CONCURRENT_MODEL_CALLS):
                assert dtmf_judge._MODEL_CALL_SLOTS.acquire(blocking=False), "DTMF model slot leaked"
                acquired += 1
        finally:
            for _ in range(acquired):
                dtmf_judge._MODEL_CALL_SLOTS.release()

    assert_idle()
    judges = []
    original_init = dtmf_judge.DtmfJudge.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        judges.append(self)

    monkeypatch.setattr(dtmf_judge.DtmfJudge, "__init__", tracked_init)
    yield
    for judge in judges:
        judge.stop(join_timeout=0)
    deadline = time.monotonic() + 5.0
    for thread in judge_threads():
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    assert_idle()
