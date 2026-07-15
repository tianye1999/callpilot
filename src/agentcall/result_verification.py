"""Fail-closed verification of high-value call results against trusted SMS."""

from __future__ import annotations

import re
from typing import Any, Iterable


def _service_address(value: Any) -> str:
    address = str(value or "").strip()
    return address if re.fullmatch(r"\d+", address) else ""


def is_carrier_service_call(dialed_number: Any, service_number: Any) -> bool:
    """Return whether the outbound target is this SIM's public service number."""
    dialed = _service_address(dialed_number)
    service = _service_address(service_number)
    return bool(dialed and service and dialed == service)


def carrier_sms_evidence(
    events: Iterable[dict[str, Any]],
    *,
    service_number: str,
    started_at: float,
    ended_at: float | None = None,
) -> list[dict[str, Any]]:
    """Return official carrier messages received in one call's time window.

    Association is deliberately strict: inbound SMS only, exact normalized
    public service number, non-empty body, and an ingestion timestamp no older
    than the call. No IMSI or subscriber number is involved.
    """
    expected = _service_address(service_number)
    if not expected:
        return []
    matched: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "sms_in":
            continue
        if _service_address(event.get("sender")) != expected:
            continue
        text = str(event.get("text") or "").strip()
        raw_timestamp = event.get("ts")
        if not isinstance(raw_timestamp, (int, float, str)):
            continue
        try:
            received_at = float(raw_timestamp)
        except (TypeError, ValueError):
            continue
        if received_at < started_at:
            continue
        if ended_at is not None and received_at > ended_at:
            continue
        if not text:
            continue
        matched.append(
            {"sender": expected, "text": text, "ts": received_at}
        )
    return matched


def _summary_defaults(lang: str) -> dict[str, Any]:
    if lang == "en":
        return {
            "caller_identity": "unknown",
            "intent": "carrier account enquiry",
            "urgency": "medium",
        }
    return {
        "caller_identity": "未知",
        "intent": "运营商账户查询",
        "urgency": "中",
    }


def apply_carrier_sms_verification(
    model_result: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    lang: str = "zh",
) -> dict[str, Any]:
    """Enforce SMS authority without asking the model to certify itself.

    A matched official message replaces the model-authored conclusion entirely,
    so a misheard amount cannot survive in ``summary``. Without evidence the
    transcript remains visible, but is explicitly and structurally unverified.
    """
    language = "en" if str(lang).lower().startswith("en") else "zh"
    defaults = _summary_defaults(language)
    result = {
        "ok": True,
        "caller_identity": model_result.get("caller_identity")
        or defaults["caller_identity"],
        "intent": model_result.get("intent") or defaults["intent"],
        "urgency": model_result.get("urgency") or defaults["urgency"],
        "callback_needed": bool(model_result.get("callback_needed", False)),
        "error": None,
    }
    if evidence:
        bodies = "\n".join(str(item["text"]) for item in evidence)
        prefix = (
            "Verified by official carrier SMS: "
            if language == "en"
            else "已由官方运营商短信核实："
        )
        result.update(
            {
                "summary": prefix + bodies,
                "result_source": "carrier_sms",
                "result_verification": "verified",
                "evidence": evidence,
            }
        )
        return result

    transcript_summary = str(model_result.get("summary") or "").strip()
    prefix = (
        "Pending verification: no official carrier SMS was received for this call. "
        "The transcript is for reference only."
        if language == "en"
        else "待核实：未收到本次通话对应的官方运营商短信，通话听写仅供参考。"
    )
    result.update(
        {
            "summary": f"{prefix} {transcript_summary}".strip(),
            "result_source": "transcript",
            "result_verification": "unverified",
            "evidence": [],
        }
    )
    return result
