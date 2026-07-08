"""coreaudio 设备序位查找单测（monkeypatch list_devices，不碰真实 CoreAudio）。

核心不变量：find_output_index / default_output_index 返回的是「输出设备中的
序位」（与 ffmpeg audiotoolbox 的 -audio_device_index 编号一致），跳过只读
输入设备/无输出流设备——通用逻辑，不依赖任何具体机器的设备布局。
"""

from __future__ import annotations

from agentcall import coreaudio

# (数组序号, 设备ID, 名字, 输出流数) —— 刻意混入输入设备与虚拟设备
_DEVICES = [
    (0, 100, "U2790B", 1),          # 输出序位 0
    (1, 101, "AC Interface", 0),    # 输入设备（无输出流）→ 跳过
    (2, 102, "AS Interface", 1),    # 输出序位 1  ← EC20 声卡
    (3, 103, "内置麦克风", 0),        # 输入设备 → 跳过
    (4, 104, "BlackHole", 1),       # 输出序位 2（虚拟）
    (5, 105, "扬声器", 1),           # 输出序位 3
]


def test_find_output_index_returns_output_ordinal(monkeypatch):
    monkeypatch.setattr(coreaudio, "list_devices", lambda: list(_DEVICES))
    # 'AS Interface' 全数组序号是 2，但输出序位是 1（ffmpeg 只数有输出的设备）
    assert coreaudio.find_output_index("Interface") == 1
    assert coreaudio.find_output_index("扬声器") == 3
    assert coreaudio.find_output_index("BlackHole") == 2
    assert coreaudio.find_output_index("不存在") is None
    # 只读输入设备即便名字匹配也不返回（无输出流）
    assert coreaudio.find_output_index("麦克风") is None


def test_default_output_index_returns_output_ordinal(monkeypatch):
    monkeypatch.setattr(coreaudio, "list_devices", lambda: list(_DEVICES))
    # 默认输出设备 id=105（扬声器）→ 输出序位 3，而非全数组序号 5
    monkeypatch.setattr(coreaudio, "_resolve_default_output_id", lambda: 105)
    assert coreaudio.default_output_index() == 3
    monkeypatch.setattr(coreaudio, "_resolve_default_output_id", lambda: 102)
    assert coreaudio.default_output_index() == 1  # AS Interface
    monkeypatch.setattr(coreaudio, "_resolve_default_output_id", lambda: 999)
    assert coreaudio.default_output_index() is None  # 未匹配
