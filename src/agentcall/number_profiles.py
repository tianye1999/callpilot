"""User-editable outbound number/task prompt profiles.

Profiles are loaded from JSON at call time so users can tune frequent
outbound scenarios without restarting the app. Lookup is intentionally
simple and deterministic: exact number+task first, then number wildcard.
"""

from __future__ import annotations

import json
import logging
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


def lookup_profile(
    number: str | None,
    task: str | None,
    *,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return a normalized prompt profile for ``number``/``task``; never raises."""
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
            item_task = _norm(item.get("task"))
            if item_task and item_task == target_task:
                exact = item
                break
            if not item_task and wildcard is None:
                wildcard = item
        return _normalize_profile(exact or wildcard, target_number, target_task)
    except Exception as exc:  # noqa: BLE001 - contract: never raise
        logger.warning("读取号码任务库失败: %s", exc)
        return None


def list_profiles(*, path: str | Path | None = None) -> list[dict[str, str]]:
    """Return all user-visible profile choices in file order; never raises."""
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
            task = _norm(item.get("task"))
            label = _norm(item.get("label")) or _fallback_label(number, task)
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
) -> dict[str, Any] | None:
    if item is None:
        return None
    raw_scenario = item.get("scenario")
    scenario = _normalize_scenario(raw_scenario if isinstance(raw_scenario, str) else "")
    if not scenario:
        logger.warning("号码任务库条目缺少有效 scenario: number=%s task=%s", number, task)
        return None
    raw_opening = item.get("opening")
    opening = _normalize_opening(raw_opening if isinstance(raw_opening, str) else "")
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


def _fallback_label(number: str, task: str) -> str:
    if task:
        return f"{number} · {task}"
    general = "general" if config.get_str("AGENT_LANGUAGE").strip().lower().startswith("en") else "通用"
    return f"{number} · {general}"
