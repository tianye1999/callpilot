"""Shadow DTMF decision judge driven by remote-party transcripts.

The judge never sends DTMF in shadow mode. It runs on one daemon worker,
coalesces transcript fragments, and keeps cleartext decisions only beside the
private per-call recording artifacts. Public events deliberately contain no
DTMF value or value-derived fingerprint.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, cast

import dashscope

from .summarizer import _extract_text

logger = logging.getLogger(__name__)

JudgeAction = Literal["press", "wait", "speak", "human", "unknown"]
JudgeReasonCode = Literal[
    "menu_matched",
    "menu_incomplete",
    "queue_hold",
    "human_detected",
    "ambiguous",
    "other",
]
DtmfActionSource = Literal["realtime", "judge", "guard"]
WindowMode = Literal["merged", "fragmented"]

_ACTIONS = frozenset({"press", "wait", "speak", "human", "unknown"})
_REASON_CODES = frozenset(
    {
        "menu_matched",
        "menu_incomplete",
        "queue_hold",
        "human_detected",
        "ambiguous",
        "other",
    }
)
_ACTION_SOURCES = frozenset({"realtime", "judge", "guard"})
_DIGITS_RE = re.compile(r"^[0-9*#]{1,4}$")
_EXPECTED_FIELDS = frozenset(
    {"action", "digits", "confidence", "reason_code", "reason"}
)
_MAX_TRANSCRIPTS = 8
_MAX_RECENT_ACTIONS = 3
_MODEL_SINGLE_FLIGHT_LOCK = threading.Lock()
_MODEL_SINGLE_FLIGHT_THREAD: threading.Thread | None = None

_SYSTEM_PROMPT = (
    "你是电话按键决策器。只输出一个严格合法的 JSON 对象，不要 Markdown 或额外文字。"
    "根据最近的对方话语流、我方任务目标和已按键历史，判断当前正确动作。"
    "只在对方明确给出按键选项，且某选项显然通向任务目标或转人工时才 press；"
    "播报未完、没有菜单或仍在排队时 wait；应由语音助手回答时 speak；"
    "明确接到人工时 human；无法可靠判断时 unknown。不得使用预置关键词映射。"
    '输出字段固定为 action、confidence、reason_code、reason；仅 action="press" 时'
    "增加 digits（1-4 位，仅 0-9、*、#），其他 action 禁止输出 digits。"
    "confidence 必须是 0 到 1 的有限数字，reason 不超过 50 字。"
)


class JudgeRecord(Protocol):
    path: Path

    def log_event(self, type: str, **fields: Any) -> None: ...  # noqa: A002


ModelCall = Callable[
    [list[dict[str, str]], str, float], tuple[str | None, str | None]
]
IdFactory = Callable[[], str]


class JudgeValidationError(ValueError):
    """A model response failed the strict judge contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class JudgeDecision:
    action: JudgeAction
    digits: str | None
    confidence: float
    reason_code: JudgeReasonCode
    reason: str


@dataclass(frozen=True)
class DtmfAction:
    action_id: str
    source: DtmfActionSource
    digits: str
    timestamp: float

    def public_fields(self) -> dict[str, str | int]:
        return {
            "action_id": self.action_id,
            "source": self.source,
            "digits_len": len(self.digits),
        }


class DtmfActionLedger:
    """Thread-safe action history; cleartext digits remain memory-only."""

    def __init__(self, *, id_factory: IdFactory | None = None) -> None:
        self._id_factory = id_factory or _opaque_id
        self._entries: deque[DtmfAction] = deque(maxlen=32)
        self._lock = threading.Lock()

    def record(
        self,
        digits: str,
        source: DtmfActionSource,
        *,
        timestamp: float | None = None,
    ) -> DtmfAction:
        if source not in _ACTION_SOURCES:
            raise ValueError(f"unsupported DTMF action source: {source}")
        normalized = digits.strip()
        if not normalized or not re.fullmatch(r"[0-9*#]+", normalized):
            raise ValueError("DTMF action digits must contain only 0-9, * or #")
        entry = DtmfAction(
            action_id=self._id_factory(),
            source=source,
            digits=normalized,
            timestamp=time.monotonic() if timestamp is None else timestamp,
        )
        with self._lock:
            self._entries.append(entry)
        return entry

    def recent(self, limit: int = _MAX_RECENT_ACTIONS) -> tuple[DtmfAction, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            return tuple(self._entries)[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def parse_judge_decision(text: str) -> JudgeDecision:
    """Parse one strict JSON decision; never coerce unsafe model output."""
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except JudgeValidationError:
        raise
    except (json.JSONDecodeError, TypeError):
        raise JudgeValidationError("invalid_json") from None
    if not isinstance(payload, dict):
        raise JudgeValidationError("invalid_schema")
    if set(payload) - _EXPECTED_FIELDS:
        raise JudgeValidationError("invalid_schema")

    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise JudgeValidationError("invalid_action")

    has_digits = "digits" in payload
    digits = payload.get("digits")
    if action == "press":
        if not isinstance(digits, str) or _DIGITS_RE.fullmatch(digits) is None:
            raise JudgeValidationError("invalid_digits")
    elif has_digits:
        raise JudgeValidationError("unexpected_digits")
    else:
        digits = None

    confidence = payload.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise JudgeValidationError("invalid_confidence")

    reason_code = payload.get("reason_code")
    if not isinstance(reason_code, str) or reason_code not in _REASON_CODES:
        raise JudgeValidationError("invalid_reason_code")
    reason = payload.get("reason")
    if not isinstance(reason, str) or len(reason) > 50:
        raise JudgeValidationError("invalid_reason")

    return JudgeDecision(
        action=cast(JudgeAction, action),
        digits=digits,
        confidence=float(confidence),
        reason_code=cast(JudgeReasonCode, reason_code),
        reason=reason,
    )


def build_judge_messages(
    transcripts: list[tuple[float, str]],
    recent_actions: tuple[DtmfAction, ...],
    task_goal: str,
) -> list[dict[str, str]]:
    """Build the bounded text-model input without enumerating IVR phrases."""
    transcript_window = transcripts[-_MAX_TRANSCRIPTS:]
    action_window = recent_actions[-_MAX_RECENT_ACTIONS:]
    payload = {
        "task_goal": (task_goal or "").strip(),
        "remote_transcripts": [
            {"t_ms": round(t_ms, 1), "text": text}
            for t_ms, text in transcript_window
        ],
        "recent_dtmf": [
            {
                "t_ms": round(entry.timestamp * 1000, 1),
                "source": entry.source,
                "digits": entry.digits,
            }
            for entry in action_window
        ],
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


class DtmfJudge:
    """Single-worker, transcript-batching shadow judge for one call."""

    def __init__(
        self,
        *,
        record: JudgeRecord,
        task_goal: str,
        ledger: DtmfActionLedger,
        model: str,
        window_mode: WindowMode,
        model_call: ModelCall | None = None,
        throttle_seconds: float = 1.5,
        timeout_seconds: float = 3.0,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._record = record
        self._task_goal = task_goal
        self._ledger = ledger
        self._model = model
        self._window_mode = window_mode
        self._model_call = model_call or _default_model_call
        self._throttle_seconds = max(0.0, throttle_seconds)
        self._timeout_seconds = max(0.01, timeout_seconds)
        self._id_factory = id_factory or _opaque_id
        self._condition = threading.Condition()
        self._segments: deque[tuple[float, str]] = deque(maxlen=_MAX_TRANSCRIPTS)
        self._pending = False
        self._deadline = 0.0
        self._running = False
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._private_lock = threading.Lock()
        self._private_lines: list[str] = []

    def start(self) -> None:
        with self._condition:
            if self._running:
                return
            self._running = True
            self._generation += 1
            generation = self._generation
            self._thread = threading.Thread(
                target=self._run,
                args=(generation,),
                name="dtmf-judge-shadow",
                daemon=True,
            )
            self._thread.start()

    def submit_remote_transcript(self, text: str, *, t_ms: float) -> None:
        normalized = (text or "").strip()
        if not normalized:
            return
        with self._condition:
            if not self._running:
                return
            self._segments.append((max(0.0, float(t_ms)), normalized))
            if not self._pending:
                self._pending = True
                self._deadline = time.monotonic() + self._throttle_seconds
            self._condition.notify()

    def stop(self, *, join_timeout: float = 0.2) -> None:
        with self._condition:
            self._running = False
            self._generation += 1
            self._pending = False
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, join_timeout))
        self._flush_private()

    def record_action(self, entry: DtmfAction) -> None:
        """Buffer cleartext action data for the private per-call analysis file."""
        with self._condition:
            if not self._running:
                return
            private = {
                "kind": "action",
                "action_id": entry.action_id,
                "ts": time.time(),
                "t_ms": round(entry.timestamp * 1000, 1),
                "source": entry.source,
                "digits": entry.digits,
                "digits_len": len(entry.digits),
            }
            self._buffer_private_locked(private)

    def _run(self, generation: int) -> None:
        while True:
            with self._condition:
                while self._running and not self._pending:
                    self._condition.wait()
                if not self._running or generation != self._generation:
                    return
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                transcripts = list(self._segments)
                self._pending = False

            started = time.monotonic()
            messages = build_judge_messages(
                transcripts,
                self._ledger.recent(_MAX_RECENT_ACTIONS),
                self._task_goal,
            )
            text, error_code = self._invoke_model(messages)
            latency_ms = round((time.monotonic() - started) * 1000, 1)

            if error_code is not None:
                if not self._commit_error(
                    generation, _sanitize_error_code(error_code), latency_ms
                ):
                    return
                continue
            try:
                decision = parse_judge_decision(text or "")
            except JudgeValidationError as exc:
                if not self._commit_error(generation, exc.code, latency_ms):
                    return
                continue
            if not self._commit_decision(generation, decision, latency_ms):
                return

    def _invoke_model(
        self, messages: list[dict[str, str]]
    ) -> tuple[str | None, str | None]:
        """Enforce the judge timeout even if an SDK call ignores its timeout hint."""
        global _MODEL_SINGLE_FLIGHT_THREAD

        box: dict[str, object] = {}

        def call() -> None:
            global _MODEL_SINGLE_FLIGHT_THREAD
            try:
                box["result"] = self._model_call(
                    messages, self._model, self._timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001
                box["error_type"] = type(exc).__name__
            finally:
                with _MODEL_SINGLE_FLIGHT_LOCK:
                    if _MODEL_SINGLE_FLIGHT_THREAD is threading.current_thread():
                        _MODEL_SINGLE_FLIGHT_THREAD = None

        thread = threading.Thread(
            target=call,
            name="dtmf-judge-model-call",
            daemon=True,
        )
        with _MODEL_SINGLE_FLIGHT_LOCK:
            previous = _MODEL_SINGLE_FLIGHT_THREAD
            if previous is not None and previous.is_alive():
                return None, "timeout"
            _MODEL_SINGLE_FLIGHT_THREAD = thread
            try:
                thread.start()
            except Exception:  # noqa: BLE001
                _MODEL_SINGLE_FLIGHT_THREAD = None
                return None, "model_error"
        thread.join(self._timeout_seconds)
        if thread.is_alive():
            return None, "timeout"
        error_type = box.get("error_type")
        if isinstance(error_type, str):
            logger.warning("DTMF 判官调用异常: error_type=%s", error_type)
            return None, "model_error"
        result = box.get("result")
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not (result[0] is None or isinstance(result[0], str))
            or not (result[1] is None or isinstance(result[1], str))
        ):
            return None, "model_error"
        return result

    def _commit_error(
        self, generation: int, code: str, latency_ms: float
    ) -> bool:
        """Atomically reject stale results or append one public in-memory event."""
        with self._condition:
            if not self._running or generation != self._generation:
                return False
            self._record.log_event(
                "judge_error",
                code=code,
                latency_ms=latency_ms,
                window_mode=self._window_mode,
            )
            return True

    def _commit_decision(
        self,
        generation: int,
        decision: JudgeDecision,
        latency_ms: float,
    ) -> bool:
        """Atomically commit correlated public/private in-memory records."""
        with self._condition:
            if not self._running or generation != self._generation:
                return False
            decision_id = self._id_factory()
            digits_len = len(decision.digits or "")
            timestamp = time.time()
            self._record.log_event(
                "dtmf_judge",
                action=decision.action,
                confidence=decision.confidence,
                reason_code=decision.reason_code,
                latency_ms=latency_ms,
                window_mode=self._window_mode,
                digits_len=digits_len,
                decision_id=decision_id,
            )
            self._buffer_private_locked(
                {
                    "kind": "decision",
                    "decision_id": decision_id,
                    "ts": timestamp,
                    "action": decision.action,
                    "digits": decision.digits,
                    "confidence": decision.confidence,
                    "reason_code": decision.reason_code,
                    "reason": decision.reason,
                    "latency_ms": latency_ms,
                    "window_mode": self._window_mode,
                }
            )
            return True

    def _buffer_private_locked(self, item: dict[str, object]) -> None:
        self._private_lines.append(json.dumps(item, ensure_ascii=False) + "\n")

    def _flush_private(self) -> None:
        with self._condition:
            lines = self._private_lines
            self._private_lines = []
        if not lines:
            return
        path = self._record.path / "judge_shadow.jsonl"
        try:
            with self._private_lock:
                with open(
                    path,
                    "a",
                    encoding="utf-8",
                    opener=lambda file_path, flags: os.open(file_path, flags, 0o600),
                ) as file:
                    file.writelines(lines)
        except OSError as exc:
            logger.warning(
                "DTMF 判官隐私分析记录写入失败: error_type=%s",
                type(exc).__name__,
            )


def _default_model_call(
    messages: list[dict[str, str]], model: str, _timeout: float
) -> tuple[str | None, str | None]:
    """Make one synchronous SDK request; ``_invoke_model`` owns its timeout."""
    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        return None, "model_error"
    response = dashscope.Generation.call(
        model=model,
        messages=messages,
        result_format="message",
        api_key=os.environ.get("DASHSCOPE_API_KEY"),
    )
    status = getattr(response, "status_code", None)
    if status is not None and status != 200:
        return None, "model_error"
    text = _extract_text(response)
    return (text, None) if text else (None, "model_error")


def _sanitize_error_code(code: str) -> str:
    allowed = {
        "timeout",
        "model_error",
        "invalid_json",
        "invalid_schema",
        "invalid_action",
        "invalid_digits",
        "unexpected_digits",
        "invalid_confidence",
        "invalid_reason_code",
        "invalid_reason",
        "duplicate_fields",
    }
    return code if code in allowed else "model_error"


def _opaque_id() -> str:
    return uuid.uuid4().hex


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JudgeValidationError("duplicate_fields")
        result[key] = value
    return result
