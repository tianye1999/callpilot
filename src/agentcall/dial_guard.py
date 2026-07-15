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
        not sim_identity.registered
        and sim_identity.reg_status not in _EXPLICIT_UNREGISTERED
    ):
        return DialGuardFailure("SIM_NOT_READY", "SIM 卡未插入或尚未就绪")
    if not sim_identity.registered:
        return DialGuardFailure(
            "SIM_NOT_REGISTERED",
            f"SIM 卡尚未注册到网络（{sim_identity.reg_status}）",
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
