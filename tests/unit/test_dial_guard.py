"""Pure dial preflight policy tests for modem and SIM readiness."""

from __future__ import annotations

from agentcall.dial_guard import check_dial_guard
from agentcall.sim_identity import UNKNOWN_SIM, SimIdentity


def _sim(
    *,
    registered: bool = True,
    reg_status: str = "已注册",
    service_number: str = "10086",
) -> SimIdentity:
    return SimIdentity(
        present=True,
        plmn="46000",
        carrier="中国移动",
        service_number=service_number,
        registered=registered,
        reg_status=reg_status,
    )


def test_guard_order_starts_with_transport_then_sim_readiness():
    failure = check_dial_guard(
        modem_online=False, sim_identity=UNKNOWN_SIM, number="10010"
    )
    assert failure is not None and failure.code == "MODEM_OFFLINE"

    failure = check_dial_guard(
        modem_online=True, sim_identity=UNKNOWN_SIM, number="10010"
    )
    assert failure is not None and failure.code == "SIM_NOT_READY"


def test_guard_distinguishes_unknown_from_explicit_non_registration():
    unknown = check_dial_guard(
        modem_online=True,
        sim_identity=_sim(registered=False, reg_status="未知"),
        number="10086",
    )
    rejected = check_dial_guard(
        modem_online=True,
        sim_identity=_sim(registered=False, reg_status="注册被拒"),
        number="10086",
    )

    assert unknown is not None and unknown.code == "SIM_NOT_READY"
    assert rejected is not None and rejected.code == "SIM_NOT_REGISTERED"


def test_guard_blocks_only_known_cross_carrier_service_numbers():
    mismatch = check_dial_guard(
        modem_online=True, sim_identity=_sim(), number="10010"
    )
    same_carrier = check_dial_guard(
        modem_online=True, sim_identity=_sim(), number="10086"
    )
    ordinary_number = check_dial_guard(
        modem_online=True, sim_identity=_sim(), number="13900000000"
    )

    assert mismatch is not None and mismatch.code == "SERVICE_NUMBER_MISMATCH"
    assert same_carrier is None
    assert ordinary_number is None


def test_missing_identity_capability_preserves_legacy_duck_typed_modems():
    assert check_dial_guard(
        modem_online=True, sim_identity=None, number="10000"
    ) is None


# ---- VoLTE-only 卡：CS 域被拒不该拦住拨号 ----


def test_volte_only_card_is_allowed_to_dial():
    """回归：中国电信卡 CREG:0,3（CS 被拒）但 CEREG:0,1，真机 ATD 能接通。

    2026-08-04 真机实测 ATD10000 得到 VOICE CALL: BEGIN 并接通，而 UI 却报
    「SIM 卡尚未注册到网络（注册被拒）」把按钮拦死——门禁只看 CS 域的后果。
    """
    from agentcall.sim_identity import identify, with_eps_registration

    sim = with_eps_registration(
        identify("460110123456789\r\nOK", "+CREG: 0,3\r\nOK"), "+CEREG: 0,1\r\nOK"
    )
    assert check_dial_guard(modem_online=True, sim_identity=sim, number="10000") is None


def test_both_domains_denied_still_blocks():
    """两个域都没注册才该拦——否则真拨不出去只能等 45s 接通超时。"""
    from agentcall.sim_identity import identify, with_eps_registration

    sim = with_eps_registration(
        identify("460110123456789\r\nOK", "+CREG: 0,3\r\nOK"), "+CEREG: 0,3\r\nOK"
    )
    failure = check_dial_guard(modem_online=True, sim_identity=sim, number="10000")
    assert failure is not None
    assert failure.code == "SIM_NOT_REGISTERED"
    # 两个域的状态都要报出来，否则用户看不出是哪边不通
    assert "CS：注册被拒" in failure.message and "LTE：注册被拒" in failure.message


def test_cs_registered_alone_still_allowed():
    """移动/联通卡走 CS 域，行为不能因这次改动变化（回归保护）。"""
    from agentcall.sim_identity import identify

    sim = identify("460010123456789\r\nOK", "+CREG: 0,1\r\nOK")
    assert sim.eps_registered is False        # 没读 EPS 域
    assert check_dial_guard(modem_online=True, sim_identity=sim, number="10010") is None
