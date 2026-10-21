"""Pure, fail-closed dial readiness policy shared by local and remote calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .sim_identity import VALID_SERVICE_NUMBERS, SimIdentity

# 从 sim_identity 的运营商表派生，不再手抄一份（WIL-133）：两处一旦漂移，
# 新增运营商的客服号就会「表里有、护栏不认」，误拨保护对那张卡直接失效。
KNOWN_SERVICE_NUMBERS = VALID_SERVICE_NUMBERS
_EXPLICIT_UNREGISTERED = frozenset({"未注册", "搜网中", "注册被拒"})


@dataclass(frozen=True)
class DialGuardFailure:
    code: Literal[
        "MODEM_OFFLINE",
        "SIM_NOT_READY",
        "SIM_NOT_REGISTERED",
        "SERVICE_NUMBER_MISMATCH",
        "SERVICE_NUMBER_UNKNOWN",
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
    # 仅放宽明确的 CS 未注册状态:EPS 已注册时可走 VoLTE,未就绪判定保持不变。
    if not sim_identity.network_attached:
        return DialGuardFailure(
            "SIM_NOT_REGISTERED",
            f"SIM 卡尚未注册到网络（CS：{sim_identity.reg_status}；LTE：{sim_identity.eps_status}）",
        )
    normalized = (number or "").strip()
    if normalized in KNOWN_SERVICE_NUMBERS:
        if not sim_identity.service_number:
            # 本卡客服号未知时**拦下**而不是放行（WIL-133）。旧写法多一个
            # `and sim_identity.service_number`，于是非大陆卡（识别不到运营商）
            # 上整条保护静默失效：真机 2026-08-18 起本机是美国 AT&T 卡，实测拨
            # 10086 一路放行——那是打到中国的国际长途，按话费计。
            # 「知道这是某家的客服号，但确认不了是不是你的」属于该拦的一档：
            # 放行的代价是话费，拦错的代价只是去填一行配置。
            return DialGuardFailure(
                "SERVICE_NUMBER_UNKNOWN",
                f"{normalized} 是已知的运营商客服号，但当前 SIM 的运营商未能识别"
                f"（{sim_identity.carrier}），无法确认它对本卡免费。"
                "如确为本卡免费客服号，请在设置里填写 CARRIER_HOTLINE。",
            )
        if normalized != sim_identity.service_number:
            return DialGuardFailure(
                "SERVICE_NUMBER_MISMATCH",
                f"当前 SIM 运营商为{sim_identity.carrier}，免费客服号应为"
                f"{sim_identity.service_number}",
            )
    return None
