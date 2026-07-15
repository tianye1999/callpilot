"""Outbound call scenario prompt generation.

The generated text is deliberately only a short per-call strategy paragraph.
Stable policy, safety, tools, and voice style remain in ``prompts.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Any

from . import config
from .prompts import agent_persona, normalize_lang, owner_name

logger = logging.getLogger(__name__)

MAX_SCENARIO_CHARS = 200
# 系统提示要求 opening 不超过30字；这里多留10字冗余，模型偶尔略超可接受，超40才丢弃回退模板。
MAX_OPENING_CHARS = 40
_CACHE_LIMIT = 64
_CACHE: "OrderedDict[tuple[str, str, str], tuple[str, str]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()

_DEFAULT_MODEL_BY_PROVIDER = {
    "qwen": "qwen-plus",
    "openai": "gpt-4o-mini",
}
_TEXT_BACKENDS = frozenset(_DEFAULT_MODEL_BY_PROVIDER)

_SYSTEM_PROMPT = {
    "zh": (
        "你是电话外呼策略助手。请只输出严格合法的JSON对象："
        '{"scenario":"不超过200字的场景与策略","opening":"不超过30字的第一句"}。'
        "用第二人称写给正在代机主打电话的语音助手。根据号码、事项和语言，"
        "自己判断对方可能是什么对象或热线，以及第一句怎么开场、是否需要自我介绍、"
        "遇到语音菜单该说短词还是对真人说整句、沟通要点是什么。"
        "判断对方是机构热线或自动系统时，opening只说需求，不做自我介绍；菜单场景用短词。"
        "判断对方是个人时，opening用礼貌完整句说明身份和来意。"
        "建议措辞必须使用用户消息里给定的机主称谓和助手称谓，严禁虚构任何身份、公司或人名。"
        "不要输出标题、项目符号或免责声明。"
    ),
    "en": (
        "You are an outbound phone-call strategy assistant. Output only a strictly "
        'valid JSON object: {"scenario":"strategy, <=200 chars","opening":"first '
        'sentence, <=30 chars"}. Write scenario in second person for a voice '
        "assistant calling on the owner's behalf. From the phone number, task, and "
        "language, infer what the other side may be and advise the first line, "
        "whether to introduce yourself, whether to use short menu phrases or full "
        "sentences with a person, and key communication points. If it seems to be "
        "an institution hotline or automated system, opening should state only the "
        "need, with no self-introduction; use short phrases for menus. If it seems "
        "to be a person, opening should be a polite full sentence with identity and "
        "purpose. Suggested "
        "wording must use the owner/persona labels supplied in the user message; "
        "never invent any identity, company, or person's name. No heading, bullets, "
        "or disclaimer outside the JSON."
    ),
}

_USER_TEMPLATE = {
    "zh": (
        "机主称谓：{owner}\n助手称谓：{persona}\n对方号码：{number}\n"
        "本通事项：{task}\n通话语言：中文"
    ),
    "en": (
        "Owner label: {owner}\nAssistant persona label: {persona}\n"
        "Other party number: {number}\nTask for this call: {task}\n"
        "Call language: English"
    ),
}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def text_backend_for_agent(provider: str | None = None) -> str:
    """Map the voice provider to the compatible auxiliary text backend.

    OpenAI uses its own chat completions endpoint. Other voice providers keep
    the historical Qwen text path; this preserves local/doubao behavior without
    changing their realtime agent implementations.
    """
    selected = (provider or config.get_str("AGENT_PROVIDER")).strip().lower()
    return "openai" if selected == "openai" else "qwen"


def select_text_model(provider: str, override: str | None = None) -> str:
    """Resolve an explicit model or the backend's inexpensive default."""
    selected = (provider or "").strip().lower()
    explicit = (override or "").strip()
    if explicit:
        return explicit
    return _DEFAULT_MODEL_BY_PROVIDER.get(selected, "qwen-plus")


def call_text_model(
    messages: list[dict[str, str]],
    *,
    provider: str,
    model: str,
    timeout: float,
    max_tokens: int = 160,
    hard_timeout: bool = True,
) -> tuple[str | None, str | None]:
    """Call one supported text backend with a hard caller-side timeout."""
    selected = (provider or "").strip().lower()
    if selected not in _TEXT_BACKENDS:
        return None, f"不支持的文本模型提供方: {selected or 'unknown'}"
    resolved_model = select_text_model(selected, model)
    if selected == "qwen" and not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        return None, "缺少环境变量 DASHSCOPE_API_KEY"
    if selected == "openai" and not os.environ.get("OPENAI_API_KEY", "").strip():
        return None, "缺少环境变量 OPENAI_API_KEY"

    def invoke() -> tuple[str | None, str | None]:
        try:
            if selected == "qwen":
                return _call_qwen_sync(messages, resolved_model)
            return _call_openai_sync(
                messages,
                resolved_model,
                timeout,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - daemon request must not escape
            return None, f"文本模型请求失败: {type(exc).__name__}: {exc}"

    if not hard_timeout:
        return invoke()

    box: dict[str, tuple[str | None, str | None]] = {}

    def worker() -> None:
        box["result"] = invoke()

    thread = threading.Thread(target=worker, name="text-model-call", daemon=True)
    thread.start()
    thread.join(max(0.0, timeout))
    if thread.is_alive():
        return None, f"文本模型请求超时（>{timeout:g}s）"
    result = box.get("result")
    if isinstance(result, tuple):
        return result
    return None, "文本模型请求未返回结果"


def parse_json_payload(text: str) -> dict[str, Any] | None:
    """Parse a JSON object while tolerating a Markdown fence or outer noise."""
    normalized = (text or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```\s*$", normalized, re.DOTALL)
    if fence:
        normalized = fence.group(1).strip()
    try:
        data = json.loads(normalized)
    except json.JSONDecodeError:
        start, end = normalized.find("{"), normalized.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(normalized[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _extract_qwen_text(response: Any) -> str | None:
    try:
        content = response.output.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return None
    if isinstance(content, list):
        content = "".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return content if isinstance(content, str) else None


def _call_qwen_sync(
    messages: list[dict[str, str]], model: str
) -> tuple[str | None, str | None]:
    import dashscope

    response = dashscope.Generation.call(
        model=model,
        messages=messages,
        result_format="message",
        api_key=os.environ.get("DASHSCOPE_API_KEY"),
    )
    status = getattr(response, "status_code", None)
    if status is not None and status != 200:
        detail = getattr(response, "message", "") or getattr(response, "code", "")
        return None, f"文本模型返回 {status}: {detail}"
    text = _extract_qwen_text(response)
    return (text, None) if text else (None, "文本模型响应中没有文本内容")


def _call_openai_sync(
    messages: list[dict[str, str]],
    model: str,
    timeout: float,
    *,
    max_tokens: int,
) -> tuple[str | None, str | None]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    try:
        status, body = _http_request_json(
            "https://api.openai.com/v1/chat/completions",
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            body=payload,
            timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read()[:200]
        return None, _status_error(exc.code, body)
    if status != 200:
        return None, _status_error(status, body)
    try:
        data = json.loads(body.decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "文本模型响应解析失败"
    return (
        (content, None)
        if isinstance(content, str) and content
        else (None, "文本模型响应中没有文本内容")
    )


def _status_error(status: int, body: bytes) -> str:
    detail = body[:200].decode("utf-8", "replace")
    return f"文本模型返回 {status}: {detail}"


def build_prompt_messages(
    number: str,
    task: str,
    lang: str,
    *,
    owner: str | None = None,
    persona: str | None = None,
) -> list[dict[str, str]]:
    lang = normalize_lang(lang)
    number_text = (number or "").strip() or "未知"
    task_text = (task or "").strip() or ("无预设事项" if lang == "zh" else "no preset task")
    owner_text = (owner or "").strip() or owner_name(lang)
    persona_text = (persona or "").strip() or agent_persona(lang)
    return [
        {"role": "system", "content": _SYSTEM_PROMPT[lang]},
        {
            "role": "user",
            "content": _USER_TEMPLATE[lang].format(
                owner=owner_text,
                persona=persona_text,
                number=number_text,
                task=task_text,
            ),
        },
    ]


def generate_prompt_scenario(
    number: str,
    task: str,
    lang: str,
    *,
    provider: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Generate a short scenario strategy paragraph; never raises."""
    lang = normalize_lang(lang)
    key = ((number or "").strip(), (task or "").strip(), lang)
    cached = _cache_get(key)
    if cached is not None:
        scenario, opening = cached
        return _ok(
            scenario,
            opening,
            provider or config.get_str("AGENT_PROVIDER"),
            "",
            cached=True,
        )
    try:
        if not config.get_bool("PROMPT_GEN_ENABLED"):
            return _fail("动态场景提示词生成已关闭", provider)
        selected_provider = (provider or config.get_str("AGENT_PROVIDER")).strip().lower()
        selected_timeout = timeout if timeout is not None else config.get_float("PROMPT_GEN_TIMEOUT")
        model = _select_model(selected_provider)
        messages = build_prompt_messages(
            number,
            task,
            lang,
            owner=owner_name(lang),
            persona=agent_persona(lang),
        )
        if selected_provider == "qwen":
            text, error = _call_qwen(messages, model, selected_timeout)
        elif selected_provider == "openai":
            text, error = _call_openai(messages, model, selected_timeout)
        else:
            return _fail(f"不支持的动态提示词提供方: {selected_provider}", selected_provider)
        if error is not None:
            logger.warning("动态场景提示词生成失败: %s", error)
            return _fail(error, selected_provider, model)
        scenario, opening = _parse_model_text(text or "")
        if not scenario:
            return _fail("模型响应中没有文本内容", selected_provider, model)
        _cache_put(key, (scenario, opening))
        return _ok(scenario, opening, selected_provider, model, cached=False)
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("动态场景提示词生成异常: %s", error)
        return _fail(error, provider)


def _select_model(provider: str) -> str:
    return select_text_model(provider, config.get_str("PROMPT_GEN_MODEL"))


def _call_qwen(
    messages: list[dict[str, str]], model: str, timeout: float
) -> tuple[str | None, str | None]:
    return call_text_model(
        messages,
        provider="qwen",
        model=model,
        timeout=timeout,
    )


def _call_openai(
    messages: list[dict[str, str]], model: str, timeout: float
) -> tuple[str | None, str | None]:
    return call_text_model(
        messages,
        provider="openai",
        model=model,
        timeout=timeout,
    )


def _http_request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        return int(status), resp.read()


def _normalize_scenario(text: str) -> str:
    collapsed = " ".join((text or "").strip().split())
    return collapsed[:MAX_SCENARIO_CHARS]


def _normalize_opening(text: str) -> str:
    """规范化生成的开场白；超限直接放弃（回退模板开场），绝不硬切成半句让 AI 念断句。"""
    collapsed = " ".join((text or "").strip().split())
    if len(collapsed) > MAX_OPENING_CHARS:
        return ""
    return collapsed


def _parse_model_text(text: str) -> tuple[str, str]:
    data = parse_json_payload(text)
    if data is None:
        return _normalize_scenario(text), ""
    scenario = data.get("scenario")
    opening = data.get("opening")
    return (
        _normalize_scenario(scenario if isinstance(scenario, str) else ""),
        _normalize_opening(opening if isinstance(opening, str) else ""),
    )


def _cache_get(key: tuple[str, str, str]) -> tuple[str, str] | None:
    with _CACHE_LOCK:
        value = _CACHE.get(key)
        if value is not None:
            _CACHE.move_to_end(key)
        return value


def _cache_put(key: tuple[str, str, str], value: tuple[str, str]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)


def _ok(
    scenario: str,
    opening: str,
    provider: str | None,
    model: str,
    *,
    cached: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "scenario": scenario,
        "opening": opening,
        "error": None,
        "provider": provider,
        "model": model,
        "cached": cached,
    }


def _fail(
    error: str, provider: str | None = None, model: str | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "scenario": "",
        "opening": "",
        "error": error,
        "provider": provider,
        "model": model or "",
        "cached": False,
    }
