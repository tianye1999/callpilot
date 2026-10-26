"""Fail-closed incoming policy for the owner's HFP phone only."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import config
from .number_profiles import canonical_dial_number, same_dial_number


def normalized_number(value: str | None) -> str:
    # canonical_dial_number is permissive for dial profiles; a caller allowlist
    # must reject hidden/textual caller IDs instead of extracting their digits.
    if not value or not re.fullmatch(r"\+?[0-9\s()\-]+", value.strip()):
        return ""
    number = canonical_dial_number(value)
    if number.startswith("0086"):
        number = "+86" + number[4:]
    return number if re.fullmatch(r"\+?[0-9]{1,32}", number) else ""


@dataclass
class IncomingCall:
    number: str | None
    deadline: float | None
    offered: bool = False
    session_started: bool = False
    ai_answered: bool = False
    owner_answered: bool = False
    allowed_numbers: tuple[str, ...] = ()

    @classmethod
    def ringing(cls, number: str | None, now: float, *, waiting: bool) -> IncomingCall:
        mode = config.get_str("INBOUND_AI_ANSWER_MODE").strip().lower()
        incoming = cls(number, None)
        if not waiting:
            if mode == "allowlist":
                incoming.allowed_numbers = tuple(
                    normalized_number(item) for item in config.get_str("INBOUND_AI_ALLOWLIST").split(",")
                )
                incoming.identify(number, now)
            elif mode == "unanswered":
                delay = config.get_int("INBOUND_AI_ANSWER_AFTER_SECONDS")
                if 0 < delay <= 600:
                    incoming.deadline = now + delay
        return incoming

    def identify(self, number: str | None, now: float) -> None:
        self.number = number
        caller = normalized_number(number)
        if caller and any(item and same_dial_number(caller, item) for item in self.allowed_numbers):
            self.deadline = now


def masked_number(number: str | None) -> str:
    digits = re.sub(r"[^0-9]", "", number or "")
    return "***" + digits[-4:] if digits else "unknown"
