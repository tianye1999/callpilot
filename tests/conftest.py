"""pytest 共享配置：让测试能导入 tests/fakes，并隔离会写盘/联网的默认配置。"""

import sys
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
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    from agentcall.rate_limit import reset_sms_rate_limit_state

    reset_sms_rate_limit_state()
    yield
    reset_sms_rate_limit_state()
