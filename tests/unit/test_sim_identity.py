"""sim_identity 纯函数单测(#88):IMSI/CREG 解析与运营商映射。"""

from __future__ import annotations

import threading

from agentcall.sim_identity import (
    UNKNOWN_SIM,
    identify,
    parse_creg,
    parse_imsi,
    with_registration,
)

# ---- parse_imsi ----

def test_parse_imsi_typical_response():
    assert parse_imsi("460110123456789\r\n\r\nOK") == "460110123456789"


def test_parse_imsi_14_digit_supported():
    assert parse_imsi("46011012345678\r\nOK") == "46011012345678"


def test_parse_imsi_error_and_garbage_rejected():
    assert parse_imsi("+CME ERROR: SIM not inserted") == ""
    assert parse_imsi("ERROR") == ""
    assert parse_imsi("") == ""
    assert parse_imsi("OK") == ""
    # 13 位太短不是 IMSI
    assert parse_imsi("4601101234567\r\nOK") == ""


# ---- parse_creg ----

def test_parse_creg_registered_states():
    assert parse_creg("+CREG: 0,1\r\nOK") == (True, "已注册")
    assert parse_creg("+CREG: 0,5\r\nOK") == (True, "已注册(漫游)")
    assert parse_creg("+CREG: 1\r\n") == (True, "已注册")
    assert parse_creg("+CREG: 5\r\n") == (True, "已注册(漫游)")


def test_parse_creg_searching_and_denied():
    assert parse_creg("+CREG: 0,2\r\nOK") == (False, "搜网中")
    assert parse_creg("+CREG: 0,3\r\nOK") == (False, "注册被拒")
    assert parse_creg("+CREG: 0,0") == (False, "未注册")


def test_parse_creg_malformed():
    assert parse_creg("") == (False, "未知")
    assert parse_creg("ERROR") == (False, "未知")


def test_with_registration_preserves_sim_identity_fields():
    sim = identify("460000123456789\r\nOK", "+CREG: 0,1")

    updated = with_registration(sim, "+CREG: 2")

    assert updated.present is True
    assert updated.plmn == sim.plmn
    assert updated.carrier == sim.carrier
    assert updated.service_number == sim.service_number
    assert updated.registered is False
    assert updated.reg_status == "搜网中"


# ---- identify:四大运营商映射(全 PLMN 表逐条锁死)----

def test_identify_all_known_plmns():
    expect = {
        "46000": ("中国移动", "10086"), "46002": ("中国移动", "10086"),
        "46004": ("中国移动", "10086"), "46007": ("中国移动", "10086"),
        "46008": ("中国移动", "10086"), "46013": ("中国移动", "10086"),
        "46001": ("中国联通", "10010"), "46006": ("中国联通", "10010"),
        "46009": ("中国联通", "10010"),
        "46003": ("中国电信", "10000"), "46005": ("中国电信", "10000"),
        "46011": ("中国电信", "10000"), "46012": ("中国电信", "10000"),
        "46015": ("中国广电", "10099"),
    }
    for plmn, (carrier, svc) in expect.items():
        sim = identify(f"{plmn}0123456789\r\nOK", "+CREG: 0,1")
        assert (sim.carrier, sim.service_number) == (carrier, svc), plmn
        assert sim.present and sim.plmn == plmn and sim.registered


def test_identify_unknown_plmn_no_service_number():
    sim = identify("310150123456789\r\nOK", "+CREG: 0,1")  # 美国运营商
    assert sim.present and sim.carrier == "未知" and sim.service_number == ""


def test_identify_no_sim_keeps_creg_info():
    sim = identify("+CME ERROR: SIM not inserted", "+CREG: 0,2")
    assert not sim.present
    assert sim.carrier == "未知" and sim.service_number == ""
    assert not sim.registered and sim.reg_status == "搜网中"


def test_identify_as_dict_has_no_full_imsi():
    """as_dict 只暴露 PLMN 前缀,绝不含完整 IMSI(隐私)。"""
    sim = identify("460110123456789\r\nOK", "+CREG: 0,1")
    d = sim.as_dict()
    assert d["plmn"] == "46011"
    assert "460110123456789" not in str(d)


def test_unknown_sim_sentinel():
    assert not UNKNOWN_SIM.present
    assert UNKNOWN_SIM.as_dict()["carrier"] == "未知"


# ---- modem 层接线(#88):refresh_sim_identity 调 AT 并缓存 ----

def _make_modem(monkeypatch, responses: dict):
    from agentcall import modem as modem_mod
    from agentcall.modem import Eg25Modem

    m = Eg25Modem("unused")
    monkeypatch.setattr(m, "_send", lambda cmd: responses.get(cmd, "OK"))
    monkeypatch.setattr(modem_mod.time, "sleep", lambda s: None)  # 免真 sleep
    return m


def test_modem_refresh_caches_and_logs(monkeypatch, caplog):
    modem = _make_modem(monkeypatch, {
        "AT+CIMI": "460030123456789\r\nOK",
        "AT+CREG?": "+CREG: 0,1\r\nOK",
    })
    with caplog.at_level("INFO"):
        modem.refresh_sim_identity()
    sim = modem.sim_identity
    assert sim.carrier == "中国电信" and sim.service_number == "10000"
    assert sim.registered
    assert any("SIM 识别" in r.message for r in caplog.records)
    assert not any("460030123456789" in r.getMessage() for r in caplog.records)  # 日志无完整 IMSI


def test_modem_refresh_at_logic_failure_degrades(monkeypatch):
    """AT 返回 ERROR / 非传输层异常 → 降级 UNKNOWN,不抛(主链路不受影响)。"""

    modem = _make_modem(monkeypatch, {"AT+CIMI": "+CME ERROR: SIM not inserted",
                                       "AT+CREG?": "+CREG: 0,2"})
    modem.refresh_sim_identity()  # 不抛
    assert not modem.sim_identity.present and modem.sim_identity.reg_status == "搜网中"


def test_modem_refresh_serial_error_propagates(monkeypatch):
    """BLOCK-3 回归锁:传输层异常必须上抛给 _open_serial 退避循环,
    绝不能吞成'识别失败'让重连误判成功。"""
    import pytest
    import serial

    from agentcall import modem as modem_mod
    from agentcall.modem import Eg25Modem

    m = Eg25Modem("unused")
    monkeypatch.setattr(modem_mod.time, "sleep", lambda s: None)

    def dead(cmd):
        raise serial.SerialException("device disconnected")

    monkeypatch.setattr(m, "_send", dead)
    with pytest.raises(serial.SerialException):
        m.refresh_sim_identity()


def test_modem_refresh_retries_cimi_power_up_delay(monkeypatch):
    """SIM 上电延迟:前两次 CIMI 空/ERROR,第三次成功 → 最终识别到卡。"""
    from agentcall import modem as modem_mod
    from agentcall.modem import Eg25Modem

    m = Eg25Modem("unused")
    monkeypatch.setattr(modem_mod.time, "sleep", lambda s: None)
    seq = iter(["ERROR", "ERROR", "460080123456789\r\nOK"])
    calls = {"creg": "+CREG: 0,1\r\nOK"}
    def fake_send(cmd):
        return next(seq) if cmd == "AT+CIMI" else calls["creg"]
    monkeypatch.setattr(m, "_send", fake_send)
    m.refresh_sim_identity()
    assert m.sim_identity.present and m.sim_identity.carrier == "中国移动"


def test_modem_default_identity_before_connect():
    from agentcall.modem import Eg25Modem
    from agentcall.sim_identity import UNKNOWN_SIM

    assert Eg25Modem("unused").sim_identity == UNKNOWN_SIM


def test_creg_urc_updates_registration_without_losing_carrier():
    from agentcall.modem import Eg25Modem

    modem = Eg25Modem("unused")
    modem._sim_identity = identify("460000123456789\r\nOK", "+CREG: 0,1")
    events = []
    modem.on_sim_identity(events.append)

    modem._buffer = "\r\n+CREG: 2\r\n"
    modem._process_buffer()
    modem._buffer += "\r\n+CREG: 2\r\n"
    modem._process_buffer()

    assert modem.sim_identity.carrier == "中国移动"
    assert modem.sim_identity.service_number == "10086"
    assert modem.sim_identity.reg_status == "搜网中"
    assert len(events) == 1


def test_qsimstat_removal_is_immediate_and_does_not_refresh_inline(monkeypatch):
    from agentcall.modem import Eg25Modem

    modem = Eg25Modem("unused")
    modem._sim_identity = identify("460000123456789\r\nOK", "+CREG: 0,1")
    calls: list[str] = []
    events = []
    monkeypatch.setattr(modem, "_send", lambda command: calls.append(command) or "OK")
    modem.on_sim_identity(events.append)

    modem._buffer = "\r\n+QSIMSTAT: 1,0\r\n"
    modem._process_buffer()

    assert modem.sim_identity == UNKNOWN_SIM
    assert events == [UNKNOWN_SIM]
    assert calls == []


def test_qsimstat_insertion_debounces_refresh_on_background_worker(monkeypatch):
    from agentcall.modem import Eg25Modem

    modem = Eg25Modem("unused")
    modem._SIM_REFRESH_DEBOUNCE_SECONDS = 0.01
    calls: list[tuple[str, str]] = []

    def send(command: str) -> str:
        calls.append((command, threading.current_thread().name))
        if command == "AT+CIMI":
            return "460000123456789\r\nOK"
        if command == "AT+CREG?":
            return "+CREG: 0,1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", send)
    modem._buffer = "\r\n+QSIMSTAT: 1,1\r\n+QSIMSTAT: 1,2\r\n"
    modem._process_buffer()
    worker = modem._sim_refresh_thread
    assert worker is not None
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert [command for command, _thread in calls].count("AT+CIMI") == 1
    assert all(thread != threading.current_thread().name for _, thread in calls)
    assert modem.sim_identity.carrier == "中国移动"


def test_qsimstat_remove_then_insert_during_debounce_keeps_latest_refresh(monkeypatch):
    from agentcall.modem import Eg25Modem

    modem = Eg25Modem("unused")
    modem._SIM_REFRESH_DEBOUNCE_SECONDS = 0.03

    def send(command: str) -> str:
        if command == "AT+CIMI":
            return "460000123456789\r\nOK"
        if command == "AT+CREG?":
            return "+CREG: 0,1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", send)
    modem._buffer = "\r\n+QSIMSTAT: 1,1\r\n"
    modem._process_buffer()
    modem._buffer = "\r\n+QSIMSTAT: 1,0\r\n+QSIMSTAT: 1,1\r\n"
    modem._process_buffer()
    worker = modem._sim_refresh_thread
    assert worker is not None
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert modem.sim_identity.carrier == "中国移动"


def test_open_serial_enables_sim_and_registration_urcs_before_identity_refresh(monkeypatch):
    from agentcall import modem as modem_mod
    from agentcall.modem import Eg25Modem

    modem = Eg25Modem("unused")
    commands: list[str] = []
    monkeypatch.setattr(modem_mod.serial, "Serial", lambda **_kwargs: object())
    monkeypatch.setattr(modem_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(modem, "_drain", lambda: None)

    def send(command: str) -> str:
        commands.append(command)
        if command == "AT+CIMI":
            return "460000123456789\r\nOK"
        if command == "AT+CREG?":
            return "+CREG: 0,1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", send)
    monkeypatch.setattr(modem, "_init_sms", lambda: None)

    modem._open_serial()

    assert commands.index("AT+QSIMSTAT=1") < commands.index("AT+CIMI")
    assert commands.index("AT+CREG=1") < commands.index("AT+CIMI")
