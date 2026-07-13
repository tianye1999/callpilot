"""Conservative parsing for an Agent's spoken DTMF execution intent."""

from __future__ import annotations

import re

_DIGIT_MAP = str.maketrans(
    {
        "零": "0",
        "〇": "0",
        "一": "1",
        "幺": "1",
        "二": "2",
        "两": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
    }
)
_INTENT_RE = re.compile(
    r"(?:^|[\s，。；！,;!])"
    r"我(?:来|帮(?:您|你))?按(?:一下|下)?(?:键)?\s*"
    r"(?P<digits>(?:[0-9零〇一二三四五六七八九幺两#*]|井号|星号|\s)+)"
)
_BLOCKED_RE = re.compile(
    r"[?？]|(?:吗|么|吧|是否|是不是|要不要)\s*[?？。！]?$|"
    r"(?:不|没|没有|还没|别|不用|不要)\s*(?:来|帮(?:您|你))?按|"
    r"我(?:来|帮(?:您|你))?按(?:错|错了)|"
    r"(?:如果|要是|假如|除非|还是|或者|或是)|"
    r"(?<!帮)(?:您按|你按)|"
    r"(?:请按|他说|她说|它说|对方说|客服说|系统说|系统提示|菜单说|播报|原话|复述)"
)


def extract_spoken_dtmf(text: str) -> str | None:
    """Return a legal DTMF sequence from a narrow affirmative self-statement.

    The guard deliberately rejects questions, negation, conditions and quoted
    menu wording. It executes an action the Agent says it is taking; it does not
    interpret the remote party's IVR menu.
    """

    normalized = " ".join(str(text or "").strip().split())
    if not normalized or _BLOCKED_RE.search(normalized):
        return None
    match = _INTENT_RE.search(normalized)
    if match is None:
        return None
    raw = match.group("digits").replace("井号", "#").replace("星号", "*")
    digits = re.sub(r"\s+", "", raw).translate(_DIGIT_MAP)
    if not re.fullmatch(r"[0-9*#]{1,32}", digits):
        return None
    return digits
