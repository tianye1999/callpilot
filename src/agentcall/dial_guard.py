"""Pure, fail-closed dial readiness policy shared by local and remote calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .sim_identity import SimIdentity

KNOWN_SERVICE_NUMBERS = frozenset({"10000", "10010", "10086", "10099"})
_EXPLICIT_UNREGISTERED = frozenset({"未注册", "搜网中", "注册被拒"})


@dataclass(frozen=True)
class DialGuardFailure:
    code: Literal[
        "MODEM_OFFLINE",
        "SIM_NOT_READY",
        "SIM_NOT_REGISTERED",
        "SERVICE_NUMBER_MISMATCH",
    ]
    message: str


def check_dial_guard(
    *,
    modem_online: bool,
    sim_identity: SimIdentity | None,
    number: str | None,
) -> DialGuardFailure | None:
    """Return the first readiness failure, or ``None`` when dialing is allowed.

    ``sim_identity=None`` means a legacy duck-typed modem that cannot report SIM
    state. Production ``Eg25Modem`` always exposes an identity, including its
    fail-closed ``UNKNOWN_SIM`` sentinel.
    """
    if not modem_online:
        return DialGuardFailure("MODEM_OFFLINE", "模组未连接，请检查 USB 连接")
    if sim_identity is None:
        return None
    if not sim_identity.present or (
        not sim_identity.network_attached
        and sim_identity.reg_status not in _EXPLICIT_UNREGISTERED
    ):
        return DialGuardFailure("SIM_NOT_READY", "SIM 卡未插入或尚未就绪")
    # 看 network_attached 而非 registered：CS 域被拒但 EPS(LTE)已注册时语音走
    # VoLTE，照样能拨通（真机实测：中国电信 46011 + SIM7600G，CREG:0,3 而
    # ATD10000 得到 VOICE CALL: BEGIN 并接通）。只看 CS 域会把可用的卡拦死。
    if not sim_identity.network_attached:
        return DialGuardFailure(
            "SIM_NOT_REGISTERED",
            f"SIM 卡尚未注册到网络（CS：{sim_identity.reg_status}；"
            f"LTE：{sim_identity.eps_status}）",
        )
    normalized = (number or "").strip()
    if (
        normalized in KNOWN_SERVICE_NUMBERS
        and sim_identity.service_number
        and normalized != sim_identity.service_number
    ):
        return DialGuardFailure(
            "SERVICE_NUMBER_MISMATCH",
            f"当前 SIM 运营商为{sim_identity.carrier}，免费客服号应为"
            f"{sim_identity.service_number}",
        )
    return None
