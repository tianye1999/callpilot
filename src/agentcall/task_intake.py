"""对话式外呼建单（WIL-120 三期 b）：聊天采集 → 任务包草稿。

机主在 Web 对话框里像聊天一样描述要办的事，文本模型追问补齐关键信息，
攒够了输出一份可直接拨打的草稿（号码 + 任务 + 场景 + task_package）。

设计取舍：

- **无服务端会话状态**：前端每轮把完整消息历史发来，本模块是纯函数式的
  单步推进（messages 进、reply/draft 出）。刷新页面即重来，不留悬挂状态。
- **fail-closed**：模型输出解析失败 → ready=False + 重试话术，绝不凭
  半份 JSON 出草稿；草稿的 task_package 复用 number_profiles 的宽松
  归一化（坏段丢弃），号码复用同一正则校验。
- **隐私**：对话内容是机主主动输入给自己系统的，不属于通话取证面——
  不落 events/metrics；服务端也不打日志正文（只记轮数与结果形态）。
- 非枚举原则：追问什么、怎么问全靠模型判断，这里只给目标描述。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import config
from .number_profiles import _normalize_task_package
from .prompt_gen import (
    call_text_model,
    parse_json_payload,
    select_text_model,
    text_backend_for_agent,
)

logger = logging.getLogger(__name__)

_INTAKE_TIMEOUT_SECONDS = 12.0  # 交互式：比 prompt_gen 的 5s 宽、比摘要的 30s 紧
# 建单是面向机主的多轮结构化对话，比摘要/裁判难——不跟 SUMMARY_MODEL 的便宜
# 默认档（2026-08-18 真机：gpt-4o-mini 追问蠢 + JSON 纪律差），各 provider 用强档。
_INTAKE_DEFAULT_BY_PROVIDER = {
    "openai": "gpt-4o", "qwen": "qwen-plus",
    "deepseek": "",  # select_text_model follows LOCAL_LLM_MODEL.
}
_MAX_MESSAGES = 40
_MAX_CONTENT_CHARS = 2000
# 追问阶段回复要短；出草稿那轮 JSON 较长（含 task_package），给足余量。
_MAX_TOKENS = 900

_SYSTEM = {
    "zh": (
        "你在帮机主{owner}把一通 AI 代打的外呼任务聊清楚。你和机主对话；"
        "聊清楚后由电话 AI 拿着任务资料去打这通电话。\n"
        "目标是攒齐：对方电话号码、要办的事（一句话）、以及一份任务资料——\n"
        "- negotiation（谈判要点/底牌：现价、目标、年限、竞品报价等，视任务而定）\n"
        "- preauth（预授权范围：电话 AI 只被授权在此范围内直接答应，如月费上限、"
        "一次性费用上限、合约期上限）\n"
        "- verification（核身信息：户名、地址、账号等，客服核身才用得上；"
        "机主不想给就不勉强，注明缺失即可）\n"
        "- blacklist（绝不同意的操作，如开通增值业务、改联系方式）\n"
        "号码规则：机主没给对方号码时，你**知道的公开客服号可以直接提议并确认**"
        "——比如美国运营商自家客服是 611、知名公司的官方客服热线你记得的就报出来"
        "（『AT&T 客服是 611，直接打这个可以吗？』），不要反过来让机主查；"
        "确实不知道或没把握的号码，诚实说不确定、请机主提供，**绝不编造号码**。\n"
        "核身信息（户名/地址/账号等）：**绝不主动索要**。最多在快收口时提一句"
        "『客服如果要核身可能用到户名或账号，现在不给也行』；机主没兴趣就立即"
        "作罢，缺着收口。\n"
        "追问方式：一次只问一两个最关键的缺口，别列问卷。**机主一旦表达开始的"
        "意思（「开始」「就这样」「够了」「打吧」等任何说法），本轮必须 ready=true"
        "出草稿，缺什么留空——再追问任何问题都是错误**。同样，你自己说出「开始/准备好了/马上安排」这类话"
        "的那一轮，必须同时 ready=true 带上 draft——说要开始却不出草稿是错误。"
        "机主说的约束类要求"
        "（如「别办理任何业务」「不要改套餐」）放进 blacklist，不要只写进 scenario。"
        "金额、期限等数字必须用机主原话，绝不替机主编造或取整。\n"
        "问机主问题时，尽量在 options 里给 2-4 个最可能的候选答案（每个几个字，"
        "可一键选择），机主也随时可以自己打字；开放性问题就不给 options。"
        "选项要具体可选（『611 客服』『我提供号码』），不要『其他』这种废选项。\n"
        "每轮只输出严格合法的 JSON，无任何多余文字：\n"
        '{{"reply": "对机主说的话", "options": ["候选1", "候选2"] 或 [], '
        '"ready": true/false, "draft": null 或 '
        '{{"number": "...", "task": "...", "label": "...", "scenario": "...", '
        '"task_package": {{"verification": {{...}}, "negotiation": {{...}}, '
        '"preauth": {{...}}, "blacklist": [...]}}}}}}\n'
        "ready=true 时 draft 必须完整可用：number 是可拨号码；task 一句话；"
        "label 是几个字的短名；scenario 是给电话 AI 的 1-3 句场景策略"
        "（含关键做法，如先转人工、排队要等）；task_package 各段能填则填。"
    ),
    "en": (
        "You are helping the owner {owner} spec out a phone call that an AI "
        "assistant will make on their behalf. You chat with the owner; the "
        "phone AI will then make the call with the dossier you assemble.\n"
        "You need: the target phone number, the task in one sentence, and a "
        "dossier —\n"
        "- negotiation (leverage & targets: current price, goal, tenure, "
        "competitor offers, as relevant)\n"
        "- preauth (what the phone AI may accept on its own: monthly cap, "
        "one-time-fee cap, contract-term cap)\n"
        "- verification (identity facts a rep may ask for: name, address, "
        "account number; skip anything the owner declines to share)\n"
        "- blacklist (actions never to agree to)\n"
        "Phone number rule: when the owner hasn't given the target number, "
        "**propose public numbers you know and confirm** — e.g. a US carrier's "
        "own customer service is 611, and well-known companies' official "
        "hotlines you remember (\"AT&T customer service is 611 — shall I just "
        "call that?\"); never bounce the lookup back to the owner. If you are "
        "not sure of a number, say so honestly and ask the owner for it — "
        "**never fabricate a number**.\n"
        "Verification facts (name/address/account): **never ask for them "
        "proactively**. At most, when about to finalize, mention once that "
        "\"the rep may ask for the account holder name or number — fine to "
        "skip for now\"; drop it immediately if the owner isn't interested.\n"
        "Ask for at most one or two missing essentials per turn — no "
        "questionnaires. **The moment the owner signals to start (\"go "
        "ahead\", \"that's enough\", \"just call\", any phrasing), this "
        "turn MUST return ready=true with the draft, leaving gaps empty — "
        "asking anything further is an error.** Likewise, any turn where YOU "
        "announce starting (\"okay, starting now\") must itself carry "
        "ready=true with the draft — announcing without a draft is an error. "
        "Constraints the owner states "
        "(\"don't change anything on the account\") go into blacklist, not "
        "only into the scenario prose. Numbers (prices, terms) must be the "
        "owner's own words — never invent or round them.\n"
        "When asking the owner a question, offer 2-4 likely answers in "
        "`options` (a few words each, one-tap choices); the owner can always "
        "type freely instead. Skip options for open-ended questions. Options "
        "must be concrete choices, never filler like \"other\".\n"
        "Every turn output strictly valid JSON and nothing else:\n"
        '{{"reply": "what you say to the owner", "options": ["choice1", '
        '"choice2"] or [], "ready": true/false, '
        '"draft": null or {{"number": "...", "task": "...", "label": "...", '
        '"scenario": "...", "task_package": {{"verification": {{...}}, '
        '"negotiation": {{...}}, "preauth": {{...}}, "blacklist": [...]}}}}}}\n'
        "When ready=true the draft must be usable as-is: a dialable number, a "
        "one-sentence task, a short label, a 1-3 sentence scenario strategy "
        "for the phone AI, and whatever dossier sections you gathered."
    ),
}

_REFORMAT_NUDGE = {
    "zh": "你刚才的输出不是约定的 JSON。把同样的意思重新只用约定的 JSON 结构输出，不要任何其他文字。",
    "en": (
        "Your last output was not the agreed JSON. Restate the same content "
        "strictly as the agreed JSON structure, with no other text."
    ),
}

_RETRY_REPLY = {
    "zh": "抱歉，我这边出了点岔子，麻烦把刚才的意思再说一遍。",
    "en": "Sorry, something glitched on my side — could you say that again?",
}


def _sanitize_messages(raw: Any) -> list[dict[str, str]] | None:
    """校验前端发来的消息历史；非法返回 None（端点层报 400）。"""
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_MESSAGES:
        return None
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            return None
        content = content.strip()
        if not content or len(content) > _MAX_CONTENT_CHARS:
            return None
        out.append({"role": role, "content": content})
    if out[-1]["role"] != "user":
        return None
    return out


# 模型产出的号码单独收严，**不复用** number_profiles._DIAL_NUMBER_RE（WIL-130）。
# 那条允许 * 与 #，是给人工手输预设留的；而 GSM 补充业务码形如 *21*<号码>#，
# 拨出去不是打电话，是在机主 SIM 上设置无条件呼叫转移——机主此后所有来电被转走
# 且几乎不会察觉。建单聊天恰恰是机主会粘贴外部文本的地方（「打发票上这个号码」），
# 所以这个字段是**间接可被攻击者影响**的，不能沿用人工输入那档宽松度。
# 下限 3 位：611 / 911 这类短号必须能过。
_LLM_DIAL_NUMBER_RE = re.compile(r"\+?[0-9]{3,20}")


def _sanitize_draft(raw: Any) -> dict[str, Any] | None:
    """草稿收口：号码/文本字段校验 + task_package 宽松归一化。"""
    if not isinstance(raw, dict):
        return None
    number = str(raw.get("number") or "").strip()
    task = " ".join(str(raw.get("task") or "").split())
    if not _LLM_DIAL_NUMBER_RE.fullmatch(number) or not task:
        return None
    return {
        "number": number,
        "task": task[:120],
        "label": " ".join(str(raw.get("label") or "").split())[:60],
        "scenario": " ".join(str(raw.get("scenario") or "").split())[:1200],
        "task_package": _normalize_task_package(raw.get("task_package")),
    }


def intake_step(
    messages: list[dict[str, str]], *, owner: str, lang: str = "zh"
) -> dict[str, Any]:
    """推进一轮建单对话（契约：绝不抛异常）。

    返回 ``{"ok", "reply", "ready", "draft", "error"}``；ready=True 时 draft
    保证可直接用于创建预设/拨打（号码已过拨号正则）。
    """
    lang = "en" if lang == "en" else "zh"
    try:
        provider = text_backend_for_agent()
        model = select_text_model(
            provider,
            config.get_str("INTAKE_MODEL")
            or _INTAKE_DEFAULT_BY_PROVIDER.get(provider, ""),
        )
        # 历史里的 assistant 消息重新包成 JSON 形态：模型在多轮后会模仿历史里
        # 自己的输出样式——若历史示范是裸文本，第二轮起就漂出 JSON 契约
        # （2026-08-18 真机复现：轮数=4 时返回 28-40 字符纯文本）。
        history = [
            m if m["role"] == "user" else {
                "role": "assistant",
                "content": json.dumps(
                    {"reply": m["content"], "options": [],
                     "ready": False, "draft": None},
                    ensure_ascii=False,
                ),
            }
            for m in messages
        ]
        chat = [
            {"role": "system", "content": _SYSTEM[lang].format(owner=owner)},
            *history,
        ]
        text, error = call_text_model(
            chat,
            provider=provider,
            model=model,
            timeout=_INTAKE_TIMEOUT_SECONDS,
            max_tokens=_MAX_TOKENS,
        )
        if error is not None:
            logger.warning("建单对话模型失败: %s", error)
            return {
                "ok": False,
                "reply": _RETRY_REPLY[lang],
                "options": [],
                "ready": False,
                "draft": None,
                "error": error,
            }
        data = parse_json_payload(text or "")
        reply = str((data or {}).get("reply") or "").strip()
        if not data or not reply:
            # 自动纠偏一次：把跑偏的输出塞回去，明确要求重新只出 JSON。
            logger.info(
                "建单对话输出跑偏（轮数=%d，长度=%d），纠偏重试一次",
                len(messages),
                len(text or ""),
            )
            correction = chat + [
                {"role": "assistant", "content": (text or "")[:500]},
                {"role": "user", "content": _REFORMAT_NUDGE[lang]},
            ]
            text, error = call_text_model(
                correction,
                provider=provider,
                model=model,
                timeout=_INTAKE_TIMEOUT_SECONDS,
                max_tokens=_MAX_TOKENS,
            )
            data = parse_json_payload(text or "") if error is None else None
            reply = str((data or {}).get("reply") or "").strip()
        if not data or not reply:
            logger.warning(
                "建单对话输出不可解析（轮数=%d，长度=%d，已纠偏重试）",
                len(messages),
                len(text or ""),
            )
            return {
                "ok": False,
                "reply": _RETRY_REPLY[lang],
                "options": [],
                "ready": False,
                "draft": None,
                "error": "unparseable_model_output",
            }
        draft = (
            _sanitize_draft(data.get("draft")) if data.get("ready") else None
        )
        raw_options = data.get("options")
        options = (
            [str(o).strip()[:60] for o in raw_options if str(o).strip()][:4]
            if isinstance(raw_options, list) and draft is None
            else []
        )
        # ready 但草稿不合格（号码非法等）→ 降级为未就绪，绝不放坏草稿过去。
        return {
            "ok": True,
            "reply": reply[:1000],
            "options": options,
            "ready": draft is not None,
            "draft": draft,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 —— 契约：绝不抛出
        logger.warning("建单对话异常: %s", exc)
        return {
            "ok": False,
            "reply": _RETRY_REPLY[lang],
            "options": [],
            "ready": False,
            "draft": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
