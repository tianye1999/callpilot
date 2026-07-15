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
