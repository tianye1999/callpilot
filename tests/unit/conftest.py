"""单测硬件标记隔离，不能清除本机真实模组的恢复提示。"""

import pytest


@pytest.fixture(autouse=True)
def isolate_power_cycle_marker(monkeypatch, tmp_path):
    from agentcall import modem_hardware

    monkeypatch.setattr(modem_hardware, "NEEDS_POWER_CYCLE_PATH", tmp_path / "power-cycle.json", raising=False)
