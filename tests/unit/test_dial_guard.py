"""Pure dial preflight policy tests for modem and SIM readiness."""

from __future__ import annotations

import pytest

from agentcall.dial_guard import check_dial_guard
from agentcall.sim_identity import UNKNOWN_SIM, SimIdentity, identify


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


@pytest.mark.parametrize("cs_stat", ["0", "1", "2", "3", "4", "5", None])
@pytest.mark.parametrize("eps_stat", ["0", "1", "2", "3", "4", "5", None])
def test_guard_cs_eps_registration_matrix(cs_stat, eps_stat):
    sim = identify(
        "460000123456789\r\nOK",
        f"+CREG: 0,{cs_stat}" if cs_stat is not None else "ERROR",
        f"+CEREG: 0,{eps_stat}" if eps_stat is not None else "ERROR",
    )
    failure = check_dial_guard(modem_online=True, sim_identity=sim, number="10086")

    if cs_stat in {"4", None}:
        assert failure is not None and failure.code == "SIM_NOT_READY"
    elif cs_stat in {"1", "5"} or eps_stat in {"1", "5"}:
        assert failure is None
    else:
        assert failure is not None and failure.code == "SIM_NOT_REGISTERED"
        assert f"CS：{sim.reg_status}" in failure.message
        assert f"LTE：{sim.eps_status}" in failure.message


def test_eps_registration_does_not_bypass_other_guards():
    sim = identify("460000123456789\r\nOK", "+CREG: 0,3", "+CEREG: 0,1")
    for online, identity, number, expected in [
        (False, sim, "10086", "MODEM_OFFLINE"),
        (True, identify("ERROR", "+CREG: 0,3", "+CEREG: 0,1"), "10086", "SIM_NOT_READY"),
        (True, sim, "10010", "SERVICE_NUMBER_MISMATCH"),
        (True, identify("234159876543210", "+CREG: 0,3", "+CEREG: 0,1"), "10086", "SERVICE_NUMBER_UNKNOWN"),
    ]:
        failure = check_dial_guard(modem_online=online, sim_identity=identity, number=number)
        assert failure is not None and failure.code == expected


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


# ---- 非大陆卡：本卡客服号未知时必须拦下（WIL-133）----


def _unknown_carrier_sim() -> SimIdentity:
    """识别不到运营商的卡：service_number 为空——旧代码在这里整个失效。"""
    return SimIdentity(
        present=True,
        plmn="23415",
        carrier="未知",
        service_number="",
        registered=True,
        reg_status="已注册",
    )


def test_known_hotline_blocked_when_this_sims_hotline_is_unknown():
    """「知道这是某家的客服号，但确认不了是不是你的」属于该拦的一档。

    旧写法多一个 `and sim_identity.service_number`，于是识别不到运营商时
    条件短路、保护静默失效。放行的代价是话费（跨运营商客服号按普通通话计费，
    从美国卡拨 10086 更是国际长途）；拦错的代价只是去填一行 CARRIER_HOTLINE。
    """
    sim = _unknown_carrier_sim()
    for number in ("10086", "10000", "10010"):
        failure = check_dial_guard(
            modem_online=True, sim_identity=sim, number=number
        )
        assert failure is not None, f"{number} 必须被拦下"
        assert failure.code == "SERVICE_NUMBER_UNKNOWN"
        assert "CARRIER_HOTLINE" in failure.message, "要告诉用户怎么解开"


def test_unknown_carrier_still_allows_ordinary_numbers():
    """收紧只针对已知客服号，普通号码不受影响。"""
    assert check_dial_guard(
        modem_online=True, sim_identity=_unknown_carrier_sim(),
        number="13800000000",
    ) is None


def test_mainland_sim_behaviour_is_unchanged():
    """大陆卡的判定路径必须与改动前完全一致。"""
    sim = _sim(service_number="10086")
    assert check_dial_guard(
        modem_online=True, sim_identity=sim, number="10086"
    ) is None
    mismatch = check_dial_guard(
        modem_online=True, sim_identity=sim, number="10000"
    )
    assert mismatch is not None and mismatch.code == "SERVICE_NUMBER_MISMATCH"


def test_us_sim_gets_611_and_cross_carrier_hotlines_are_blocked():
    """美国卡：611 放行，中国客服号拦下（那是国际长途）。"""
    from agentcall.sim_identity import identify

    sim = identify("+CIMI: 310410123456789", "+CREG: 0,1")
    assert sim.service_number == "611"
    assert check_dial_guard(
        modem_online=True, sim_identity=sim, number="611"
    ) is None
    blocked = check_dial_guard(
        modem_online=True, sim_identity=sim, number="10086"
    )
    assert blocked is not None and blocked.code == "SERVICE_NUMBER_MISMATCH"


def test_known_numbers_derive_from_the_carrier_table():
    """护栏集合从运营商表派生，避免两处漂移导致新运营商不被保护。"""
    from agentcall.dial_guard import KNOWN_SERVICE_NUMBERS
    from agentcall.sim_identity import VALID_SERVICE_NUMBERS

    assert KNOWN_SERVICE_NUMBERS == VALID_SERVICE_NUMBERS
    assert "611" in KNOWN_SERVICE_NUMBERS
