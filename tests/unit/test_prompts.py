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
    monkeypatch.setenv("OWNER_NAME", " 李明 ")
    monkeypatch.setenv("AGENT_PERSONA", "数字分身")
    assert owner_name() == "李明"  # 两端空白被去掉
    assert agent_persona() == "数字分身"


def test_owner_and_persona_defaults(monkeypatch):
    monkeypatch.delenv("OWNER_NAME", raising=False)
    monkeypatch.delenv("AGENT_PERSONA", raising=False)
    assert owner_name() == "机主"
    assert agent_persona() == "AI 助理"


# ---- 系统提示词 ----

def test_outbound_instructions_inject_owner_persona_task():
    text = build_instructions("outbound", "李明", "数字分身", "查询本月话费")
    assert "李明的数字分身" in text
    assert "本通电话主题：查询本月话费" in text
    assert "不要问对方“有什么可以帮您”" in text
    # IVR 按键指引与收束规则（仅外呼有）
    assert "send_dtmf" in text
    assert "hangup_call" in text
    assert "【IVR 应对】" in text
    assert "【必须主动挂断】" in text


def test_winddown_instructions_bilingual():
    from agentcall.prompts import winddown_instructions
    assert "告别" in winddown_instructions("zh") and "再见" in winddown_instructions("zh")
    en = winddown_instructions("en")
    assert "goodbye" in en.lower() and "end the call" in en.lower()


def test_inbound_instructions_inject_owner_and_rules():
    text = build_instructions("inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK)
    assert "李明的数字分身" in text
    assert "现在不方便接" in text
    assert "会转告李明" in text
    # 外呼专属片段不得出现在来电提示词里
    assert "本通电话主题" not in text
    assert "【IVR 应对】" not in text


def test_instructions_common_sections_present():
    """两个方向共享的日期/安全边界/工具指引都在。"""
    for direction in ("outbound", "inbound"):
        text = build_instructions(direction, "李明", "AI 助理", "随便")
        assert "当前真实日期时间是" in text
        assert "安全边界" in text
        assert "send_sms" in text
        assert "query_verification_code" in text


# ---- 开场白 ----

def test_outbound_opening_injects_owner_and_task():
    text = opening_instructions("outbound", "李明", "数字分身", "查询本月话费")
    assert "我是李明的数字分身" in text
    assert "李明让我打这个电话" in text
    assert "这次主要是查询本月话费" in text


def test_inbound_opening_injects_owner():
    text = opening_instructions("inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK)
    assert "我是李明的数字分身" in text
    assert "李明现在不方便接" in text
    # 来电开场白不应带外呼专属措辞
    assert "这次主要是" not in text


# ---- 无预设任务（空 task）：不塞元指令、强化「你是主叫不是客服」----

def test_outbound_empty_task_uses_no_agenda_frame():
    text = build_instructions("outbound", "李明", "数字分身", "", "zh")
    assert "本通电话主题：" not in text          # 不硬塞主题行
    assert "没有预设具体事项" in text            # 走优雅兜底
    assert "绝不要充当客服" in text              # 强化主叫身份
    assert "不要问对方“有什么可以帮您”" in text
    en = build_instructions("outbound", "Alex", "AI assistant", "", "en")
    assert "Topic of this call:" not in en
    assert "no preset agenda" in en
    assert "never act like\ncustomer service" in en or "customer service" in en


def test_outbound_empty_task_opening_no_meta():
    text = opening_instructions("outbound", "李明", "数字分身", "", "zh")
    assert "有件事想跟您沟通一下" in text        # 空任务用自然措辞
    assert "这次主要是" not in text              # 不注入空/元任务
    en = opening_instructions("outbound", "Alex", "AI assistant", "", "en")
    assert "There's something I'd like to go over" in en
    assert "It's mainly about" not in en


# ---- 多语言（AGENT_LANGUAGE=en）----

def test_normalize_lang_falls_back_to_zh():
    from agentcall.prompts import normalize_lang
    assert normalize_lang("en") == "en"
    assert normalize_lang("EN") == "en"
    assert normalize_lang("zh") == "zh"
    assert normalize_lang("fr") == "zh"   # 未支持语言回退
    assert normalize_lang(None) == "zh"
    assert normalize_lang("") == "zh"


def test_english_build_instructions_are_english():
    from agentcall.prompts import build_instructions
    out = build_instructions("outbound", "Alex", "AI assistant", "confirm a time", "en")
    assert "You are Alex's AI assistant" in out
    assert "confirm a time" in out
    assert "send_dtmf" in out          # IVR 指引仍在
    assert "hangup_call" in out
    assert "机主" not in out and "你是" not in out   # 无中文残留
    inb = build_instructions("inbound", "Alex", "AI assistant", "", "en")
    assert "answering an incoming call for Alex" in inb
    assert "机主" not in inb


def test_english_opening_instructions_are_english():
    from agentcall.prompts import opening_instructions
    out = opening_instructions("outbound", "Alex", "AI assistant", "a delivery", "en")
    assert "this is Alex's AI assistant" in out
    assert "开场白" not in out


def test_owner_persona_fallback_per_language(monkeypatch):
    from agentcall import prompts
    monkeypatch.delenv("OWNER_NAME", raising=False)
    monkeypatch.delenv("AGENT_PERSONA", raising=False)
    monkeypatch.setattr(prompts.config, "get_str", lambda k, *a, **kw: "")
    assert prompts.owner_name("en") == "the owner"
    assert prompts.owner_name("zh") == "机主"
    assert prompts.agent_persona("en") == "AI assistant"
    assert prompts.agent_persona("zh") == "AI 助理"


def test_agent_language_reads_config(monkeypatch):
    from agentcall import prompts, config
    monkeypatch.setenv("AGENT_LANGUAGE", "en")
    assert prompts.agent_language() == "en"
    monkeypatch.setenv("AGENT_LANGUAGE", "zh")
    assert prompts.agent_language() == "zh"
