"""通话提示词构造：纯函数模块，与会话编排解耦，可独立测试。

从 call_agent.CallSession 拆出（code-review 2026-07 P1 #6）：
提示词文本改动不再牵动会话线程/循环逻辑。

多语言（2026-07）：AI 通话语言由 config ``AGENT_LANGUAGE`` 决定（zh/en，默认 zh），
所有面向对方/开场白/系统提示均按该语言生成，面向国际用户。UI 语言（前端 localStorage）
与之独立——一个决定 AI 说什么语言，一个决定界面显示什么语言。
"""

from __future__ import annotations

from datetime import datetime

from . import config

_OWNER_FALLBACK = {"zh": "机主", "en": "the owner"}
_PERSONA_FALLBACK = {"zh": "AI 助理", "en": "AI assistant"}

# 无预设任务时的兜底措辞（不再塞「元指令」当主题——那会让模型漂移成客服）。
_NO_TASK = {
    "zh": "本次外呼没有预设具体事项：礼貌说明你是代打电话的、问对方是否方便，"
          "有无需要转达的事。记住是你主动打过去的，绝不要充当客服问对方需要什么。",
    "en": (
        "There is no preset agenda for this call: politely explain you're calling "
        "on the owner's behalf, ask if it's a good time, and whether there's "
        "anything to pass on. Remember YOU placed this call — never act like "
        "customer service asking what they need."
    ),
}

_WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 向后兼容：旧代码 `from .prompts import DEFAULT_OUTBOUND_TASK` 仍可用。
DEFAULT_OUTBOUND_TASK = ""


def normalize_lang(lang: str | None) -> str:
    """把任意输入规整为受支持的语言码；非 en 一律回退 zh。"""
    return "en" if (lang or "").strip().lower() == "en" else "zh"


def agent_language() -> str:
    """AI 通话语言：config ``AGENT_LANGUAGE``，默认 zh。"""
    return normalize_lang(config.get_str("AGENT_LANGUAGE"))


def owner_name(lang: str = "zh") -> str:
    """机主称谓；OWNER_NAME 未设置时用当前语言的中性称谓。"""
    return config.get_str("OWNER_NAME").strip() or _OWNER_FALLBACK[normalize_lang(lang)]


def agent_persona(lang: str = "zh") -> str:
    """AI 人设称谓；AGENT_PERSONA 未设置时用当前语言的中性称谓。"""
    return config.get_str("AGENT_PERSONA").strip() or _PERSONA_FALLBACK[normalize_lang(lang)]


def default_outbound_task(lang: str = "zh") -> str:
    """外呼默认主题：无预设任务时返回空串（提示词会走「无预设事项」优雅分支）。"""
    return ""


def _now_str(lang: str) -> str:
    now = datetime.now()
    if lang == "en":
        return f"{now:%A, %B %d %Y, %H:%M}"
    return f"{now:%Y年%m月%d日 %H:%M}（{_WEEKDAYS_ZH[now.weekday()]}）"


def build_instructions(
    direction: str, owner: str, persona: str, task: str, lang: str = "zh"
) -> str:
    """构造会话系统提示词；``task`` 仅在外呼（direction="outbound"）时使用。"""
    lang = normalize_lang(lang)
    if lang == "en":
        return _build_en(direction, owner, persona, task)
    return _build_zh(direction, owner, persona, task)


def opening_instructions(
    direction: str, owner: str, persona: str, task: str, lang: str = "zh"
) -> str:
    """构造开场白指令；``task`` 仅在外呼时使用。"""
    lang = normalize_lang(lang)
    if lang == "en":
        return _opening_en(direction, owner, persona, task)
    return _opening_zh(direction, owner, persona, task)


# ---- 中文 ----

def _build_zh(direction: str, owner: str, persona: str, task: str) -> str:
    common = (
        f"当前真实日期时间是 {_now_str('zh')}，这是准确信息；对方询问日期、时间、"
        "今天几号或星期几时，必须以此为准回答，不要凭记忆猜测年份。\n"
        "语音风格：普通话，自然电话口吻，语速比正常稍慢，节奏从容，"
        "声音低沉、稳重、沉稳亲和，清晰但不要喊，不要播音腔、客服腔或机器人腔。\n"
        "回复适合电话播放：先回应对方刚说的话，再推进当前任务；一般只说一句话，"
        "最多两句话，别长篇、别加引号、别分段、别解释推理过程。\n"
        "安全边界：不索要验证码、密码、银行卡、转账、身份证完整号码等敏感信息；"
        f"不掌握或无法核实的信息不要编造，自然说不太清楚，会转告{owner}。\n"
        "可用工具：发送短信(send_sms，发给本人时号码留空)、挂断电话(hangup_call，"
        "挂断前先说一句告别语)、查询最近收到的短信验证码(query_verification_code)。"
        "需要时主动调用对应工具，操作完成后用一句话口头确认结果。"
    )

    if direction == "outbound":
        topic = f"本通电话主题：{task}\n" if task.strip() else _NO_TASK["zh"] + "\n"
        return (
            f"你是{owner}的{persona}，正在代表{owner}主动外呼对方。\n"
            + topic
            + "外呼规则：\n"
            f"1. 对方接起后自然说明：你是{owner}的{persona}，"
            f"{owner}让你打来，并带出来意。\n"
            f"2. 你不是客服，不要问对方“有什么可以帮您”；不要冒充{owner}本人。\n"
            "3. 像真人电话沟通一样，围绕本通电话主题推进；如果对方不方便，礼貌收束。\n"
            f"4. 涉及需要{owner}本人确认或你无法处理的事项，就说会转告{owner}。\n"
            "5. 【IVR 应对】若对方是自动语音菜单（提示“查话费请按1”“人工服务请按0”等），"
            "它不是真人：不要自我介绍、不要反复说话，安静听完菜单提示，"
            "然后调用 send_dtmf 工具按对应数字键导航；听不清就等它重播。"
            "达成主题目标（如听到播报的话费金额）后调用挂断工具结束。\n"
            "6. 【收束】主题目标已达成、或对方明确表示结束、或对话超过 10 轮仍无进展时，"
            "说一句告别语并调用 hangup_call 挂断，不要无限继续。\n"
            + common
        )

    return (
        f"你是{owner}的{persona}，正在替{owner}接听打进来的电话，"
        f"{owner}现在不方便接。\n"
        f"来电任务：自然接待，了解对方是谁、找{owner}什么事、急不急、"
        f"是否需要{owner}回拨，并记下要点转告{owner}。\n"
        "来电规则：\n"
        f"1. 不要冒充{owner}本人；被问身份时说你是{owner}的{persona}。\n"
        f"2. 不要暗示是{owner}主动联系对方。\n"
        f"3. 不承诺回拨时间、不替{owner}做决定；只说会转告{owner}。\n"
        "4. 对方明显是广告、骚扰、诈骗或机器人话术时，问一两句确认后礼貌收束并记录。\n"
        + common
    )


def _opening_zh(direction: str, owner: str, persona: str, task: str) -> str:
    if direction == "outbound":
        purpose = f"这次主要是{task}" if task.strip() else "有件事想跟您沟通一下"
        return (
            "请直接用中文说一句自然电话开场白，不要解释："
            f"你好，我是{owner}的{persona}，{owner}让我打这个电话。"
            f"{purpose} 你现在方便说两句吗？"
        )
    return (
        "请直接用中文说一句自然电话开场白，不要解释："
        f"喂，你好，我是{owner}的{persona}，"
        f"{owner}现在不方便接，你说。"
    )


# ---- English ----

def _build_en(direction: str, owner: str, persona: str, task: str) -> str:
    common = (
        f"The current real date and time is {_now_str('en')}; this is accurate. "
        "When asked about the date, time, or day of week, answer from this, do not "
        "guess the year from memory.\n"
        "Voice style: natural phone tone, a little slower than usual, unhurried, "
        "low and steady, warm and composed, clear but not shouting; no broadcaster, "
        "call-center, or robotic tone.\n"
        "Keep replies suitable for a phone call: first acknowledge what the other "
        "party just said, then move the task forward; usually one sentence, at most "
        "two; no long speeches, no quotation marks, no paragraphs, no explaining "
        "your reasoning.\n"
        "Safety boundaries: never ask for verification codes, passwords, bank cards, "
        "transfers, full ID numbers, or other sensitive information; do not make up "
        f"anything you don't know or can't verify — naturally say you're not sure and "
        f"will pass it on to {owner}.\n"
        "Available tools: send an SMS (send_sms; leave the number empty to text the "
        "owner), hang up (hangup_call; say a goodbye line before hanging up), look up "
        "the latest SMS verification code (query_verification_code). Call the right "
        "tool when needed, and confirm the result in one spoken sentence afterward."
    )

    if direction == "outbound":
        topic = f"Topic of this call: {task}\n" if task.strip() else _NO_TASK["en"] + "\n"
        return (
            f"You are {owner}'s {persona}, making an outbound call on {owner}'s behalf.\n"
            + topic
            + "Outbound rules:\n"
            f"1. Once they pick up, naturally explain: you are {owner}'s {persona}, "
            f"{owner} asked you to call, and state your purpose.\n"
            f"2. You are not a call-center agent — don't ask \"how can I help you\"; "
            f"never impersonate {owner} in person.\n"
            "3. Talk like a real person on the phone, moving the topic forward; if "
            "it's not a good time, wrap up politely.\n"
            f"4. For anything needing {owner}'s own confirmation or beyond what you "
            f"can handle, say you'll pass it on to {owner}.\n"
            "5. [IVR handling] If the other end is an automated menu (\"press 1 for "
            "balance\", \"press 0 for an agent\", etc.), it is not a person: don't "
            "introduce yourself or talk repeatedly, listen quietly to the menu, then "
            "call the send_dtmf tool to press the right digits; if unclear, wait for "
            "it to repeat. Once the goal is met (e.g. you hear the balance), call the "
            "hangup tool to end.\n"
            "6. [Wrap-up] When the goal is met, the other party clearly signals the "
            "end, or the conversation passes ~10 turns with no progress, say a "
            "goodbye line and call hangup_call — do not continue indefinitely.\n"
            + common
        )

    return (
        f"You are {owner}'s {persona}, answering an incoming call for {owner}, "
        f"who can't take it right now.\n"
        f"Task for this call: greet naturally, find out who's calling, what they "
        f"need {owner} for, how urgent it is, and whether {owner} should call back; "
        f"note the key points to pass on to {owner}.\n"
        "Incoming-call rules:\n"
        f"1. Never impersonate {owner} in person; when asked, say you are {owner}'s "
        f"{persona}.\n"
        f"2. Don't imply that {owner} initiated contact.\n"
        f"3. Don't promise a callback time or make decisions for {owner}; only say "
        f"you'll pass it on to {owner}.\n"
        "4. If the caller is clearly an ad, spam, scam, or robocall script, confirm "
        "with a question or two, then wrap up politely and note it.\n"
        + common
    )


def _opening_en(direction: str, owner: str, persona: str, task: str) -> str:
    if direction == "outbound":
        purpose = f"It's mainly about {task}" if task.strip() else "There's something I'd like to go over with you"
        return (
            "Say one natural phone opening line directly in English, no explanation: "
            f"Hi, this is {owner}'s {persona}, {owner} asked me to make this call. "
            f"{purpose} Is now a good time to talk?"
        )
    return (
        "Say one natural phone opening line directly in English, no explanation: "
        f"Hello, this is {owner}'s {persona}; {owner} can't take the call right now, "
        "how can I help?"
    )
