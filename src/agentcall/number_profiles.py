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
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from . import config
from .prompt_gen import _normalize_opening, _normalize_scenario

logger = logging.getLogger(__name__)


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
        exact: dict[str, Any] | None = None
        wildcard: dict[str, Any] | None = None
        for item in profiles:
            if not isinstance(item, dict) or _norm(item.get("number")) != target_number:
                continue
            task_variants = {_norm(v) for v in _task_values(item.get("task")) if _norm(v)}
            if task_variants:
                if target_task and target_task in task_variants:
                    exact = item
                    break
            elif wildcard is None:
                wildcard = item
        return _normalize_profile(exact or wildcard, target_number, target_task, lang)
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        logger.warning("读取号码任务库失败: %s", exc)
        return None


def list_profiles(*, lang: str = "zh", path: str | Path | None = None) -> list[dict[str, str]]:
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
        for item in profiles:
            if not isinstance(item, dict):
                continue
            number = _norm(item.get("number"))
            if not number:
                continue
            task = _norm(_pick_lang(item.get("task"), lang))
            label = _norm(_pick_lang(item.get("label"), lang)) or _fallback_label(number, task, lang)
            choices.append({"number": number, "task": task, "label": label})
        return choices
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        logger.warning("列出号码任务库失败: %s", exc)
        return []


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


def _normalize_profile(
    item: dict[str, Any] | None,
    number: str,
    task: str,
    lang: str,
) -> dict[str, Any] | None:
    if item is None:
        return None
    scenario = _normalize_scenario(_pick_lang(item.get("scenario"), lang))
    if not scenario:
        logger.warning("号码任务库条目缺少有效 scenario: number=%s task=%s", number, task)
        return None
    opening = _normalize_opening(_pick_lang(item.get("opening"), lang))
    return {
        "ok": True,
        "scenario": scenario,
        "opening": opening,
        "error": None,
        "provider": "",
        "model": "",
        "cached": False,
        "source": "profile",
        "number": number,
        "task": task,
    }


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _fallback_label(number: str, task: str, lang: str) -> str:
    if task:
        return f"{number} · {task}"
    general = "general" if _lang_key(lang) == "en" else "通用"
    return f"{number} · {general}"
