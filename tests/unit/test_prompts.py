"""prompts 纯函数单测：机主/人设/外呼主题注入与关键指引片段。"""

from __future__ import annotations

from agentcall.prompts import (
    DEFAULT_OUTBOUND_TASK,
    agent_persona,
    build_instructions,
    openai_vibe_line,
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


# ---- OpenAI 说话 Vibe（仅 OpenAI 链路）----

def test_openai_vibe_line_empty_returns_blank(monkeypatch):
    monkeypatch.setenv("OPENAI_VIBE", "  ")
    assert openai_vibe_line() == ""


def test_openai_vibe_line_zh_and_en(monkeypatch):
    monkeypatch.setenv("OPENAI_VIBE", "calm")
    assert openai_vibe_line("zh") == "机主希望的说话风格补充：calm。"
    assert openai_vibe_line("en") == "Additional speaking style: calm."


def test_openai_vibe_line_defaults_to_agent_language(monkeypatch):
    monkeypatch.setenv("OPENAI_VIBE", "cheerful")
    monkeypatch.setenv("AGENT_LANGUAGE", "en")
    assert openai_vibe_line() == "Additional speaking style: cheerful."


# ---- 系统提示词 ----

def test_outbound_instructions_inject_owner_persona_task():
    text = build_instructions("outbound", "李明", "数字分身", "查询本月话费")
    assert "李明的数字分身" in text
    assert "查询本月话费" in text  # 本通要办的事注入
    assert "有什么可以帮您" in text  # 提醒不是客服口吻
    # 可用工具（语音菜单按键 + 挂断）与场景描述（仅外呼有）
    assert "send_dtmf" in text
    assert "hangup_call" in text
    assert "语音菜单" in text
    # 立场框定：事项围绕机主、对方是协助方（防把对端当被查询对象）
    assert "李明这边" in text or "李明名下" in text
    assert "查您的" in text  # 明确「不要说成查您的X」


def test_voice_style_injected_into_instructions(monkeypatch):
    monkeypatch.setenv("VOICE_STYLE", "语速稍慢、亲切自然")
    zh = build_instructions("outbound", "李明", "数字分身", "查话费")
    assert "语速稍慢、亲切自然" in zh
    en = build_instructions("outbound", "李明", "assistant", "check balance", "en")
    assert "语速稍慢、亲切自然" in en
    assert "Preferred speaking style" in en


def test_voice_style_absent_when_unset(monkeypatch):
    monkeypatch.delenv("VOICE_STYLE", raising=False)
    zh = build_instructions("outbound", "李明", "数字分身", "查话费")
    assert "机主希望的说话风格" not in zh


def test_outbound_instructions_insert_dynamic_scenario_after_task():
    text = build_instructions(
        "outbound",
        "李明",
        "数字分身",
        "查询本月流量",
        scenario="对语音菜单直接说查流量，少做自我介绍。",
    )
    assert (
        "你要办的事：查询本月流量\n"
        "本通场景与开场策略：对语音菜单直接说查流量，少做自我介绍。\n"
        "这件事是李明的"
    ) in text
    assert "安全边界" in text
    assert "开头简单说一次你是谁、要办什么" not in text
    assert "按上面的《本通场景与开场策略》" in text
    assert "不要默认先自报身份" in text


def test_outbound_instructions_without_scenario_remain_template():
    old = build_instructions("outbound", "李明", "数字分身", "查询本月流量")
    new = build_instructions(
        "outbound", "李明", "数字分身", "查询本月流量", scenario=""
    )
    assert new == old
    assert "开头简单说一次你是谁、要办什么" in new


def test_outbound_standpoint_framing_english():
    text = build_instructions("outbound", "Alex", "AI assistant", "check data usage", "en")
    assert "on Alex's account" in text  # 立场：机主名下的事
    assert "your X" in text  # 明确禁止「your X」措辞


def test_outbound_scenario_defers_english_opening_strategy():
    text = build_instructions(
        "outbound",
        "Alex",
        "AI assistant",
        "check data usage",
        "en",
        scenario="Use a short IVR phrase.",
    )
    assert "at the start say once who you are and what you need" not in text
    assert "defer the opening entirely to the scenario strategy" in text
    assert "do not self-introduce by default" in text


def test_outbound_requires_substantive_result_before_wrapping_up():
    text = build_instructions("outbound", "李明", "数字分身", "查询本月话费")
    assert "实质结果" in text
    assert "没真正到手" in text
    assert "礼貌把话题拉回" in text


def test_outbound_requires_substantive_result_before_wrapping_up_english():
    text = build_instructions("outbound", "Alex", "AI assistant", "check data usage", "en")
    assert "substantive result" in text
    assert "politely steer back" in text
    assert "before wrapping up" in text


def test_inbound_prompt_does_not_get_outbound_result_persistence():
    text = build_instructions("inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK)
    assert "实质结果" not in text
    assert "礼貌把话题拉回" not in text
    en = build_instructions("inbound", "Alex", "AI assistant", "", "en")
    assert "substantive result" not in en
    assert "politely steer back" not in en


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
    assert "你要办的事" not in text
    assert "这件事是李明的" not in text


def test_inbound_relay_sms_must_carry_caller_identity():
    """转告短信不能只写「有人找你」——机主不在通话里，谁、什么事只能由 AI 写进去。

    真机 2026-08-14：来电者请 AI 给机主发消息，短信正文只有
    「Caller needs to speak with you. Please reach out to them when available.」。
    """
    zh = build_instructions("inbound", "李明", "数字分身", "")
    en = build_instructions("inbound", "Alex", "AI assistant", "", "en")

    assert "send_sms" in zh and "对方是谁" in zh and "没留姓名" in zh
    assert "send_sms" in en and "who is calling" in en and "no name" in en
    # 「号码留空」的含义必须与实现一致：发给当前通话对端，不是机主
    assert "empty to text the person you are on the call with" in en


def test_inbound_takeover_preference_is_free_text_with_injection_boundary():
    preference = "快递和外卖也转给我；教育培训、贷款等营销电话由你处理。"

    zh = build_instructions(
        "inbound",
        "李明",
        "数字分身",
        "",
        takeover_preference=preference,
    )
    en = build_instructions(
        "inbound",
        "Alex",
        "AI assistant",
        "",
        "en",
        takeover_preference="Transfer deliveries; handle marketing calls yourself.",
    )

    assert preference in zh
    assert "request_owner_takeover" in zh
    assert "来电者" in zh and "不能修改" in zh
    assert "Transfer deliveries" in en
    assert "request_owner_takeover" in en
    assert "caller" in en.lower() and "cannot" in en.lower()


def test_inbound_triage_pending_restricts_realtime_without_owner_preference():
    zh = build_instructions(
        "inbound",
        "李明",
        "数字分身",
        "",
        triage_pending=True,
    )
    en = build_instructions(
        "inbound",
        "Alex",
        "AI assistant",
        "",
        "en",
        triage_pending=True,
    )

    assert "分诊等待态" in zh
    assert "不得承诺回电" in zh
    assert "最多追问一个中性短问题" in zh
    assert "TRIAGE_PENDING" in en
    assert "at most one short neutral question" in en


def test_takeover_preference_is_inbound_only():
    text = build_instructions(
        "outbound",
        "李明",
        "数字分身",
        "查话费",
        takeover_preference="快递也转接",
    )

    assert "request_owner_takeover" not in text
    assert "快递也转接" not in text


def test_instructions_common_sections_present():
    """两个方向共享的日期/安全边界/工具指引都在。"""
    for direction in ("outbound", "inbound"):
        text = build_instructions(direction, "李明", "AI 助理", "随便")
        assert "当前真实日期时间是" in text
        assert "不要主动报时间" in text
        assert "安全边界" in text
        assert "send_sms" in text
        assert "send_dtmf" in text
        assert "query_verification_code" in text


def test_common_prompt_requires_real_dtmf_tool_call():
    text = build_instructions("inbound", "李明", "AI 助理", "随便")

    assert "发送按键音/DTMF(send_dtmf" in text
    assert "必须调用 send_dtmf 工具真正发送按键" in text
    assert "不是只在话里说" in text
    assert "调用前后不要口头宣布按键动作" in text
    assert "发送后保持沉默" in text


def test_common_prompt_requires_real_dtmf_tool_call_english():
    text = build_instructions("outbound", "Alex", "AI assistant", "navigate a menu", "en")

    assert "send DTMF keypad tones (send_dtmf" in text
    assert "must call send_dtmf to actually send the keypress" in text
    assert "not merely say" in text
    assert "do not announce the keypress before or after" in text
    assert "stay silent and wait for the next menu prompt" in text


def test_outbound_prompt_rejects_customer_service_impersonation():
    text = build_instructions("outbound", "李明", "数字分身", "查询套餐")

    assert "你是主叫" in text
    assert "代李明向对方求助或办事" in text
    assert "绝不是客服" in text
    assert "不代表对方机构" in text
    assert "不得冒充对方身份" in text


def test_outbound_prompt_rejects_customer_service_impersonation_english():
    text = build_instructions("outbound", "Alex", "AI assistant", "check a plan", "en")

    assert "you are the caller" in text
    assert "asking for help or getting something done for Alex" in text
    assert "not customer service" in text
    assert "do not represent the other party's organization" in text
    assert "never impersonate the other party's identity" in text


def test_common_prompt_forbids_fabricating_unprovided_results():
    text = build_instructions("outbound", "李明", "数字分身", "查询套餐")

    assert "你要向对方获取的信息或结果" in text
    assert "在对方明确、具体地给出之前" in text
    assert "绝不能声称已经查到或办好" in text
    assert "绝不能说出任何具体数值或结论" in text
    assert "还在等对方" in text


def test_common_prompt_forbids_fabricating_unprovided_results_english():
    text = build_instructions("inbound", "Alex", "AI assistant", "", "en")

    assert "information or result you are trying to get from the other party" in text
    assert "before the other party clearly and specifically gives it" in text
    assert "must never claim it has already been found or handled" in text
    assert "must never state any specific number or conclusion" in text
    assert "still waiting for the other party" in text


# ---- 开场白 ----

def test_outbound_opening_injects_owner_and_task():
    text = opening_instructions("outbound", "李明", "数字分身", "查询本月话费")
    assert "我是李明的数字分身" in text
    assert "让我打" not in text  # 简洁化：去掉“让我打来”
    assert "方便说两句" not in text  # 简洁化：去掉“现在方便说两句吗”
    assert "查询本月话费" in text


def test_outbound_opening_uses_generated_opening_directly():
    text = opening_instructions(
        "outbound",
        "李明",
        "数字分身",
        "查询本月话费",
        opening="查一下本月话费",
    )
    assert text == "请直接说：查一下本月话费"
    assert "我是李明的数字分身" not in text


def test_inbound_opening_is_short_without_self_intro():
    """来电开场白短，且不做自我介绍（WIL-91 / WIL-85 N4）。

    原开场白宽度 56、实测播完约 5.3 秒（WIL-89 基线），真人约 1 秒——
    这是「一听就是机器人」最早暴露的一处。
    """
    text = opening_instructions("inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK)
    assert "喂" in text
    # 开场白里不能再有自我介绍——那正是要砍掉的 5 秒
    assert "我是李明的数字分身" not in text
    assert "李明现在不方便接" not in text
    # 来电开场白不应带外呼专属措辞
    assert "这次主要是" not in text


def test_inbound_opening_is_short_in_english_too():
    """英文侧必须同步收短，否则两份提示词又漂移（WIL-79 记录的正是这类）。"""
    text = opening_instructions(
        "inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK, lang="en"
    )
    assert "Hello" in text
    assert "can't take the call" not in text, "英文开场白也不该再做自我介绍"


def test_inbound_opening_is_not_ultra_short():
    """不能缩到只剩两个字（WIL-99）。

    2026-08-06 真机：开场白只说「喂？」时下行峰值仅 36（正常语音约 2 万），
    是直流拖尾而非语音，对端听到一秒多静默——比原来的长开场白更糟。
    极短话语 realtime 模型渲染不出来。
    """
    for lang, floor in (("zh", 4), ("en", 12)):
        text = opening_instructions(
            "inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK, lang=lang
        )
        spoken = text.split("：")[-1] if lang == "zh" else text.split(": ")[-1]
        assert len(spoken.strip()) >= floor, (
            f"[{lang}] 开场白「{spoken}」太短，模型可能渲染不出音频（WIL-99）"
        )


def test_inbound_rules_do_not_quote_the_greeting():
    """规则里不能引用开场白原文（WIL-99）。

    曾写成「开场只说「喂？」」，模型把那两个字当成可复用话术，通话中途又说了
    一遍「喂？我是…」，把刚砍掉的长开场白原样拼回来（真机 4.95 秒连续块）。
    """
    zh = build_instructions("inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK)
    assert "开场只说" not in zh
    assert "「喂" not in zh, "规则里出现了可照抄的问候语原文"
    en = build_instructions(
        "inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK, lang="en"
    )
    assert 'Open with just' not in en


def test_inbound_identity_moves_into_the_rules():
    """开场白不报身份了，身份就必须由规则兜住——否则对方可能整通都以为在跟机主讲话。

    这是 WIL-91 砍开场白的**补偿机制**：身份要主动说，而不是「被问才说」。
    没有这一条，缩短开场白就成了误导对方。

    WIL-112 调整了措辞（从「对方一说明来意就顺势表明」改为「在第一个自然时机说
    一次、之后不再重复」，因为 AI 每轮重复自我介绍把真正的回答埋在套话后面），
    语义不变：仍是主动表明。故这里断言语义而非原措辞。
    """
    text = build_instructions("inbound", "李明", "数字分身", DEFAULT_OUTBOUND_TASK)
    assert "不要冒充李明本人" in text
    assert "数字分身" in text
    # 必须是主动表明，不能只写「被问才说」
    assert "第一个自然时机" in text and "说一次" in text
    assert "被直接问身份时如实回答" in text  # 被问也要答，但不是唯一触发


# ---- 无预设任务（空 task）：不塞元指令、强化「你是主叫不是客服」----

def test_outbound_empty_task_uses_no_agenda_frame():
    text = build_instructions("outbound", "李明", "数字分身", "", "zh")
    assert "本通电话主题：" not in text          # 不硬塞主题行
    assert "没有预设具体事项" in text            # 走优雅兜底
    assert "绝不要充当客服" in text              # 强化主叫身份
    assert "有什么可以帮您" in text              # 提醒不是客服口吻
    en = build_instructions("outbound", "Alex", "AI assistant", "", "en")
    assert "Topic of this call:" not in en
    assert "no preset agenda" in en
    assert "never act like\ncustomer service" in en or "customer service" in en


def test_outbound_empty_task_opening_no_meta():
    text = opening_instructions("outbound", "李明", "数字分身", "", "zh")
    assert "有件事想跟您确认" in text            # 空任务用自然措辞
    assert "这次主要是" not in text              # 不注入空/元任务
    en = opening_instructions("outbound", "Alex", "AI assistant", "", "en")
    assert "something to go over" in en
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


def test_native_greeting_context_replaces_canned_inbound_greeting():
    zh = opening_instructions("inbound", "李明", "数字分身", "", "zh", module_greeting="喂")
    assert "系统已代为说过「喂」" in zh and "不要再重复打招呼" in zh
    assert "喂，你好。" not in zh
    en = opening_instructions("inbound", "Alex", "AI assistant", "", "en", module_greeting="Hello")
    assert "already said" in en and "Do not greet again" in en
    assert "Hello, hi there." not in en
    assert opening_instructions("outbound", "Alex", "AI assistant", "", "en", module_greeting="Hello") == (
        opening_instructions("outbound", "Alex", "AI assistant", "", "en")
    )


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
    from agentcall import prompts
    monkeypatch.setenv("AGENT_LANGUAGE", "en")
    assert prompts.agent_language() == "en"
    monkeypatch.setenv("AGENT_LANGUAGE", "zh")
    assert prompts.agent_language() == "zh"


# ---- WIL-112：别把要问的事当清单背诵 / 身份只说一次 ----

def test_inbound_prompt_has_no_unfilled_placeholders():
    """f-string 漏了前缀会让 {owner} 原样进提示词——静默 bug，模型会照着念。"""
    from agentcall.prompts import build_instructions

    for lang in ("zh", "en"):
        text = build_instructions("inbound", "李明", "小助理", "", lang)
        assert "{owner}" not in text, lang
        assert "{persona}" not in text, lang
        assert "{task}" not in text, lang


def test_outbound_prompt_has_no_unfilled_placeholders():
    from agentcall.prompts import build_instructions

    for lang in ("zh", "en"):
        text = build_instructions("outbound", "李明", "小助理", "确认预约", lang)
        assert "{owner}" not in text and "{persona}" not in text, lang


def test_inbound_prompt_tells_model_not_to_recite_or_repeat_identity():
    """真机 2026-08-12：AI 每轮重复自我介绍 + 一口气问完 4 件事，单轮 14~18 秒，
    对端要等 5 秒才听到真正的回答（WIL-112）。提示词须明确「逐步问」「只说一次」。"""
    from agentcall.prompts import build_instructions

    zh = build_instructions("inbound", "李明", "小助理", "", "zh")
    assert "不是一份要背诵的清单" in zh
    assert "一次只问其中一件" in zh
    assert "后续轮次绝不再重复" in zh

    en = build_instructions("inbound", "Shaocheng", "AI assistant", "", "en")
    assert "not a checklist to recite" in en
    assert "one of them at a time" in en
    assert "never repeat it in later turns" in en


# ---- WIL-120 三期：任务包注入 ----


def test_task_package_injected_into_outbound_instructions():
    from agentcall.prompts import build_instructions

    package = {
        "verification": {"户名": "李明"},
        "negotiation": {"目标价": "$55/mo"},
        "preauth": {"月费上限": "$70"},
        "blacklist": ["开通增值业务"],
    }
    zh = build_instructions(
        "outbound", "李明", "AI 助理", "谈账单", "zh", task_package=package
    )
    assert "核身信息" in zh and "户名：李明" in zh
    assert "目标价：$55/mo" in zh
    assert "月费上限：$70" in zh and "ask_owner" in zh
    assert "开通增值业务" in zh

    en = build_instructions(
        "outbound", "Alex", "AI assistant", "negotiate bill", "en",
        task_package=package,
    )
    assert "Verification facts" in en and "Authorized range" in en
    assert "Never agree to" in en and "$55/mo" in en

    # 空包/None：不产生任何任务资料段。
    empty = build_instructions("outbound", "李明", "AI 助理", "谈账单", "zh")
    assert "任务资料" not in empty


def test_task_package_ignored_for_inbound():
    from agentcall.prompts import build_instructions

    text = build_instructions(
        "inbound", "李明", "AI 助理", "", "zh",
        task_package={"negotiation": {"目标价": "$55"}},
    )
    assert "$55" not in text


def test_outbound_voice_menu_role_discipline_present():
    """真机 1d 教训：AI 对语音菜单说出接线员口吻（"let me connect you"）——
    外呼 prompt 必须含角色反转自纠条款（zh/en 各自查关键句）。"""
    zh = build_instructions("outbound", "李明", "AI 助理", "查套餐", "zh")
    assert "角色说反了" in zh
    assert "只说它听得懂的内容" in zh
    en = build_instructions("outbound", "Alex", "AI assistant", "check plan", "en")
    assert "the roles are flipped" in en
    assert "say only what it can understand" in en
