"""pytest 共享配置：让测试能导入 tests/fakes，并隔离会写盘/联网的默认配置。"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


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
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    from agentcall.agents.local_agent import reset_shared_speech_models
    from agentcall.rate_limit import reset_sms_rate_limit_state

    reset_sms_rate_limit_state()
    reset_shared_speech_models()
    yield
    reset_sms_rate_limit_state()
    reset_shared_speech_models()


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
