"""prompts 纯函数单测：机主/人设/外呼主题注入与关键指引片段。"""

from __future__ import annotations

from agentcall.prompts import (
    DEFAULT_OUTBOUND_TASK,
    agent_persona,
    build_instructions,
    opening_instructions,
    owner_name,
)


# ---- 机主与人设：config 读取与中性缺省 ----

def test_owner_and_persona_from_env(monkeypatch):
    monkeypatch.setenv("OWNER_NAME", " 田野 ")
    monkeypatch.setenv("AGENT_PERSONA", "数字分身")
    assert owner_name() == "田野"  # 两端空白被去掉
    assert agent_persona() == "数字分身"


def test_owner_and_persona_defaults(monkeypatch):
    monkeypatch.delenv("OWNER_NAME", raising=False)
    monkeypatch.delenv("AGENT_PERSONA", raising=False)
    assert owner_name() == "机主"
    assert agent_persona() == "AI 助理"


# ---- 系统提示词 ----

def test_outbound_instructions_inject_owner_persona_task():
    text = build_instructions("outbound", "田野", "数字分身", "查询本月话费")
    assert "田野的数字分身" in text
    assert "本通电话主题：查询本月话费" in text
    assert "不要问对方“有什么可以帮您”" in text
    # IVR 按键指引与收束规则（仅外呼有）
    assert "send_dtmf" in text
    assert "hangup_call" in text
    assert "【IVR 应对】" in text
    assert "【收束】" in text


def test_inbound_instructions_inject_owner_and_rules():
    text = build_instructions("inbound", "田野", "数字分身", DEFAULT_OUTBOUND_TASK)
    assert "田野的数字分身" in text
    assert "现在不方便接" in text
    assert "会转告田野" in text
    # 外呼专属片段不得出现在来电提示词里
    assert "本通电话主题" not in text
    assert "【IVR 应对】" not in text


def test_instructions_common_sections_present():
    """两个方向共享的日期/安全边界/工具指引都在。"""
    for direction in ("outbound", "inbound"):
        text = build_instructions(direction, "田野", "AI 助理", "随便")
        assert "当前真实日期时间是" in text
        assert "安全边界" in text
        assert "send_sms" in text
        assert "query_verification_code" in text


# ---- 开场白 ----

def test_outbound_opening_injects_owner_and_task():
    text = opening_instructions("outbound", "田野", "数字分身", "查询本月话费")
    assert "我是田野的数字分身" in text
    assert "田野让我打这个电话" in text
    assert "这次主要是查询本月话费" in text


def test_inbound_opening_injects_owner():
    text = opening_instructions("inbound", "田野", "数字分身", DEFAULT_OUTBOUND_TASK)
    assert "我是田野的数字分身" in text
    assert "田野现在不方便接" in text
    # 来电开场白不应带外呼主题
    assert DEFAULT_OUTBOUND_TASK not in text
