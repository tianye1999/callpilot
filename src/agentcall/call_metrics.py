"""每通通话的指标汇总（WIL-95）：从事件流生成 ``metrics.json``。

契约要点（与 WIL-95 规格逐条对应）：

- **schema_version 是硬要求**：任何字段的定义变更必须升版本；汇总工具按版本
  分组，拒绝跨版本直接平均（改了定义不升版本，历史数据会静默变得不可比，
  且不会报错——最危险的一类问题）。
- **测不了/条件未满足的指标记 null + 原因**（``unavailable`` 表），**绝不记 0**
  ——0 会被读成「瞬时完成」。
- **命名诚实**：``local_response`` 而非 ``e2e``——蜂窝两腿在观测范围外
  （WIL-95 2.1），把本地测量叫端到端会系统性低估。
- **只读事件、不碰音频**：音频派生指标（每轮出声时长、打断率等）归 WIL-89
  离线审计工具——两个生产者、一份契约，字段定义以先落盘者为准。
- **隐私（WIL-95 §7）**：不含转写文本、任务目标原文、按键值、任何凭证——
  字数/次数/布尔这类派生量除外。

本模块是纯函数（events 进、dict 出），落盘由 ``call_log.CallRecord.finish``
调用；判定层（任务是否达成）刻意不在这里——那是可重算的派生视图
（WIL-95 §4，见 ``task_verdict``），不写进不可变的原始记录。
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Any

# 指标契约版本。
# v1（2026-08-14）：三个逐轮时延 + 配置快照 + 基础 rollup。
# v2（2026-08-14）：新增 termination / hangup_latency_ms / tool_call_latency_ms /
#   contact_known / takeover / has_task_goal（WIL-95 第二期补埋点）。
# v3（2026-08-17）：新增 answered_to_first_audio_ms（文档 A 组「接起→首字」，
#   到达口径）/ takeover_latency_ms / abnormal_drop / pcm_enable_failed +
#   pcm_degraded（文档 D 组异常掉话的蜂窝指纹：CPCMREG 启用行为，WIL-95 第二期收尾）。
# v4（2026-08-18）：新增 hold_seconds / hold_periods（WIL-120 二期：收尾裁判
#   判 on_hold 的 hold_started/hold_ended 事件聚合；文档 G 组「排队等待检测」）。
# v5: takeover rejection reasons and request-to-AI-reclaim latency (#131).
# v6 (#130): startup_marks_ms and distinct local-write playout metrics. Legacy
# arrival metrics retain their definition and are null for local-write events.
# v7 (#130): native modem greeting acknowledgement/timing, separate from AI playout.
# （#131 与 #130 在各自分支上都从 v4 起号，合入 product/physical-sim 时顺延为 v5–v7。）
# v8: shared local-tail -> actual-write latency and separate MOSS preparation/provider timings.
# v9: MOSS outbound preparation is measured before dialing, separate from answered wait.
SCHEMA_VERSION = 9

# 逐轮时延指标：events.jsonl 里 latency 事件的 stage → metrics.json 字段名。
# 各 stage 的精确定义（2026-08-14）：
# - local_response：本地音尾 → 下行首块回复**到达**，每轮一条（call_agent 埋点；
#   覆盖上行传输 + VAD 判停 + 生成，不含播出排队与蜂窝两腿）
# - first_audio_delta：provider 判停(speech_stopped) → 首个音频增量到达
#   （openai_agent 埋点；语音到语音架构下 TTFT/TTFB 的唯一可观测合并量，
#   WIL-95 2.2——local provider 三段式下语义不同，看 config.provider 归因）
# - playout_backlog：本轮首块回复入队时，前面已排未播的音频时长（WIL-112
#   实测体感 3~5s 的主因；0 是有效读数=无积压）
_LATENCY_STAGES = {
    "local_response": "local_response_latency_ms",
    # Same local peak-VAD tail, but stops on the first confirmed hardware write.
    # Keep the historical arrival metric unchanged for existing local comparisons.
    "local_response_playout": "local_response_playout_ms",
    "moss_input_stop_to_audio": "moss_input_stop_to_audio_ms",
    "moss_session_setup": "moss_session_setup_ms",
    "moss_briefing": "moss_briefing_ms",
    "moss_answered_wait": "moss_answered_wait_ms",
    "moss_prebrief": "moss_prebrief_ms",
    "first_audio_delta": "first_audio_delta_ms",
    "playout_backlog": "playout_backlog_ms",
}

# 汇总层（metrics_report / 看板）拉取的全部时延字段；tool_call 单列——
# 它是每次工具调用一条，不是每轮一条，语义与上面三个不同。
LATENCY_FIELDS = [*_LATENCY_STAGES.values(), "tool_call_latency_ms"]

# 本版本先天测不了/未埋点的指标及原因——字段常驻，等埋点补上后从这里移走。
# 打断类的原因按 barge_in 开关分岔（WIL-95 2.3），在 build 时动态填。
# takeover_latency_ms 已于 v3 移走（takeover_requested 事件其实一直在，
# call_agent.py _request_owner_takeover 处；原「无该事件」注释系误判）。
_STATIC_UNAVAILABLE = {
    "e2e_latency_ms": "carrier_legs_unobservable",  # WIL-95 2.1：不要假装能测
    "scenario": "not_tagged",  # 场景矩阵②③④⑤维度标签：任务预设/判官标注，后补
}

# hangup_latency 的「对话尾声」取这些事件里最晚的 ts（2026-08-14 定义 v2）：
# transcript（转写到达≈该轮说完）/ dtmf_outcome（按键推进判定）/
# tool_call（最后一次工具动作）。这是**事件近似**，不含播出尾巴；
# 判「滞留/误挂」仍要判官读转写裁决（WIL-95 C 类），此值只是量化证据。
_ACTIVITY_EVENT_TYPES = ("transcript", "dtmf_outcome", "tool_call")


def _nearest_rank_p90(ordered: list[float]) -> float:
    """最近秩 p90：ceil(0.9n)-1。int(0.9n) 在 n=10 时取到最大值，小样本会
    系统性偏高（与 scripts/naturalness_audit.py 同一实现与教训）。"""
    rank = max(1, math.ceil(0.9 * len(ordered)))
    return ordered[rank - 1]


def _describe(values: list[float]) -> dict[str, Any] | None:
    """n/median/p90/max + 原始值；空样本返回 None（落到 unavailable，不记 0）。

    保留 ``values``（升序）是刻意的：单通样本数很小（通常 <50），多存几十个
    数换来跨通分位数**精确可算**——汇总工具只读 metrics.json，不必回头重解析
    全部 events.jsonl（WIL-76 全量重扫退化的教训）。
    """
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "median": round(statistics.median(ordered), 1),
        "p90": round(_nearest_rank_p90(ordered), 1),
        "max": round(ordered[-1], 1),
        "values": [round(v, 1) for v in ordered],
    }


def _derive_termination(
    status: str,
    *,
    takeover: dict[str, int],
    dead_media_hangup: bool,
    winddown_reason: str | None,
    hangup_tool_called: bool,
    inbound_deadline: bool,
) -> dict[str, str]:
    """通话终止归类（2026-08-14 定义 v2，全部由既有事件派生）。

    优先级：未接通 > 接管 > 媒体已死 > 错误 > AI 主动收尾（细分起因）>
    对端先挂（排除法兜底——主循环因对端挂断而结束时本机没有任何主动信号）。
    """
    if status == "not_connected":
        return {"kind": "not_connected", "reason": "not_connected"}
    if takeover.get("committed"):
        return {"kind": "takeover", "reason": "takeover_committed"}
    if dead_media_hangup:
        return {"kind": "dead_media", "reason": "uplink_digital_silence"}
    if status not in ("completed",):
        return {"kind": "error", "reason": status}
    if winddown_reason:
        return {"kind": "agent_hangup", "reason": winddown_reason}
    if hangup_tool_called:
        return {"kind": "agent_hangup", "reason": "hangup_tool"}
    if inbound_deadline:
        return {"kind": "agent_hangup", "reason": "inbound_deadline"}
    return {"kind": "peer_hangup", "reason": "inferred_no_local_signal"}


def build_call_metrics(
    events: list[dict[str, Any]],
    *,
    call_id: str,
    direction: str,
    duration_s: float,
    answered: bool,
    status: str,
    generated_at: float | None = None,
) -> dict[str, Any]:
    """从一通通话的事件流构建 metrics.json 内容（纯函数，不做 IO）。"""
    latencies: dict[str, list[float]] = {stage: [] for stage in _LATENCY_STAGES}
    tool_latencies: list[float] = []
    config_snapshot: dict[str, Any] | None = None
    first_audio_ms: float | None = None
    first_playout_ms: float | None = None
    first_playout_boundary: str | None = None
    startup_marks_ms: dict[str, float] = {}
    module_greeting: dict[str, Any] | None = None
    agent_turn_chars: list[float] = []
    peer_turns = 0
    tool_calls: dict[str, int] = {}
    dtmf_actions = 0
    dtmf_outcomes: dict[str, int] = {}
    barge_in_fallback = False
    contact_known: bool | None = None
    has_task_goal = False
    winddown_reason: str | None = None
    inbound_deadline = False
    dead_media_hangup = False
    hangup_tool_called = False
    takeover: dict[str, int] = {}
    takeover_unavailable_reasons: dict[str, int] = {}
    takeover_reclaim_ms: list[float] = []
    finished_ts: float | None = None
    last_activity_ts: float | None = None
    answered_t_ms: float | None = None
    greeting_t_ms: float | None = None
    takeover_requested_ts: float | None = None
    takeover_committed_ts: float | None = None
    pcm_enable: dict[str, Any] | None = None
    hold_open_ts: float | None = None
    hold_seconds_total = 0.0
    hold_periods = 0
    wrap_up_judge_on: bool | None = None
    local_turns: dict[int, dict[str, Any]] = {}
    local_opening: dict[str, Any] | None = None

    for event in events:
        etype = event.get("type")
        if etype == "local_turn" and isinstance(event.get("turn_id"), int):
            turn_id = event["turn_id"]
            turn = local_turns.setdefault(turn_id, {"turn_id": turn_id, "stages": {}})
            stage = event.get("stage")
            if isinstance(stage, str):
                turn["stages"].setdefault(stage, {
                    k: event[k] for k in ("t_ms", "ms", "kind") if k in event
                })
        elif etype == "local_opening":
            local_opening = {k: event[k] for k in ("hit", "prepare_ms") if k in event}
        if etype in {
            "answered", "pcm_ready", "bridge_started", "agent_connect_started", "agent_connected",
            "greeting_requested", "agent_audio_received", "first_audio", "module_greeting",
        }:
            t_ms = event.get("t_ms")
            if isinstance(t_ms, (int, float)) and math.isfinite(t_ms) and t_ms >= 0:
                startup_marks_ms.setdefault(str(etype), float(t_ms))
        ts = event.get("ts")
        if etype in _ACTIVITY_EVENT_TYPES and isinstance(ts, (int, float)):
            if last_activity_ts is None or ts > last_activity_ts:
                last_activity_ts = float(ts)
        if etype == "latency":
            stage = event.get("stage")
            ms = event.get("ms")
            if not isinstance(ms, (int, float)):
                continue
            if stage in latencies:
                latencies[stage].append(float(ms))
            elif stage == "tool_call":
                tool_latencies.append(float(ms))
        elif etype == "config_snapshot":
            config_snapshot = {
                k: v for k, v in event.items() if k not in ("type", "ts")
            }
        elif etype == "module_greeting" and isinstance(event.get("ok"), bool):
            # Native TTS has no host PCM and OK does not prove far-end audibility.
            # Whitelist only result/timings; never copy the configured greeting text.
            module_greeting = {"ok": event["ok"]}
            for field in ("duration_ms", "started_after_answer_ms", "t_ms"):
                value = event.get(field)
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    module_greeting[field] = value
        elif etype == "first_audio":
            ms = event.get("ms")
            boundary = event.get("boundary")
            if boundary in {"pcm_written", "audio_device_written", "playback_pipe_written"}:
                first_playout_boundary = str(boundary)
                if isinstance(ms, (int, float)):
                    first_playout_ms = float(ms)
            elif isinstance(ms, (int, float)):
                first_audio_ms = float(ms)
        elif etype == "transcript":
            role = event.get("role")
            text = event.get("text") or ""
            if role == "agent":
                agent_turn_chars.append(float(len(text)))
            elif role == "user":
                peer_turns += 1
        elif etype == "tool_call":
            tool = str(event.get("tool") or "unknown")
            tool_calls[tool] = tool_calls.get(tool, 0) + 1
            if tool == "hangup_call":
                result = event.get("result")
                if isinstance(result, dict) and result.get("success"):
                    hangup_tool_called = True
        elif etype == "dtmf_action":
            dtmf_actions += 1
        elif etype == "dtmf_outcome":
            outcome_status = str(event.get("status") or "unknown")
            dtmf_outcomes[outcome_status] = dtmf_outcomes.get(outcome_status, 0) + 1
        elif etype == "barge_in_fallback":
            barge_in_fallback = True
        elif etype == "call_context":
            value = event.get("contact_known")
            if isinstance(value, bool):
                contact_known = value
        elif etype == "task_goal":
            has_task_goal = True  # 只记有无，目标原文不进 metrics（隐私）
        elif etype == "winddown":
            winddown_reason = str(event.get("reason") or "") or winddown_reason
        elif etype == "inbound_hard_deadline":
            inbound_deadline = True
        elif etype == "dead_media_detected":
            if event.get("hangup"):
                dead_media_hangup = True
        elif isinstance(etype, str) and etype.startswith("takeover_"):
            suffix = etype[len("takeover_"):]
            takeover[suffix] = takeover.get(suffix, 0) + 1
            if suffix == "unavailable":
                reason = str(event.get("reason") or "unknown")
                takeover_unavailable_reasons[reason] = takeover_unavailable_reasons.get(reason, 0) + 1
            elif suffix == "timeout_reclaimed":
                elapsed = event.get("elapsed_ms")
                if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and math.isfinite(elapsed):
                    if elapsed >= 0:
                        takeover_reclaim_ms.append(float(elapsed))
            # 首次请求→首次接通即接管时延；重复事件（理论上不会有）取最早的。
            if isinstance(ts, (int, float)):
                if suffix == "requested" and takeover_requested_ts is None:
                    takeover_requested_ts = float(ts)
                elif suffix == "committed" and takeover_committed_ts is None:
                    takeover_committed_ts = float(ts)
        elif etype == "answered":
            t_ms = event.get("t_ms")
            if isinstance(t_ms, (int, float)) and answered_t_ms is None:
                answered_t_ms = float(t_ms)
        elif etype == "greeting_sent":
            t_ms = event.get("t_ms")
            if isinstance(t_ms, (int, float)) and greeting_t_ms is None:
                greeting_t_ms = float(t_ms)
        elif etype == "pcm_enable":
            pcm_enable = {
                "ok": bool(event.get("ok")),
                "attempts": event.get("attempts"),
                "degraded": bool(event.get("degraded")),
            }
        elif etype == "call_limits":
            value = event.get("wrap_up_judge")
            if isinstance(value, bool):
                wrap_up_judge_on = value
        elif etype == "hold_started":
            if hold_open_ts is None and isinstance(ts, (int, float)):
                hold_open_ts = float(ts)
        elif etype == "hold_ended":
            hold_periods += 1
            seconds = event.get("seconds")
            if isinstance(seconds, (int, float)):
                hold_seconds_total += max(0.0, float(seconds))
            elif hold_open_ts is not None and isinstance(ts, (int, float)):
                hold_seconds_total += max(0.0, float(ts) - hold_open_ts)
            hold_open_ts = None
        elif etype == "call_finished" and isinstance(ts, (int, float)):
            finished_ts = float(ts)

    unavailable = dict(_STATIC_UNAVAILABLE)
    if module_greeting is None:
        unavailable["module_greeting"] = "no_module_greeting_event"
    if config_snapshot is None:
        unavailable["config"] = "config_snapshot_event_missing"
    # 打断类（WIL-95 2.3）：随 BARGE_IN_ENABLED 分岔。开着也还没埋点（WIL-94
    # E2E 之后补），关着结构上不发生——原因必须区分，混在一起没法归因。
    barge_in = bool(config_snapshot.get("barge_in_enabled")) if config_snapshot else False
    interruption_reason = (
        "not_instrumented_yet" if barge_in and not barge_in_fallback
        else "half_duplex_mode"
    )
    unavailable["interruption_latency_ms"] = interruption_reason
    unavailable["post_interruption_recovery"] = interruption_reason
    # 音频派生指标归 WIL-89 离线审计；录音关闭时连离线也无从谈起。
    recording = bool(config_snapshot.get("recording_enabled")) if config_snapshot else False
    unavailable["audio_derived_metrics"] = (
        "offline_audit_wil89" if recording else "recording_disabled"
    )

    latency_out: dict[str, Any] = {}
    for stage, field in _LATENCY_STAGES.items():
        described = _describe(latencies[stage])
        if described is None:
            unavailable[field] = "no_samples"
        else:
            latency_out[field] = described
    tool_described = _describe(tool_latencies)
    if tool_described is None:
        unavailable["tool_call_latency_ms"] = "no_samples"
    else:
        latency_out["tool_call_latency_ms"] = tool_described
    if first_audio_ms is None:
        unavailable["first_audio_ms"] = (
            "local_write_use_first_playout_ms" if first_playout_boundary else "no_samples"
        )
    if contact_known is None:
        unavailable["contact_known"] = "no_call_context_event"
    if not takeover_reclaim_ms:
        unavailable["takeover_reclaim_latency_ms"] = "no_timeout_reclaim"

    # hangup_latency_ms（v2 定义见 _ACTIVITY_EVENT_TYPES 注释）：对话尾声
    # → 通话实际结束。agent 主动收尾时包含刻意的道别延时，属定义内。
    hangup_latency_ms: float | None = None
    if finished_ts is not None and last_activity_ts is not None:
        gap = (finished_ts - last_activity_ts) * 1000
        if gap >= 0:
            hangup_latency_ms = round(gap, 1)
    if hangup_latency_ms is None:
        unavailable["hangup_latency_ms"] = "no_dialogue_activity"

    # answered_to_first_audio_ms（v3，文档 A 组「Inbound 接起→首字」）：
    # 接起(answered) → 整通首块 AI 音频**到达** = (greeting_sent - answered 的
    # t_ms 差) + first_audio_ms。命名诚实：到达≠对方听到（播出与蜂窝腿在外）。
    # opening_mode=wait（不发开场白）或远程链路（answered 无 t_ms）→ null + 原因。
    answered_to_first_audio_ms: float | None = None
    if answered_t_ms is None:
        unavailable["answered_to_first_audio_ms"] = "no_answered_mark"
    elif greeting_t_ms is None:
        unavailable["answered_to_first_audio_ms"] = "greeting_not_sent"
    elif first_audio_ms is None:
        unavailable["answered_to_first_audio_ms"] = "no_first_audio"
    elif greeting_t_ms < answered_t_ms:
        unavailable["answered_to_first_audio_ms"] = "inconsistent_marks"
    else:
        answered_to_first_audio_ms = round(
            (greeting_t_ms - answered_t_ms) + first_audio_ms, 1
        )

    # Local output acceptance, not provider arrival or measured far-end audibility.
    # Keep separate keys so existing reports never pool the two boundaries.
    answered_to_first_playout_ms: float | None = None
    first_write_t_ms = startup_marks_ms.get("first_audio") if first_playout_boundary else None
    if answered_t_ms is not None and first_write_t_ms is not None and first_write_t_ms >= answered_t_ms:
        answered_to_first_playout_ms = round(first_write_t_ms - answered_t_ms, 1)
    else:
        unavailable["answered_to_first_playout_ms"] = "no_consistent_local_write_marks"
    if first_playout_ms is None:
        unavailable["first_playout_ms"] = (
            "greeting_not_requested" if first_playout_boundary else "no_local_write_sample"
        )

    # takeover_latency_ms（v3，文档 F 组「转接建立时延」）：首次 takeover_requested
    # → 首次 takeover_committed。请求了没接通（超时/回滚）记 null + 原因。
    takeover_latency_ms: float | None = None
    if takeover_requested_ts is None:
        unavailable["takeover_latency_ms"] = "no_takeover_request"
    elif (
        takeover_committed_ts is None
        or takeover_committed_ts < takeover_requested_ts
    ):
        unavailable["takeover_latency_ms"] = "takeover_not_committed"
    else:
        takeover_latency_ms = round(
            (takeover_committed_ts - takeover_requested_ts) * 1000, 1
        )

    # pcm_enable_failed / pcm_degraded（v3，文档 D 组异常掉话的蜂窝指纹）：
    # 启用失败=整通无声；attempts≥阈值=模组劣化（对端听到卡顿）。事件缺失
    # （Quectel 路径 / 历史通话）记 unavailable，不猜。
    if pcm_enable is None:
        unavailable["pcm_enable"] = "no_pcm_enable_event"

    # hold_seconds（v4）：通话结束仍未退出 hold（挂在排队里被硬时限收尾）时，
    # 未闭合区间按 call_finished 时刻收口——不然最惨的排队一秒都记不上。
    if hold_open_ts is not None:
        close_ts = finished_ts if finished_ts is not None else last_activity_ts
        if close_ts is not None and close_ts > hold_open_ts:
            hold_seconds_total += close_ts - hold_open_ts
            hold_periods += 1
    hold_seconds: float | None = None
    if direction != "outbound":
        unavailable["hold_seconds"] = "outbound_only"
    elif wrap_up_judge_on is False:
        # 裁判被 profile 显式关闭 = hold 检测不在场，0 会被读成「没排过队」。
        unavailable["hold_seconds"] = "hold_detection_disabled"
    elif wrap_up_judge_on is None:
        unavailable["hold_seconds"] = "no_call_limits_event"
    else:
        hold_seconds = round(hold_seconds_total, 1)

    termination = _derive_termination(
        status,
        takeover=takeover,
        dead_media_hangup=dead_media_hangup,
        winddown_reason=winddown_reason,
        hangup_tool_called=hangup_tool_called,
        inbound_deadline=inbound_deadline,
    )

    metrics: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        # 调用方（finish）传通话结束时刻，保持纯函数不读钟；直调时才落到 now。
        "generated_at": time.time() if generated_at is None else generated_at,
        "call_id": call_id,
        "direction": direction,
        "duration_s": round(duration_s, 1),
        "answered": answered,
        "status": status,
        "config": config_snapshot,
        "latency": latency_out,
        # greeting 下发→整通首块音频到达；**不是**文档的「接起→首字」——
        # 那要用 answered/greeting_sent 事件的 t_ms 推导（WIL-95 A 类注）。
        "first_audio_ms": first_audio_ms,
        "first_playout_ms": first_playout_ms,
        "answered_to_first_playout_ms": answered_to_first_playout_ms,
        "first_playout_boundary": first_playout_boundary,
        "startup_marks_ms": startup_marks_ms,
        "module_greeting": module_greeting,
        "agent_turn_chars": _describe(agent_turn_chars),
        "peer_turns": peer_turns,
        "tool_calls": tool_calls,
        "dtmf": {"actions": dtmf_actions, "outcomes": dtmf_outcomes},
        "barge_in_fallback": barge_in_fallback,
        "termination": termination,
        "hangup_latency_ms": hangup_latency_ms,
        "answered_to_first_audio_ms": answered_to_first_audio_ms,
        "takeover": takeover,
        "takeover_unavailable_reasons": takeover_unavailable_reasons,
        "takeover_reclaim_latency_ms": _describe(takeover_reclaim_ms),
        "takeover_latency_ms": takeover_latency_ms,
        "contact_known": contact_known,
        "has_task_goal": has_task_goal,
        # 非预期断线（媒体死/错误终止）；文档 D 组要求的直方图在汇总层按时长出。
        "abnormal_drop": termination["kind"] in ("dead_media", "error"),
        "hold_seconds": hold_seconds,
        "hold_periods": hold_periods if hold_seconds is not None else None,
        "pcm_enable_failed": None if pcm_enable is None else not pcm_enable["ok"],
        "pcm_degraded": None if pcm_enable is None else pcm_enable["degraded"],
        "pcm_enable_attempts": None if pcm_enable is None else pcm_enable["attempts"],
        "unavailable": unavailable,
    }
    # 关闭埋点时不改变旧 metrics 结构；每轮缺失阶段保留缺失，不伪造零延迟。
    if local_turns or local_opening is not None:
        metrics["local_pipeline"] = {"turns": list(local_turns.values()), "opening": local_opening}
    release_causes = [
        {key: event[key] for key in ("raw", "source", "cause") if key in event}
        for event in events if event.get("type") == "release_cause"
    ]
    if release_causes:
        metrics["release_causes"] = release_causes
    return metrics
