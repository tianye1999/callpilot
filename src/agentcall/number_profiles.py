"""User-editable outbound number/task prompt profiles (multilingual).

Profiles are loaded from JSON at call time so users can tune frequent
outbound scenarios without restarting the app. Lookup is intentionally
simple and deterministic: exact number+task first, then number wildcard.

Each of ``label`` / ``task`` / ``scenario`` / ``opening`` may be a plain
string (language-neutral) or an object like ``{"zh": "...", "en": "..."}``.
``scenario``/``opening`` are picked by the call language (AGENT_LANGUAGE);
``label``/``task`` are picked by the UI language passed to ``list_profiles``.
Missing language falls back to the other language, then any non-empty value,
so single-language profiles keep working unchanged. Fallback is per-field, so
prefer supplying both languages for every field to avoid mixed-language output.

``opening_mode``(#80-B,可选): ``say``(默认)拨通即说开场白;``wait``
静默等对方先说——IVR 热线(如运营商客服)用,避免 AI 开场白压掉首段菜单
播报。语言无关的普通字符串;非法值按 ``say`` 处理。

``dtmf_spoken_followup``(可选): 默认 ``false``。仅对显式启用的 IVR
profile，在 Agent 明确说出自己将按键却未调用工具时启用执行层安全网。

``result_verification``(可选): ``none``(默认)或 ``carrier_sms``。后者仅用于
运营商账户查询，要求通话结果由同一运营商公共客服号发来的短信做可信校验。

``max_call_seconds``(WIL-120 一期,可选): 覆盖全局 ``OUTBOUND_MAX_SECONDS``。
0=不限时,1..7200=该场景的时长上限;缺省=用全局值(默认 600s)。长通话
(客服排队 30 分钟+)是**场景属性**而非全局属性——只给点名的预设放开。

``wrap_up_judge``(WIL-120 一期,可选): 默认 ``true``。长排队场景置 ``false``
显式关闭收尾裁判——排队循环音会被裁判误判成「对话打转」而提前挂断。
这是一期的诚实开关;二期换成 hold 状态冻结后收回。

``task_package``(WIL-120 三期,可选): 结构化任务包,四段全可选——
``verification``(核身事实,仅对方要求时按需提供)/``negotiation``(谈判底牌)/
``preauth``(预授权区间:AI 只被授权在此范围内直接答应,超出必须 ask_owner)/
``blacklist``(绝不同意的操作清单)。前三段是自由 键→值 表(都是字符串,键是
给模型看的自然语言标签,不做字段枚举);值随 prompt 进模型但**绝不进
events/metrics/日志**(WIL-95 §7)。真实账户信息只存本地 number_profiles.json。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from . import config
from .prompt_gen import (
    MAX_OPENING_WIDTH,
    _normalize_opening,
    display_width,
)

logger = logging.getLogger(__name__)

_PROFILE_WRITE_LOCK = threading.RLock()
MAX_PROFILE_SCENARIO_CHARS = 1200
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DIAL_NUMBER_RE = re.compile(r"\+?[0-9*#]{1,32}")
_MANAGED_FIELDS = {
    "id",
    "enabled",
    "number",
    "match_mode",
    "label",
    "task",
    "scenario",
    "opening",
    "opening_mode",
    "dtmf_spoken_followup",
    "result_verification",
    "max_call_seconds",
    "wrap_up_judge",
    "task_package",
}


class ProfileValidationError(ValueError):
    """A profile cannot be stored because one or more fields are invalid."""


class ProfileConflictError(ValueError):
    """A profile would make exact or number-fallback matching ambiguous."""


class ProfileNotFoundError(LookupError):
    """The requested profile id does not exist in the current library."""


def default_profiles_file() -> Path:
    configured = config.get_str("NUMBER_PROFILES_FILE").strip()
    if configured:
        return Path(configured).expanduser()
    return config.data_dir() / "number_profiles.json"


def bundled_seed_file() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "seed" / "number_profiles.example.json"
    return Path(__file__).resolve().parents[2] / "data" / "number_profiles.example.json"


def ensure_seeded(
    *,
    target: str | Path | None = None,
    seed: str | Path | None = None,
) -> bool:
    """Copy the bundled preset library on first run; never overwrite or raise."""
    target_path = (
        Path(target).expanduser()
        if target is not None
        else config.data_dir() / "number_profiles.json"
    )
    seed_path = Path(seed).expanduser() if seed is not None else bundled_seed_file()
    if target_path.exists():
        return False
    try:
        if not seed_path.exists():
            logger.warning("号码任务库种子文件不存在: %s", seed_path)
            return False
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed_path, target_path)
        return True
    except OSError as exc:
        logger.warning("初始化号码任务库失败: %s", exc)
        return False


def _lang_key(lang: str | None) -> str:
    return "en" if str(lang or "").strip().lower().startswith("en") else "zh"


def _pick_lang(value: Any, lang: str) -> str:
    """Pick a string for ``lang`` from a str (neutral) or {zh,en} mapping.

    Falls back to the other language, then any non-empty string value, so a
    profile that only supplies one language still resolves.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        key = _lang_key(lang)
        other = "zh" if key == "en" else "en"
        for candidate in (key, other):
            picked = value.get(candidate)
            if isinstance(picked, str) and picked.strip():
                return picked
        for picked in value.values():
            if isinstance(picked, str) and picked.strip():
                return picked
    return ""


def _task_values(value: Any) -> list[str]:
    """All language variants of a ``task`` field, for bilingual matching."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, str)]
    return []


def lookup_profile(
    number: str | None,
    task: str | None,
    *,
    lang: str = "zh",
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return a normalized prompt profile for ``number``/``task``; never raises.

    ``lang`` is the call language (AGENT_LANGUAGE) used to pick scenario/opening.
    ``task`` matches against any language variant of a profile's task.
    """
    target_number = _norm(number)
    target_task = _norm(task)
    if not target_number:
        return None
    try:
        data = _load_profiles_file(Path(path).expanduser() if path is not None else default_profiles_file())
        if data is None:
            return None
        profiles = data.get("profiles")
        if not isinstance(profiles, list):
            logger.warning("号码任务库格式无效: profiles 不是列表")
            return None
        exact: tuple[dict[str, Any], str] | None = None
        wildcard: tuple[dict[str, Any], str] | None = None
        for item, profile_id in _profiles_with_ids(profiles):
            if not _is_enabled(item) or not same_dial_number(item.get("number"), target_number):
                continue
            task_variants = {_norm(v) for v in _task_values(item.get("task")) if _norm(v)}
            if task_variants:
                if target_task and target_task in task_variants:
                    exact = (item, profile_id)
                    break
            elif wildcard is None:
                wildcard = (item, profile_id)
        matched = exact or wildcard
        if matched is None:
            return None
        return _normalize_profile(matched[0], target_number, target_task, lang, matched[1])
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        logger.warning("读取号码任务库失败: %s", exc)
        return None


def lookup_profile_by_id(
    profile_id: str | None,
    number: str | None,
    task: str | None,
    *,
    lang: str = "zh",
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Resolve an explicitly selected profile id; never raises.

    The supplied number must still match the profile so a stale browser cannot
    apply one destination's prompt strategy to another destination.
    """
    target_id = _norm(profile_id)
    target_number = _norm(number)
    if not target_id or not target_number:
        return None
    try:
        data = _load_profiles_file(Path(path).expanduser() if path is not None else default_profiles_file())
        if data is None or not isinstance(data.get("profiles"), list):
            return None
        for item, item_id in _profiles_with_ids(data["profiles"]):
            if item_id != target_id:
                continue
            if not _is_enabled(item) or not same_dial_number(
                item.get("number"), target_number
            ):
                return None
            return _normalize_profile(item, target_number, _norm(task), lang, item_id)
        return None
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        logger.warning("按 ID 读取号码任务库失败: %s", exc)
        return None


def list_profiles(
    *,
    lang: str = "zh",
    path: str | Path | None = None,
    include_id: bool = False,
) -> list[dict[str, str]]:
    """Return all user-visible profile choices in file order; never raises.

    ``lang`` is the UI language used to pick each entry's label/task text.
    """
    try:
        data = _load_profiles_file(Path(path).expanduser() if path is not None else default_profiles_file())
        if data is None:
            return []
        profiles = data.get("profiles")
        if not isinstance(profiles, list):
            logger.warning("号码任务库格式无效: profiles 不是列表")
            return []
        choices: list[dict[str, str]] = []
        for item, profile_id in _profiles_with_ids(profiles):
            if not _is_enabled(item):
                continue
            number = _norm(item.get("number"))
            if not number:
                continue
            task = _norm(_pick_lang(item.get("task"), lang))
            label = _norm(_pick_lang(item.get("label"), lang)) or _fallback_label(number, task, lang)
            choice = {"number": number, "task": task, "label": label}
            if include_id:
                choice["id"] = profile_id
            choices.append(choice)
        return choices
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        logger.warning("列出号码任务库失败: %s", exc)
        return []


def list_managed_profiles(*, path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return editable, language-complete profile records; never raises."""
    try:
        data = _load_profiles_file(Path(path).expanduser() if path is not None else default_profiles_file())
        if data is None or not isinstance(data.get("profiles"), list):
            return []
        return [_managed_profile(item, profile_id) for item, profile_id in _profiles_with_ids(data["profiles"])]
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        logger.warning("读取号码任务库管理数据失败: %s", exc)
        return []


def create_profile(
    payload: Any, *, path: str | Path | None = None
) -> dict[str, Any]:
    """Validate and append one profile, writing the JSON file atomically."""
    profile = _validate_profile_payload(payload)
    profile["id"] = uuid.uuid4().hex
    target = Path(path).expanduser() if path is not None else default_profiles_file()
    with _PROFILE_WRITE_LOCK:
        data, profiles = _load_profiles_for_write(target)
        _ensure_profile_ids(profiles)
        _check_conflicts(profiles, profile)
        profiles.append(profile)
        _write_profiles_file(target, data)
    return _managed_profile(profile, profile["id"])


def update_profile(
    profile_id: str,
    payload: Any,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Replace editable fields for one profile while retaining its stable id."""
    target_id = _validate_profile_id(profile_id)
    validated = _validate_profile_payload(payload)
    target = Path(path).expanduser() if path is not None else default_profiles_file()
    with _PROFILE_WRITE_LOCK:
        data, profiles = _load_profiles_for_write(target)
        _ensure_profile_ids(profiles)
        index = _find_profile_index(profiles, target_id)
        if index is None:
            raise ProfileNotFoundError("预设任务不存在或已被删除")
        current = profiles[index]
        retained = {key: value for key, value in current.items() if key not in _MANAGED_FIELDS}
        replacement = {**retained, **validated, "id": target_id}
        _check_conflicts(profiles, replacement, exclude_id=target_id)
        profiles[index] = replacement
        _write_profiles_file(target, data)
    return _managed_profile(replacement, target_id)


def delete_profile(profile_id: str, *, path: str | Path | None = None) -> bool:
    """Delete a profile by id; return False when it is already absent."""
    target_id = _validate_profile_id(profile_id)
    target = Path(path).expanduser() if path is not None else default_profiles_file()
    with _PROFILE_WRITE_LOCK:
        data, profiles = _load_profiles_for_write(target)
        _ensure_profile_ids(profiles)
        index = _find_profile_index(profiles, target_id)
        if index is None:
            return False
        del profiles[index]
        _write_profiles_file(target, data)
        return True


def _load_profiles_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("号码任务库读取/解析失败: %s", exc)
        return None
    if not isinstance(loaded, dict):
        logger.warning("号码任务库格式无效: 顶层不是对象")
        return None
    return loaded


# task_package 尺寸护栏：键≤40 字符、值≤200、每段≤16 条、blacklist 项≤120。
# 只限尺寸不限字段名——键是给模型看的自然语言标签，枚举字段名违反非枚举原则。
_PACKAGE_SECTIONS = ("verification", "negotiation", "preauth")
_PACKAGE_MAX_ENTRIES = 16
_PACKAGE_MAX_KEY = 40
_PACKAGE_MAX_VALUE = 200
_PACKAGE_MAX_BLACKLIST_ITEM = 120


def _normalize_task_package(value: Any) -> dict[str, Any] | None:
    """宽松读取（call 时用）：非法段丢弃、非法条目跳过，绝不抛。"""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for section in _PACKAGE_SECTIONS:
        raw = value.get(section)
        if not isinstance(raw, dict):
            continue
        entries = {
            str(k).strip()[:_PACKAGE_MAX_KEY]: str(v).strip()[:_PACKAGE_MAX_VALUE]
            for k, v in list(raw.items())[:_PACKAGE_MAX_ENTRIES]
            if str(k).strip() and str(v).strip()
        }
        if entries:
            out[section] = entries
    raw_blacklist = value.get("blacklist")
    if isinstance(raw_blacklist, list):
        items = [
            str(item).strip()[:_PACKAGE_MAX_BLACKLIST_ITEM]
            for item in raw_blacklist[:_PACKAGE_MAX_ENTRIES]
            if str(item).strip()
        ]
        if items:
            out["blacklist"] = items
    return out or None


def _validate_task_package(value: Any) -> dict[str, Any] | None:
    """严格校验（CRUD 写入用）：结构/尺寸非法直接拒绝，不静默截断。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProfileValidationError("task_package 必须是对象")
    unknown = set(value) - set(_PACKAGE_SECTIONS) - {"blacklist"}
    if unknown:
        raise ProfileValidationError(
            f"task_package 仅支持 {'/'.join(_PACKAGE_SECTIONS)}/blacklist 段，"
            f"多余: {sorted(unknown)}"
        )
    out: dict[str, Any] = {}
    for section in _PACKAGE_SECTIONS:
        raw = value.get(section)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ProfileValidationError(f"task_package.{section} 必须是对象")
        if len(raw) > _PACKAGE_MAX_ENTRIES:
            raise ProfileValidationError(
                f"task_package.{section} 最多 {_PACKAGE_MAX_ENTRIES} 条"
            )
        entries: dict[str, str] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ProfileValidationError(
                    f"task_package.{section} 的键和值都必须是字符串"
                )
            key, val = k.strip(), " ".join(v.strip().split())
            if not key or not val:
                continue
            if len(key) > _PACKAGE_MAX_KEY or len(val) > _PACKAGE_MAX_VALUE:
                raise ProfileValidationError(
                    f"task_package.{section} 单条过长"
                    f"（键≤{_PACKAGE_MAX_KEY}、值≤{_PACKAGE_MAX_VALUE} 字符）"
                )
            entries[key] = val
        if entries:
            out[section] = entries
    raw_blacklist = value.get("blacklist")
    if raw_blacklist is not None:
        if not isinstance(raw_blacklist, list) or any(
            not isinstance(item, str) for item in raw_blacklist
        ):
            raise ProfileValidationError("task_package.blacklist 必须是字符串数组")
        if len(raw_blacklist) > _PACKAGE_MAX_ENTRIES:
            raise ProfileValidationError(
                f"task_package.blacklist 最多 {_PACKAGE_MAX_ENTRIES} 条"
            )
        items = []
        for item in raw_blacklist:
            cleaned = " ".join(item.strip().split())
            if not cleaned:
                continue
            if len(cleaned) > _PACKAGE_MAX_BLACKLIST_ITEM:
                raise ProfileValidationError(
                    f"task_package.blacklist 单条不能超过 "
                    f"{_PACKAGE_MAX_BLACKLIST_ITEM} 字符"
                )
            items.append(cleaned)
        if items:
            out["blacklist"] = items
    return out or None


def _normalize_max_call_seconds(value: Any) -> int | None:
    """None=未设置(用全局);0=不限;1..7200=场景上限。非法值当未设置,不猜。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 <= value <= 7200:
        return value
    return None


def _normalize_profile(
    item: dict[str, Any] | None,
    number: str,
    task: str,
    lang: str,
    profile_id: str = "",
) -> dict[str, Any] | None:
    if item is None:
        return None
    scenario = _normalize_profile_scenario(_pick_lang(item.get("scenario"), lang))
    if not scenario:
        logger.warning("号码任务库条目缺少有效 scenario: number=%s task=%s", number, task)
        return None
    opening = _normalize_opening(_pick_lang(item.get("opening"), lang))
    # 开场模式(#80-B):say=拨通即说开场白(默认);wait=静默等对方先说
    # (IVR 热线场景,AI 开场白会压掉首段菜单播报)。非法值回落 say。
    opening_mode = str(item.get("opening_mode") or "").strip().lower()
    if opening_mode not in {"say", "wait"}:
        opening_mode = "say"
    return {
        "ok": True,
        "scenario": scenario,
        "opening": opening,
        "opening_mode": opening_mode,
        "dtmf_spoken_followup": item.get("dtmf_spoken_followup") is True,
        "result_verification": (
            "carrier_sms"
            if item.get("result_verification") == "carrier_sms"
            else "none"
        ),
        "max_call_seconds": _normalize_max_call_seconds(
            item.get("max_call_seconds")
        ),
        "wrap_up_judge": item.get("wrap_up_judge") is not False,
        "task_package": _normalize_task_package(item.get("task_package")),
        "error": None,
        "provider": "",
        "model": "",
        "cached": False,
        "source": "profile",
        "profile_id": profile_id,
        "number": number,
        "task": task,
    }


def _profiles_with_ids(profiles: list[Any]) -> list[tuple[dict[str, Any], str]]:
    rows: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for index, item in enumerate(profiles):
        if not isinstance(item, dict):
            continue
        candidate = _norm(item.get("id"))
        if not _PROFILE_ID_RE.fullmatch(candidate) or candidate in seen:
            canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(f"{index}\0{canonical}".encode()).hexdigest()[:20]
            candidate = f"legacy_{digest}"
        if candidate in seen:
            candidate = f"legacy_{index}_{hashlib.sha256(candidate.encode()).hexdigest()[:12]}"
        seen.add(candidate)
        rows.append((item, candidate))
    return rows


def _ensure_profile_ids(profiles: list[Any]) -> None:
    for item, profile_id in _profiles_with_ids(profiles):
        item["id"] = profile_id


def _is_enabled(item: dict[str, Any]) -> bool:
    return item.get("enabled") is not False


def _localized_map(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        text = value.strip()
        return {"zh": text, "en": text}
    if isinstance(value, dict):
        return {
            "zh": _norm(value.get("zh")),
            "en": _norm(value.get("en")),
        }
    return {"zh": "", "en": ""}


def _managed_profile(item: dict[str, Any], profile_id: str) -> dict[str, Any]:
    task = _localized_map(item.get("task"))
    match_mode = "exact" if any(task.values()) else "number"
    opening_mode = str(item.get("opening_mode") or "").strip().lower()
    if opening_mode not in {"say", "wait"}:
        opening_mode = "say"
    return {
        "id": profile_id,
        "enabled": _is_enabled(item),
        "number": _norm(item.get("number")),
        "match_mode": match_mode,
        "label": _localized_map(item.get("label")),
        "task": task,
        "scenario": _localized_map(item.get("scenario")),
        "opening": _localized_map(item.get("opening")),
        "opening_mode": opening_mode,
        "dtmf_spoken_followup": item.get("dtmf_spoken_followup") is True,
        "result_verification": (
            "carrier_sms"
            if item.get("result_verification") == "carrier_sms"
            else "none"
        ),
        "max_call_seconds": _normalize_max_call_seconds(
            item.get("max_call_seconds")
        ),
        "wrap_up_judge": item.get("wrap_up_judge") is not False,
        "task_package": _normalize_task_package(item.get("task_package")),
    }


def _validate_profile_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProfileValidationError("预设任务必须是 JSON 对象")
    number = _norm(payload.get("number"))
    if not _DIAL_NUMBER_RE.fullmatch(number):
        raise ProfileValidationError("号码格式不合法，仅支持可拨号字符且最长 32 位")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ProfileValidationError("enabled 必须是布尔值")

    label = _validate_localized(payload.get("label"), "label", max_chars=120)
    task = _validate_localized(payload.get("task"), "task", max_chars=120)
    scenario = _validate_localized(
        payload.get("scenario"),
        "scenario",
        max_chars=MAX_PROFILE_SCENARIO_CHARS,
        required=True,
    )
    opening = _validate_localized(
        payload.get("opening"), "opening", max_width=MAX_OPENING_WIDTH
    )
    match_mode = _norm(payload.get("match_mode"))
    if not match_mode:
        match_mode = "exact" if _localized_values(task) else "number"
    if match_mode not in {"exact", "number"}:
        raise ProfileValidationError("match_mode 只能是 exact 或 number")
    if match_mode == "exact" and not _localized_values(task):
        raise ProfileValidationError("精确匹配预设的 task 不能为空")

    # #80-B:opening_mode 仅 say/wait；非法值拒绝，不静默回落
    opening_mode = _norm(payload.get("opening_mode"))
    if opening_mode and opening_mode not in {"say", "wait"}:
        raise ProfileValidationError("opening_mode 只能是 say 或 wait")
    if not opening_mode:
        opening_mode = "say"
    dtmf_spoken_followup = payload.get("dtmf_spoken_followup", False)
    if not isinstance(dtmf_spoken_followup, bool):
        raise ProfileValidationError("dtmf_spoken_followup 必须是布尔值")
    result_verification = payload.get("result_verification", "none")
    if result_verification not in {"none", "carrier_sms"}:
        raise ProfileValidationError(
            "result_verification 只能是 none 或 carrier_sms"
        )
    max_call_seconds = payload.get("max_call_seconds")
    if max_call_seconds is not None:
        if isinstance(max_call_seconds, bool) or not isinstance(
            max_call_seconds, int
        ) or not (0 <= max_call_seconds <= 7200):
            raise ProfileValidationError(
                "max_call_seconds 只能是 0（不限）到 7200 之间的整数，或留空用全局值"
            )
    wrap_up_judge = payload.get("wrap_up_judge", True)
    if not isinstance(wrap_up_judge, bool):
        raise ProfileValidationError("wrap_up_judge 必须是布尔值")
    task_package = _validate_task_package(payload.get("task_package"))

    profile: dict[str, Any] = {
        "enabled": enabled,
        "number": number,
        "label": label,
        "scenario": scenario,
        "opening": opening,
        "opening_mode": opening_mode,
        "dtmf_spoken_followup": dtmf_spoken_followup,
        "result_verification": result_verification,
        "wrap_up_judge": wrap_up_judge,
    }
    if max_call_seconds is not None:
        # 只在显式提供时落盘：缺省=跟随全局，与 0（不限）语义不同。
        profile["max_call_seconds"] = max_call_seconds
    if task_package is not None:
        profile["task_package"] = task_package
    if match_mode == "exact":
        profile["task"] = task
    return profile


def _validate_localized(
    value: Any,
    field: str,
    *,
    max_chars: int | None = None,
    max_width: int | None = None,
    required: bool = False,
) -> str | dict[str, str]:
    if value is None:
        cleaned: str | dict[str, str] = ""
    elif isinstance(value, str):
        cleaned = " ".join(value.strip().split())
    elif isinstance(value, dict):
        if any(key not in {"zh", "en"} for key in value):
            raise ProfileValidationError(f"{field} 仅支持 zh/en 双语字段")
        if any(not isinstance(item, str) for item in value.values()):
            raise ProfileValidationError(f"{field} 的语言值必须是字符串")
        cleaned = {
            "zh": " ".join(str(value.get("zh") or "").strip().split()),
            "en": " ".join(str(value.get("en") or "").strip().split()),
        }
    else:
        raise ProfileValidationError(f"{field} 必须是字符串或 zh/en 对象")
    values = _localized_values(cleaned)
    if required and not values:
        raise ProfileValidationError(f"{field} 不能为空")
    if max_chars is not None and any(len(item) > max_chars for item in values):
        raise ProfileValidationError(f"{field} 不能超过 {max_chars} 个字符")
    if max_width is not None and any(display_width(item) > max_width for item in values):
        # 宽度口径：中文按 2、拉丁按 1，两种文字用同一阈值才公平。
        raise ProfileValidationError(
            f"{field} 不能超过 {max_width} 显示宽度（中文约 {max_width // 2} 字）"
        )
    return cleaned


def _localized_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, str) and item]
    return []


def _normalize_profile_scenario(text: str) -> str:
    collapsed = " ".join((text or "").strip().split())
    return collapsed[:MAX_PROFILE_SCENARIO_CHARS]


def _check_conflicts(
    profiles: list[Any],
    candidate: dict[str, Any],
    *,
    exclude_id: str | None = None,
) -> None:
    candidate_number = _norm(candidate.get("number"))
    candidate_tasks = {_norm(value) for value in _task_values(candidate.get("task")) if _norm(value)}
    for item, profile_id in _profiles_with_ids(profiles):
        # 冲突检测必须与 lookup 用同一套等价关系，否则 13800138000 与
        # +8613800138000 可以并存为两条预设，之后命中哪条取决于文件顺序。
        if profile_id == exclude_id or not same_dial_number(
            item.get("number"), candidate_number
        ):
            continue
        item_tasks = {_norm(value) for value in _task_values(item.get("task")) if _norm(value)}
        if not candidate_tasks and not item_tasks:
            raise ProfileConflictError("同一号码只能有一个号码通配预设")
        if candidate_tasks and item_tasks and candidate_tasks.intersection(item_tasks):
            raise ProfileConflictError("同一号码存在重复的精确任务匹配")


def _load_profiles_for_write(path: Path) -> tuple[dict[str, Any], list[Any]]:
    if not path.exists():
        data: dict[str, Any] = {"profiles": []}
        return data, data["profiles"]
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileValidationError(f"号码任务库无法读取或 JSON 格式错误: {exc}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("profiles"), list):
        raise ProfileValidationError("号码任务库顶层必须包含 profiles 列表")
    return loaded, loaded["profiles"]


def _write_profiles_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def _validate_profile_id(profile_id: str) -> str:
    candidate = _norm(profile_id)
    if not _PROFILE_ID_RE.fullmatch(candidate):
        raise ProfileValidationError("预设任务 ID 格式不合法")
    return candidate


def _find_profile_index(profiles: list[Any], profile_id: str) -> int | None:
    for index, item in enumerate(profiles):
        if isinstance(item, dict) and _norm(item.get("id")) == profile_id:
            return index
    return None


def _norm(value: Any) -> str:
    return str(value or "").strip()


# 本机 SIM 所在国家的国际区号。**只**剥离这一个前缀，不做「1–3 位随便试」。
#
# 第一版写的是「试 1–3 位，凑得上就算同号」，Codex 评审给出反例：
#   same_dial_number("+33123456789", "3123456789") → True
# 只剥掉一个 "3" 就凑上了，但 +33 是法国、3123456789 是另一个国家的号码。
# 误判的代价比原 bug 更重 —— 拨 A 号却套用了 B 号的精调提示词。
#
# 定成单一常量是有先例的：sim_identity.py 的 PLMN→运营商映射同样是
# 「公开电信标准的确定性事实数据」，项目已明确它不属于被禁止的枚举
# （非枚举硬原则针对的是对话理解与交互管控，不是号码格式）。
# 真机硬约束本来也只允许拨本卡运营商的免费客服号（大陆），默认 86 相符。
DIAL_COUNTRY_CODE = "86"
# 允许「去国家码后仍相等」的最短有效长度。短号（10086 / 110 等）绝不能靠
# 后缀匹配去命中长号码，否则 10086 的预设会误命中任意以 10086 结尾的号码。
_MIN_SUBSCRIBER_DIGITS = 7


def canonical_dial_number(value: Any) -> str:
    """把拨号号码收敛成可比较形式：去掉分隔符，保留前导 + 与 * #。"""
    raw = _norm(value)
    if not raw:
        return ""
    plus = raw.startswith("+")
    body = "".join(ch for ch in raw if ch.isdigit() or ch in "*#")
    return ("+" + body) if plus else body


def same_dial_number(left: Any, right: Any) -> bool:
    """两个号码是否指向同一个被叫。

    #75：预设匹配曾是裸字符串相等，而拨号号码允许前导 `+`。用户从通讯录带
    `+86` 拨出时，精心写好的预设**静默失配**并落回动态生成——表现为「同一个
    号码有时用预设有时不用」，且没有任何日志说明原因。

    规则刻意收得很紧，因为**误判比漏判更糟**（拨 A 号却套用 B 号的精调提示词）：
    只有带 `+` 的那一方、且其国家码正好是本机的 DIAL_COUNTRY_CODE 时才剥离；
    剩余订户号还要至少 _MIN_SUBSCRIBER_DIGITS 位，防止短号被后缀误命中。
    """
    a = canonical_dial_number(left)
    b = canonical_dial_number(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # 只有带 + 的那一方才可能含国家码；两边都不带 + 时不做任何猜测。
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    bare = shorter.lstrip("+")
    if shorter.startswith("+") or not longer.startswith("+"):
        return False
    if len(bare) < _MIN_SUBSCRIBER_DIGITS or not bare.isdigit():
        return False
    return longer == f"+{DIAL_COUNTRY_CODE}{bare}"


def _fallback_label(number: str, task: str, lang: str) -> str:
    if task:
        return f"{number} · {task}"
    general = "general" if _lang_key(lang) == "en" else "通用"
    return f"{number} · {general}"
