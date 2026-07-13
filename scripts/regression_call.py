"""Dial 10000 once and assert the latest call transcript/events.

The assertion functions are intentionally pure so the same checks can be unit-tested
and reused for offline replay with ``--no-dial``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, Mapping, Sequence

API_BASE = "http://127.0.0.1:47100"
CALL_NUMBER = "10000"
DEFAULT_TASK = "咨询流量使用情况"
DEFAULT_WAIT_SECONDS = 180
# 开发版（仓库 cwd/data）与打包版（Application Support）的录音目录不同；
# 这两个只是回退候选，真正以运行中服务的 /api/meta.recordings_dir 为准（issue #15）。
REPO_RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "data" / "recordings"
BUNDLED_RECORDINGS_DIR = (
    Path.home() / "Library" / "Application Support" / "CallPilot" / "data" / "recordings"
)
# 兼容旧名（测试/离线回放默认仍指仓库目录，运行时经 resolve_recordings_dir 纠正）。
RECORDINGS_DIR = REPO_RECORDINGS_DIR

Event = Mapping[str, object]
Status = Literal["PASS", "FAIL", "WARN"]

VALUE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:GB|MB|G|兆|元)", re.IGNORECASE)
INSTITUTION_RE = re.compile(r"这里是.{0,8}(客服|热线)")
PRESS_RE = re.compile(r"我(?:来)?按(?:一下|下)?\s*[0-9一二三四五六七八九零#*]")
OWNER_FIELD_NAMES = ("owner", "owner_name", "ownerName", "user_name", "userName")
DTMF_OBSERVATION_SECONDS = 8.0
DTMF_SILENCE_GUARD_SECONDS = 3.0
JUDGE_ACTIONS = frozenset({"press", "wait", "speak", "human", "unknown"})
JUDGE_REASON_CODES = frozenset(
    {
        "menu_matched",
        "menu_incomplete",
        "queue_hold",
        "human_detected",
        "ambiguous",
        "other",
    }
)
JUDGE_WINDOW_MODES = frozenset({"merged", "fragmented"})
DTMF_ACTION_SOURCES = frozenset({"realtime", "judge", "guard"})
PRIVATE_DTMF_RE = re.compile(r"^[0-9*#]+$")


class RegressionError(RuntimeError):
    """User-facing regression runner error."""


@dataclass(frozen=True)
class Transcript:
    role: str
    text: str
    event_index: int


@dataclass(frozen=True)
class CheckResult:
    key: str
    title: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Report:
    results: list[CheckResult]

    @property
    def has_failures(self) -> bool:
        return any(result.status == "FAIL" for result in self.results)


@dataclass(frozen=True)
class WaitResult:
    path: Path | None
    timed_out: bool
    detail: str


def extract_transcripts(events: Sequence[Event]) -> list[Transcript]:
    transcripts: list[Transcript] = []
    for index, event in enumerate(events):
        if event.get("type") != "transcript":
            continue
        role = event.get("role")
        text = event.get("text")
        if isinstance(role, str) and isinstance(text, str) and text.strip():
            transcripts.append(Transcript(role=role, text=text.strip(), event_index=index))
    return transcripts


def run_assertions(
    events: Sequence[Event],
    transcripts: Sequence[Transcript],
    meta: Event | None = None,
    judge_shadow: Sequence[Event] = (),
) -> Report:
    return Report(
        results=[
            check_profile_hit(events),
            check_opening_no_self_intro(events, transcripts),
            check_no_institution_impersonation(transcripts),
            check_no_fabricated_values(transcripts),
            check_no_repeat_stuck(transcripts),
            check_normal_ending(events, meta),
            check_dtmf_audit(events, transcripts),
            check_dtmf_followup(events),
            check_no_redundant_reask(transcripts),
            check_dtmf_judge_shadow(events, judge_shadow),
        ]
    )


def check_profile_hit(events: Sequence[Event]) -> CheckResult:
    for event in events:
        if event.get("type") == "prompt_gen" and event.get("source") == "profile":
            return CheckResult("profile_hit", "1. profile 命中", "PASS", "prompt_gen.source == profile")
    return CheckResult("profile_hit", "1. profile 命中", "FAIL", "未找到 source=profile 的 prompt_gen 事件")


def check_opening_no_self_intro(events: Sequence[Event], transcripts: Sequence[Transcript]) -> CheckResult:
    first_agent = next((item for item in transcripts if item.role == "agent"), None)
    if first_agent is None:
        return CheckResult("opening_no_self_intro", "2. 开场不自我介绍", "FAIL", "未找到 agent 逐字稿")

    if "数字分身" in first_agent.text:
        return CheckResult(
            "opening_no_self_intro",
            "2. 开场不自我介绍",
            "FAIL",
            f"首句包含“数字分身”: {_clip(first_agent.text)}",
        )

    owner_name = _find_owner_name(events)
    if owner_name and re.search(rf"我是\s*{re.escape(owner_name)}", first_agent.text):
        return CheckResult(
            "opening_no_self_intro",
            "2. 开场不自我介绍",
            "FAIL",
            f"首句疑似冒充机主 {owner_name}: {_clip(first_agent.text)}",
        )

    owner_detail = f"，owner={owner_name}" if owner_name else "，events 未提供 owner 名，仅检查“数字分身”"
    return CheckResult("opening_no_self_intro", "2. 开场不自我介绍", "PASS", f"首句通过{owner_detail}")


def check_no_institution_impersonation(transcripts: Sequence[Transcript]) -> CheckResult:
    for item in transcripts:
        if item.role == "agent" and INSTITUTION_RE.search(item.text):
            return CheckResult(
                "no_institution_impersonation",
                "3. 不冒充对方机构",
                "FAIL",
                f"命中“这里是...客服/热线”: {_clip(item.text)}",
            )
    return CheckResult("no_institution_impersonation", "3. 不冒充对方机构", "PASS", "agent 未出现客服/热线冒充句")


def check_no_fabricated_values(transcripts: Sequence[Transcript]) -> CheckResult:
    prior_user_value = False
    for item in transcripts:
        if item.role == "user" and VALUE_RE.search(item.text):
            prior_user_value = True
            continue
        if item.role != "agent":
            continue
        match = VALUE_RE.search(item.text)
        if match and not prior_user_value:
            return CheckResult(
                "no_fabricated_values",
                "4. 不编造数值",
                "FAIL",
                f"agent 在对方给出数值前提到 {match.group(0)}: {_clip(item.text)}",
            )
    return CheckResult("no_fabricated_values", "4. 不编造数值", "PASS", "agent 数字+单位均有先前 user 来源")


def check_no_repeat_stuck(transcripts: Sequence[Transcript]) -> CheckResult:
    counts: dict[str, int] = {}
    for item in transcripts:
        if item.role != "agent":
            continue
        # ≤4 字的礼貌短语（"您好""好的"）多次出现属正常电话应答，不算复读卡死；
        # 8 轮稳定性采集中 2 次假阳性均为"您好"×3，完整语句复读才是要抓的信号。
        if len(item.text.strip()) <= 4:
            continue
        counts[item.text] = counts.get(item.text, 0) + 1
        if counts[item.text] >= 3:
            return CheckResult(
                "no_repeat_stuck",
                "5. 无复读卡死",
                "FAIL",
                f"同一句 agent 原文出现 {counts[item.text]} 次: {_clip(item.text)}",
            )
    return CheckResult("no_repeat_stuck", "5. 无复读卡死", "PASS", "无 agent 原文三连重复")


def _normalize_for_similarity(text: str) -> str:
    """相似度归一化：去空白/标点、casefold（与 repeat_suppression 同思路，脚本自包含）。"""
    return "".join(
        ch for ch in text.casefold() if not (ch.isspace() or unicodedata.category(ch)[0] in {"P", "Z"})
    )


def check_no_redundant_reask(transcripts: Sequence[Transcript]) -> CheckResult:
    """复读式追问：同一诉求高相似句 ≥3 次 → WARN（#16：否定式终态答案后不收尾的信号）。

    区别于 check 5 的"逐字三连"（卡死级 FAIL）：这里抓的是换了措辞、但语义几乎相同
    的反复追问，阈值 0.85；WARN 不 FAIL——它是体验缺陷而非链路故障。
    """
    normalized: list[tuple[str, str]] = []
    for item in transcripts:
        if item.role != "agent":
            continue
        norm = _normalize_for_similarity(item.text)
        if len(norm) < 8:  # 短句（问候/确认）跳过
            continue
        normalized.append((norm, item.text))
    for i, (norm, raw) in enumerate(normalized):
        similar = 1
        for other_norm, _other_raw in normalized[i + 1 :]:
            if SequenceMatcher(None, norm, other_norm).ratio() >= 0.85:
                similar += 1
        if similar >= 3:
            return CheckResult(
                "no_redundant_reask",
                "9. 无复读式追问",
                "WARN",
                f"近似同句追问 {similar} 次: {_clip(raw)}",
            )
    return CheckResult("no_redundant_reask", "9. 无复读式追问", "PASS", "无高相似追问 ≥3 次")


def check_normal_ending(events: Sequence[Event], meta: Event | None = None) -> CheckResult:
    ended_events = [event for event in events if event.get("type") == "ended"]
    if not ended_events:
        return CheckResult("normal_ending", "6. 正常收尾", "FAIL", "未找到 ended 事件")

    failures: list[str] = []
    duration = _duration_seconds(events, meta)
    # 上限 = 外呼硬时限 + 收尾余量：到点后还要跨一个判定 tick、说完告别语、
    # 延迟挂断（HANGUP_TOOL_DELAY 默认 4.5s）再落盘。真机实测兜底收尾最长 170.5s
    # （2026-07-10），余量 20s 会把正常兜底误伤成 FAIL——放宽到 30s。
    max_duration = float(os.environ.get("OUTBOUND_MAX_SECONDS", "150") or 150) + 30
    if duration is None:
        failures.append("无法确定通话时长")
    elif duration >= max_duration:
        failures.append(f"通话时长 {duration:.1f}s >= {max_duration:.0f}s（硬时限+收尾余量）")

    failed_statuses = [status for status in _statuses(events, meta) if status.lower() == "failed"]
    if failed_statuses:
        failures.append("status=failed")

    if failures:
        return CheckResult("normal_ending", "6. 正常收尾", "FAIL", "；".join(failures))
    duration_detail = f"{duration:.1f}s" if duration is not None else "unknown"
    detail = f"ended 存在，duration={duration_detail}，status 未 failed"
    return CheckResult("normal_ending", "6. 正常收尾", "PASS", detail)


def check_dtmf_audit(events: Sequence[Event], transcripts: Sequence[Transcript]) -> CheckResult:
    dtmf_events = [event for event in events if event.get("type") == "dtmf"]
    press_line = next((item for item in transcripts if item.role == "agent" and PRESS_RE.search(item.text)), None)

    if press_line is not None and not dtmf_events:
        return CheckResult(
            "dtmf_audit",
            "7. DTMF 审计一致",
            "WARN",
            f"agent 说了按键但没有 dtmf 事件: {_clip(press_line.text)}",
        )

    bad_modes = [str(event.get("mode") or "") for event in dtmf_events if event.get("mode") != "inband"]
    if bad_modes:
        return CheckResult(
            "dtmf_audit",
            "7. DTMF 审计一致",
            "WARN",
            f"存在非 inband DTMF mode: {', '.join(bad_modes)}",
        )

    if dtmf_events:
        return CheckResult("dtmf_audit", "7. DTMF 审计一致", "PASS", "所有 dtmf 事件 mode=inband")
    return CheckResult("dtmf_audit", "7. DTMF 审计一致", "PASS", "未出现按键表述，也无 dtmf 事件")


def check_dtmf_followup(events: Sequence[Event]) -> CheckResult:
    """Observe whether the agent stays silent until the next remote transcript.

    This deliberately does not classify menu text as success/failure. IVR wording
    is open-ended; the timing and transcript excerpt are evidence for a human A/B
    decision, not a keyword-driven IVR state machine.
    """
    observations: list[CheckResult] = []
    for index, event in enumerate(events):
        if event.get("type") != "dtmf" or event.get("result") == "failure":
            continue
        sent_at = _number(event.get("ts"))
        if sent_at is None:
            observations.append(CheckResult(
                "dtmf_followup",
                "8. DTMF 后静默与菜单观测",
                "WARN",
                "成功 dtmf 事件缺少 ts，无法计算后续菜单延迟",
            ))
            continue

        remote_candidates: list[tuple[float, str]] = []
        immediate_agent: tuple[float, str] | None = None
        for later in events[index + 1 :]:
            if later.get("type") == "dtmf":
                break
            if later.get("type") != "transcript":
                continue
            at = _number(later.get("ts"))
            text = later.get("text")
            if at is None or not isinstance(text, str) or not text.strip():
                continue
            role = later.get("role")
            delay = max(0.0, at - sent_at)
            if (
                role == "agent"
                and immediate_agent is None
                and delay <= DTMF_SILENCE_GUARD_SECONDS
            ):
                immediate_agent = (delay, text.strip())
            elif (
                role == "user"
                and delay <= DTMF_OBSERVATION_SECONDS
                and len(remote_candidates) < 3
            ):
                remote_candidates.append((delay, text.strip()))

        if immediate_agent is not None:
            observations.append(CheckResult(
                "dtmf_followup",
                "8. DTMF 后静默与菜单观测",
                "FAIL",
                f"DTMF 后 {immediate_agent[0]:.1f}s Agent 在静默保护窗内说话: "
                f"{_clip(immediate_agent[1])}",
            ))
            continue

        if not remote_candidates:
            later_remote: tuple[float, str] | None = None
            for later in events[index + 1 :]:
                if later.get("type") == "dtmf":
                    break
                later_at = _number(later.get("ts"))
                later_text = later.get("text")
                if (
                    later.get("type") == "transcript"
                    and later.get("role") == "user"
                    and later_at is not None
                    and isinstance(later_text, str)
                    and later_text.strip()
                ):
                    later_remote = (
                        max(0.0, later_at - sent_at),
                        later_text.strip(),
                    )
                    break
            later_detail = ""
            if later_remote is not None:
                later_detail = f"；首段在 {later_remote[0]:.1f}s: {_clip(later_remote[1])}"
            observations.append(CheckResult(
                "dtmf_followup",
                "8. DTMF 后静默与菜单观测",
                "WARN",
                f"DTMF 后 {DTMF_OBSERVATION_SECONDS:g}s 观测窗内无后续对端转写"
                f"{later_detail}",
            ))
            continue
        candidates = " | ".join(
            f"+{delay:.1f}s {_clip(text, 60)}" for delay, text in remote_candidates
        )
        observations.append(CheckResult(
            "dtmf_followup",
            "8. DTMF 后静默与菜单观测",
            "PASS",
            f"Agent 静默 ≥{DTMF_SILENCE_GUARD_SECONDS:g}s；"
            f"对端候选（人工判菜单是否推进）: {candidates}",
        ))

    if not observations:
        return CheckResult(
            "dtmf_followup",
            "8. DTMF 后静默与菜单观测",
            "PASS",
            "无已发送 DTMF 事件",
        )
    for status in ("FAIL", "WARN"):
        result = next((item for item in observations if item.status == status), None)
        if result is not None:
            return result
    if len(observations) == 1:
        return observations[0]
    return CheckResult(
        "dtmf_followup",
        "8. DTMF 后静默与菜单观测",
        "PASS",
        f"{len(observations)} 次 DTMF 后均先收到对端转写",
    )


def check_dtmf_judge_shadow(
    events: Sequence[Event], private_entries: Sequence[Event]
) -> CheckResult:
    """Summarize shadow/realtime alignment without printing cleartext digits."""
    title = "10. DTMF 判官 shadow 对齐"
    public_decisions = [event for event in events if event.get("type") == "dtmf_judge"]
    public_actions = [event for event in events if event.get("type") == "dtmf_action"]
    judge_errors = sum(event.get("type") == "judge_error" for event in events)
    if not public_decisions:
        if judge_errors or public_actions or private_entries:
            return CheckResult(
                "dtmf_judge_shadow",
                title,
                "WARN",
                f"decisions=0 errors={judge_errors} unjudged_action={len(public_actions)}",
            )
        return CheckResult(
            "dtmf_judge_shadow", title, "PASS", "本通未产生 shadow 判定"
        )

    schema_errors = 0
    missing_private = 0
    private_decisions, duplicate_private_decisions = _index_private_entries(
        private_entries, "decision", "decision_id"
    )
    private_actions, duplicate_private_actions = _index_private_entries(
        private_entries, "action", "action_id"
    )
    schema_errors += duplicate_private_decisions + duplicate_private_actions

    decisions: list[Event] = []
    seen_decision_ids: set[str] = set()
    for public in public_decisions:
        decision_id = public.get("decision_id")
        if not isinstance(decision_id, str) or decision_id in seen_decision_ids:
            schema_errors += 1
            continue
        seen_decision_ids.add(decision_id)
        private = private_decisions.get(decision_id)
        if private is None:
            missing_private += 1
            continue
        if not _valid_decision_pair(public, private):
            schema_errors += 1
            continue
        decisions.append(private)

    actions: list[Event] = []
    seen_action_ids: set[str] = set()
    for public in public_actions:
        action_id = public.get("action_id")
        if not isinstance(action_id, str) or action_id in seen_action_ids:
            schema_errors += 1
            continue
        seen_action_ids.add(action_id)
        private = private_actions.get(action_id)
        if private is None:
            missing_private += 1
            continue
        if not _valid_action_pair(public, private):
            schema_errors += 1
            continue
        actions.append(private)

    schema_errors += len(set(private_decisions) - seen_decision_ids)
    schema_errors += len(set(private_actions) - seen_action_ids)
    press_decisions = [entry for entry in decisions if entry.get("action") == "press"]
    nonpress_decisions = [entry for entry in decisions if entry.get("action") != "press"]
    exact = 0
    mismatch = 0
    no_action = 0
    unused_action_ids = {
        action_id
        for action in actions
        for action_id in [action.get("action_id")]
        if isinstance(action_id, str)
    }
    for decision in sorted(
        press_decisions, key=lambda item: _number(item.get("ts")) or 0.0
    ):
        decision_ts = _number(decision.get("ts"))
        decision_digits = decision.get("digits")
        if decision_ts is None or not isinstance(decision_digits, str):
            missing_private += 1
            continue
        candidates = [
            action
            for action in actions
            if action.get("action_id") in unused_action_ids
            if (action_ts := _number(action.get("ts"))) is not None
            and abs(action_ts - decision_ts) <= 5.0
        ]
        if not candidates:
            no_action += 1
            continue
        nearest = min(
            candidates,
            key=lambda action: abs((_number(action.get("ts")) or 0.0) - decision_ts),
        )
        nearest_id = nearest.get("action_id")
        if isinstance(nearest_id, str):
            unused_action_ids.discard(nearest_id)
        if nearest.get("digits") == decision_digits:
            exact += 1
        else:
            mismatch += 1

    unexpected_action = 0
    unused_nonpress = list(nonpress_decisions)
    for action in actions:
        action_id = action.get("action_id")
        if action_id not in unused_action_ids:
            continue
        action_ts = _number(action.get("ts"))
        if action_ts is None:
            continue
        candidates = [
            decision
            for decision in unused_nonpress
            if (decision_ts := _number(decision.get("ts"))) is not None
            and abs(action_ts - decision_ts) <= 5.0
        ]
        if not candidates:
            continue
        nearest = min(
            candidates,
            key=lambda decision: abs(
                action_ts - (_number(decision.get("ts")) or 0.0)
            ),
        )
        unused_nonpress.remove(nearest)
        if isinstance(action_id, str):
            unused_action_ids.discard(action_id)
        unexpected_action += 1
    unjudged_action = len(unused_action_ids)

    modes: dict[str, int] = {}
    for decision in decisions:
        mode = decision.get("window_mode")
        label = mode if isinstance(mode, str) else "unknown"
        modes[label] = modes.get(label, 0) + 1
    mode_detail = ",".join(f"{key}={value}" for key, value in sorted(modes.items()))
    detail = (
        f"decisions={len(decisions)} press={len(press_decisions)} exact={exact} "
        f"mismatch={mismatch} no_action={no_action} "
        f"unexpected_action={unexpected_action} unjudged_action={unjudged_action} "
        f"missing_private={missing_private} schema_errors={schema_errors} "
        f"errors={judge_errors}; "
        f"window_mode[{mode_detail or 'none'}]"
    )
    status: Status = (
        "WARN"
        if (
            mismatch
            or no_action
            or unexpected_action
            or unjudged_action
            or missing_private
            or schema_errors
            or judge_errors
        )
        else "PASS"
    )
    return CheckResult("dtmf_judge_shadow", title, status, detail)


def _index_private_entries(
    entries: Sequence[Event], kind: str, id_field: str
) -> tuple[dict[str, Event], int]:
    indexed: dict[str, Event] = {}
    duplicates = 0
    for entry in entries:
        if entry.get("kind") != kind:
            continue
        entry_id = entry.get(id_field)
        if not isinstance(entry_id, str) or entry_id in indexed:
            duplicates += 1
            continue
        indexed[entry_id] = entry
    return indexed, duplicates


def _valid_decision_pair(public: Event, private: Event) -> bool:
    action = public.get("action")
    confidence = _number(public.get("confidence"))
    latency = _number(public.get("latency_ms"))
    digits = private.get("digits")
    digits_len = public.get("digits_len")
    if (
        not isinstance(action, str)
        or action not in JUDGE_ACTIONS
        or isinstance(public.get("confidence"), bool)
        or confidence is None
        or not 0.0 <= confidence <= 1.0
        or not isinstance(public.get("reason_code"), str)
        or public.get("reason_code") not in JUDGE_REASON_CODES
        or latency is None
        or latency < 0
        or not isinstance(public.get("window_mode"), str)
        or public.get("window_mode") not in JUDGE_WINDOW_MODES
        or isinstance(digits_len, bool)
        or not isinstance(digits_len, int)
        or not isinstance(public.get("decision_id"), str)
        or _number(public.get("ts")) is None
    ):
        return False
    if action == "press":
        if (
            not isinstance(digits, str)
            or re.fullmatch(r"[0-9*#]{1,4}", digits) is None
            or digits_len != len(digits)
        ):
            return False
    elif digits is not None or digits_len != 0:
        return False
    private_confidence = _number(private.get("confidence"))
    private_latency = _number(private.get("latency_ms"))
    reason = private.get("reason")
    return bool(
        private.get("decision_id") == public.get("decision_id")
        and private.get("action") == action
        and private_confidence == confidence
        and private.get("reason_code") == public.get("reason_code")
        and isinstance(reason, str)
        and len(reason) <= 50
        and private_latency == latency
        and private.get("window_mode") == public.get("window_mode")
        and _number(private.get("ts")) is not None
    )


def _valid_action_pair(public: Event, private: Event) -> bool:
    source = public.get("source")
    digits_len = public.get("digits_len")
    digits = private.get("digits")
    return bool(
        isinstance(public.get("action_id"), str)
        and isinstance(source, str)
        and source in DTMF_ACTION_SOURCES
        and isinstance(digits_len, int)
        and not isinstance(digits_len, bool)
        and digits_len > 0
        and _number(public.get("ts")) is not None
        and private.get("action_id") == public.get("action_id")
        and private.get("source") == source
        and isinstance(digits, str)
        and PRIVATE_DTMF_RE.fullmatch(digits) is not None
        and private.get("digits_len") == digits_len == len(digits)
        and _number(private.get("ts")) is not None
        and _number(private.get("t_ms")) is not None
    )


def load_events(call_dir: Path) -> list[dict[str, object]]:
    path = call_dir / "events.jsonl"
    if not path.exists():
        raise RegressionError(f"events.jsonl 不存在: {path}")
    events: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegressionError(f"{path}:{line_number} JSON 解析失败: {exc}") from exc
        if not isinstance(event, dict):
            raise RegressionError(f"{path}:{line_number} 不是 JSON object")
        events.append(event)
    return events


def load_meta(call_dir: Path) -> dict[str, object] | None:
    path = call_dir / "meta.json"
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegressionError(f"{path} JSON 解析失败: {exc}") from exc
    if not isinstance(meta, dict):
        raise RegressionError(f"{path} 不是 JSON object")
    return meta


def load_judge_shadow(call_dir: Path) -> list[dict[str, object]]:
    """Load the optional gitignored cleartext analysis file for local-only replay."""
    path = call_dir / "judge_shadow.jsonl"
    if not path.exists():
        return []
    entries: list[dict[str, object]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegressionError(
                f"{path}:{line_number} JSON 解析失败: {exc}"
            ) from exc
        if not isinstance(entry, dict):
            raise RegressionError(f"{path}:{line_number} 不是 JSON object")
        entries.append(entry)
    return entries


def find_latest_recording(recordings_dir: Path = RECORDINGS_DIR) -> Path:
    if not recordings_dir.exists():
        raise RegressionError(f"录音目录不存在: {recordings_dir}")
    candidates = [path for path in recordings_dir.iterdir() if path.is_dir() and (path / "events.jsonl").exists()]
    if not candidates:
        raise RegressionError(f"未找到可分析的 events.jsonl: {recordings_dir}")
    return max(candidates, key=lambda path: path.name)


def resolve_recording_path(recording: str, cwd: Path | None = None) -> Path:
    raw_path = Path(recording).expanduser()
    base_dir = cwd if cwd is not None else Path.cwd()
    path = raw_path if raw_path.is_absolute() else base_dir / raw_path
    path = path.resolve()
    if not path.exists():
        raise RegressionError(f"录音目录不存在: {path}")
    if not path.is_dir():
        raise RegressionError(f"录音路径不是目录: {path}")
    return path


def list_recording_dirs(recordings_dir: Path = RECORDINGS_DIR) -> set[Path]:
    if not recordings_dir.exists():
        return set()
    return {path for path in recordings_dir.iterdir() if path.is_dir()}


def _service_recordings_dir(api_base: str = API_BASE) -> Path | None:
    """向运行中的服务询问录音根目录（/api/meta.recordings_dir）；拿不到返回 None。"""
    try:
        with urllib.request.urlopen(f"{api_base}/api/meta", timeout=5) as response:
            meta = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    raw = meta.get("recordings_dir") if isinstance(meta, dict) else None
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return None


def resolve_recordings_dir(
    cli_value: str | None = None,
    *,
    api_base: str = API_BASE,
    ask_service: bool = True,
) -> Path:
    """确定录音根目录（issue #15：开发版与打包版目录不同，猜错就会空等超时）。

    优先级：``--recordings-dir`` 显式 > 运行中服务的 /api/meta（SSOT）>
    ``CALL_LOG_DIR`` 环境变量 > 打包版数据目录（存在时）> 仓库 data/recordings。
    """
    if cli_value:
        return Path(cli_value).expanduser()
    if ask_service:
        from_service = _service_recordings_dir(api_base)
        if from_service is not None:
            return from_service
    env_value = os.environ.get("CALL_LOG_DIR", "").strip()
    if env_value:
        return Path(env_value).expanduser()
    if BUNDLED_RECORDINGS_DIR.is_dir():
        return BUNDLED_RECORDINGS_DIR
    return REPO_RECORDINGS_DIR


def dial_call(task: str) -> None:
    body = {"number": CALL_NUMBER, "task": task, "preset_task": task}
    _post_json("/api/call/dial", body)


def hangup_call() -> str | None:
    try:
        _post_json("/api/call/hangup", {})
    except RegressionError as exc:
        return str(exc)
    return None


def wait_for_finished_recording(
    recordings_dir: Path,
    before_dirs: set[Path],
    timeout_seconds: int,
    poll_interval: float = 1.0,
) -> WaitResult:
    deadline = time.monotonic() + timeout_seconds

    def _scan() -> tuple[Path | None, Path | None]:
        """返回 (已结束的目录, 最新出现的目录)。"""
        newest: Path | None = None
        new_dirs = sorted(
            list_recording_dirs(recordings_dir) - before_dirs,
            key=lambda path: path.name,
            reverse=True,
        )
        for call_dir in new_dirs:
            if newest is None:
                newest = call_dir
            if _recording_finished(call_dir):
                return call_dir, newest
        return None, newest

    latest_new: Path | None = None
    while time.monotonic() < deadline:
        finished, newest = _scan()
        latest_new = newest or latest_new
        if finished is not None:
            return WaitResult(path=finished, timed_out=False, detail="通话已结束")
        time.sleep(poll_interval)
    # 超时兜底再扫一次：极短通话可能恰在最后一个 poll 间隙内结束。
    finished, newest = _scan()
    latest_new = newest or latest_new
    if finished is not None:
        return WaitResult(path=finished, timed_out=False, detail="通话已结束")
    return WaitResult(path=latest_new, timed_out=True, detail=f"等待 {timeout_seconds}s 后仍未结束")


def print_report(report: Report, call_dir: Path | None = None) -> None:
    if call_dir is not None:
        print(f"Recording: {call_dir}")
    for result in report.results:
        print(f"[{result.status}] {result.title} - {result.detail}")
    counts = {
        status: sum(1 for result in report.results if result.status == status)
        for status in ("PASS", "WARN", "FAIL")
    }
    exit_text = "exit 1" if counts["FAIL"] else "exit 0"
    print(f"SUMMARY: PASS={counts['PASS']} WARN={counts['WARN']} FAIL={counts['FAIL']} ({exit_text})")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    timeout_result: CheckResult | None = None
    call_dir: Path | None = None

    try:
        if args.recording is not None:
            call_dir = resolve_recording_path(args.recording)
        elif args.no_dial:
            call_dir = find_latest_recording(resolve_recordings_dir(args.recordings_dir))
        else:
            recordings_dir = resolve_recordings_dir(args.recordings_dir)
            before_dirs = list_recording_dirs(recordings_dir)
            dial_call(args.task)
            wait_result = wait_for_finished_recording(recordings_dir, before_dirs, args.wait)
            call_dir = wait_result.path
            if wait_result.timed_out:
                hangup_error = hangup_call()
                detail = wait_result.detail
                if hangup_error:
                    detail = f"{detail}；挂断请求失败: {hangup_error}"
                else:
                    detail = f"{detail}；已请求挂断"
                    # 挂断是异步收尾：events/meta 落盘还要 1-2s，等一小段再读，
                    # 否则必然撞上"events.jsonl 不存在"（2026-07-10 真机复现）。
                    if call_dir is not None:
                        deadline = time.monotonic() + 10.0
                        while time.monotonic() < deadline and not _recording_finished(call_dir):
                            time.sleep(0.5)
                timeout_result = CheckResult("wait_finished", "0. 等待通话结束", "FAIL", detail)
                if call_dir is None:
                    report = Report([timeout_result])
                    print_report(report)
                    return 1

        if call_dir is None:
            raise RegressionError("未找到通话录音目录")
        events = load_events(call_dir)
        meta = load_meta(call_dir)
        judge_shadow = load_judge_shadow(call_dir)
        transcripts = extract_transcripts(events)
        report = run_assertions(events, transcripts, meta, judge_shadow)
        if timeout_result is not None:
            report = Report([timeout_result, *report.results])
        print_report(report, call_dir)
        return 1 if report.has_failures else 0
    except RegressionError as exc:
        results: list[CheckResult] = []
        if timeout_result is not None:
            results.append(timeout_result)
        results.append(CheckResult("runner_error", "0. 回归脚本运行", "FAIL", str(exc)))
        report = Report(results)
        print_report(report, call_dir)
        return 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dial 10000 and assert call events/transcripts.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-dial", action="store_true", help="不拨打，分析录音根目录下最近一通录音")
    mode.add_argument("--recording", help="不拨打，分析指定录音目录（绝对或相对路径）")
    parser.add_argument(
        "--recordings-dir",
        help="录音根目录；缺省依次取运行中服务的 /api/meta、CALL_LOG_DIR、打包版数据目录、仓库 data/recordings",
    )
    parser.add_argument("--task", default=DEFAULT_TASK, help=f"外呼任务，默认：{DEFAULT_TASK}")
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT_SECONDS, help="等待通话结束的秒数，默认 180")
    args = parser.parse_args(argv)
    if args.wait <= 0:
        parser.error("--wait 必须是正整数")
    task = str(args.task).strip()
    if not task:
        parser.error("--task 不能为空")
    args.task = task
    return args


def _post_json(path: str, body: object) -> dict[str, object]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RegressionError(f"POST {path} HTTP {exc.code}: {payload}") from exc
    except urllib.error.URLError as exc:
        raise RegressionError(f"POST {path} 失败: {exc}") from exc

    try:
        decoded = json.loads(payload) if payload else {}
    except json.JSONDecodeError as exc:
        raise RegressionError(f"POST {path} 返回非 JSON: {payload[:200]}") from exc
    if not isinstance(decoded, dict):
        raise RegressionError(f"POST {path} 返回不是 JSON object")
    if decoded.get("ok") is False:
        raise RegressionError(f"POST {path} 返回 ok=false: {decoded}")
    return decoded


def _recording_finished(call_dir: Path) -> bool:
    if (call_dir / "meta.json").exists():
        return True
    events_path = call_dir / "events.jsonl"
    if not events_path.exists():
        return False
    try:
        return any(
            json.loads(line).get("type") == "ended"
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line
        )
    except (OSError, json.JSONDecodeError):
        return False


def _duration_seconds(events: Sequence[Event], meta: Event | None) -> float | None:
    if meta is not None:
        duration = _number(meta.get("duration"))
        if duration is not None:
            return duration

    ended_events = [event for event in events if event.get("type") == "ended"]
    if ended_events:
        for key in ("duration", "duration_seconds"):
            duration = _number(ended_events[-1].get(key))
            if duration is not None:
                return duration
        t_ms = _number(ended_events[-1].get("t_ms"))
        if t_ms is not None:
            return t_ms / 1000.0

    started_ts = next((_number(event.get("ts")) for event in events if event.get("type") == "call_started"), None)
    ended_ts = next((_number(event.get("ts")) for event in reversed(events) if event.get("type") == "ended"), None)
    if started_ts is not None and ended_ts is not None:
        return max(0.0, ended_ts - started_ts)
    return None


def _statuses(events: Sequence[Event], meta: Event | None) -> list[str]:
    statuses: list[str] = []
    for event in events:
        if event.get("type") in {"ended", "call_finished"}:
            status = event.get("status")
            if isinstance(status, str):
                statuses.append(status)
    if meta is not None:
        status = meta.get("status")
        if isinstance(status, str):
            statuses.append(status)
    return statuses


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _find_owner_name(events: Sequence[Event]) -> str | None:
    for event in events:
        for field_name in OWNER_FIELD_NAMES:
            value = event.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _clip(text: str, limit: int = 120) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 1]}…"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
