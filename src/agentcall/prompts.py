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
            f"【立场】本通要办的都是{owner}的事、围绕{owner}名下的账户/情况。你是主叫，"
            f"对方（含客服、语音菜单）是你请求协助的对象——需要查询/办理时说"
            f"“帮我查/办{owner}这边的X”，绝不要说成“您的X”“查您的X”，"
            "别把对方当成被查询或被服务的人。\n"
            + topic
            + "外呼规则：\n"
            f"1. 对方接起后简洁说明你是{owner}的{persona}，并直接带出来意"
            "（不用加“让我打来”这类话）。\n"
            f"2. 你不是客服，不要问对方“有什么可以帮您”；不要冒充{owner}本人。\n"
            "3. 像真人电话沟通一样，围绕本通电话主题推进；如果对方不方便，礼貌收束。\n"
            f"4. 本通目标由你亲自达成（拿到信息/办成事）；只有确实需要{owner}本人"
            f"决定的才说转告{owner}——不要拿“转告{owner}”当搪塞而不去把事办完。\n"
            "5. 【IVR 应对】对方是自动语音菜单时它不是真人，不要自我介绍：\n"
            "   · 语音识别型（提示“请说出您的需求”“您请说”）——直接清晰说出关键词"
            "（如“查流量”“查话费”），不要按键；\n"
            "   · 数字按键型（提示“查话费按1”“人工按0”）——才调用 send_dtmf 按对应键。\n"
            "6. 【必须主动挂断】一旦达成目标、或对方/菜单开始重复循环、或你已尝试 2-3 次"
            "仍无进展，立刻说一句简短告别语并调用 hangup_call 结束——绝不要一直打转、"
            "绝不要等对方先挂。\n"
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


def winddown_instructions(lang: str = "zh") -> str:
    """到达外呼硬时限时的收尾道别指令（让 AI 说一句简短告别就结束）。"""
    if normalize_lang(lang) == "en":
        return (
            "Say one short goodbye line in English and end the call now, e.g.: "
            "Sorry to take your time, I'll let you go now — thank you, goodbye."
        )
    return (
        "请直接说一句简短的告别语就结束通话，例如："
        "不好意思占用您时间了，我这边先挂了，谢谢，再见。"
    )


def _opening_zh(direction: str, owner: str, persona: str, task: str) -> str:
    if direction == "outbound":
        purpose = f"这次主要是{task}" if task.strip() else "有件事想跟您沟通一下"
        return (
            "请直接用中文说一句自然电话开场白，不要解释："
            f"你好，我是{owner}的{persona}。"
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
            f"[Standpoint] Everything here is {owner}'s business, about {owner}'s own "
            f"account/situation. YOU are the caller; the other party (including agents "
            f"and voice menus) is whom you ask for help — say \"please look up/handle X "
            f"on {owner}'s account\", never phrase it as \"your X\" as if the other "
            "party were the one being queried or served.\n"
            + topic
            + "Outbound rules:\n"
            f"1. Once they pick up, briefly say you are {owner}'s {persona} and get "
            "straight to the point (no phrasing like \"asked me to call\").\n"
            f"2. You are not a call-center agent — don't ask \"how can I help you\"; "
            f"never impersonate {owner} in person.\n"
            "3. Talk like a real person on the phone, moving the topic forward; if "
            "it's not a good time, wrap up politely.\n"
            f"4. YOU accomplish the goal yourself (get the info / get it done); only "
            f"say you'll relay to {owner} for things that truly need {owner}'s own "
            f"decision — don't hide behind \"I'll tell {owner}\" instead of finishing.\n"
            "5. [IVR handling] An automated menu is not a person; don't introduce "
            "yourself:\n"
            "   - speech-recognition menu (\"tell me what you need\") — just clearly "
            "SAY the keyword (e.g. \"check data usage\"), do NOT press keys;\n"
            "   - digit menu (\"press 1 for balance\", \"press 0 for an agent\") — "
            "only then call send_dtmf for the right digit.\n"
            "6. [You MUST hang up] Once the goal is met, or the party/menu starts "
            "looping, or you've tried 2-3 times with no progress, immediately say a "
            "short goodbye and call hangup_call — never keep going in circles, never "
            "wait for them to hang up first.\n"
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
            f"Hi, this is {owner}'s {persona}. "
            f"{purpose} Is now a good time to talk?"
        )
    return (
        "Say one natural phone opening line directly in English, no explanation: "
        f"Hello, this is {owner}'s {persona}; {owner} can't take the call right now, "
        "how can I help?"
    )
