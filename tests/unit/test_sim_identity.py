"""sim_identity 纯函数单测(#88):IMSI/CREG 解析与运营商映射。"""

from __future__ import annotations

import threading

from agentcall.sim_identity import (
    UNKNOWN_SIM,
    identify,
    parse_cpin,
    parse_creg,
    parse_imsi,
    parse_pin_attempts,
    with_lock_state,
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


def test_open_serial_skips_quectel_private_urc_on_other_vendors(monkeypatch):
    """QSIMSTAT 是 Quectel 私有：SIMCom 上必回 ERROR，只会往日志里塞假故障。

    CREG=1 是 3GPP 标准命令，两家都吃，必须照发——否则 SIM/注册变化没人通知。
    """
    from agentcall import modem as modem_mod
    from agentcall.modem import Eg25Modem

    monkeypatch.setenv("MODEM_USB_VID", "1e0e")
    modem = Eg25Modem("unused")
    commands: list[str] = []
    monkeypatch.setattr(modem_mod.serial, "Serial", lambda **_kwargs: object())
    monkeypatch.setattr(modem_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(modem, "_drain", lambda: None)

    def send(command: str) -> str:
        commands.append(command)
        if command == "AT+CIMI":
            return "460010123456789\r\nOK"
        if command == "AT+CREG?":
            return "+CREG: 0,1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", send)
    monkeypatch.setattr(modem, "_init_sms", lambda: None)

    modem._open_serial()

    assert "AT+QSIMSTAT=1" not in commands
    assert "AT+CREG=1" in commands


# ---- parse_cpin / parse_pin_attempts / with_lock_state ----

def test_parse_cpin_reads_lock_codes():
    assert parse_cpin("+CPIN: READY\r\n\r\nOK") == "READY"
    assert parse_cpin("+CPIN: SIM PIN\r\n\r\nOK") == "SIM PIN"
    assert parse_cpin("+CPIN: SIM PUK\r\n\r\nOK") == "SIM PUK"
    # 小写/多空格来自不同厂商固件,归一化为大写单空格
    assert parse_cpin("+cpin:  sim   pin\r\nOK") == "SIM PIN"


def test_parse_cpin_unknown_when_absent():
    # 未插卡时模组只回 ERROR,没有 +CPIN 行——必须是"未知"而不是"锁着"
    assert parse_cpin("+CME ERROR: 10") == ""
    assert parse_cpin("ERROR") == ""
    assert parse_cpin("") == ""


def test_parse_pin_attempts_reads_first_field():
    assert parse_pin_attempts("+SPIC: 3,10,1,10\r\nOK") == 3
    assert parse_pin_attempts("+SPIC: 0,10,0,10\r\nOK") == 0


def test_parse_pin_attempts_unknown_when_unsupported():
    # Quectel 不支持 +SPIC,返回 -1(未知)而不是 0(会被误判成"已锁死")
    assert parse_pin_attempts("ERROR") == -1
    assert parse_pin_attempts("") == -1


def test_with_lock_state_marks_locked_sim():
    sim = with_lock_state(UNKNOWN_SIM, "+CPIN: SIM PIN\r\nOK", "+SPIC: 3,10,1,10\r\nOK")
    assert sim.locked is True
    assert sim.lock_state == "SIM PIN"
    assert sim.lock_status == "等待 PIN"
    assert sim.pin_attempts == 3


def test_with_lock_state_ready_is_not_locked():
    sim = with_lock_state(UNKNOWN_SIM, "+CPIN: READY\r\nOK", "ERROR")
    assert sim.locked is False
    assert sim.lock_status == "已解锁"
    assert sim.pin_attempts == -1


def test_unknown_lock_state_is_not_reported_as_locked():
    # 读不到 CPIN 不等于锁着——否则未插卡会被误报成"需要 PIN"
    assert with_lock_state(UNKNOWN_SIM, "ERROR").locked is False
    assert UNKNOWN_SIM.locked is False


def test_as_dict_exposes_locked_for_frontend():
    sim = with_lock_state(UNKNOWN_SIM, "+CPIN: SIM PIN\r\nOK", "+SPIC: 2,10,1,10\r\nOK")
    data = sim.as_dict()
    assert data["locked"] is True
    assert data["pin_attempts"] == 2
    # 完整 IMSI 绝不出网
    assert "imsi" not in data


def test_identify_defaults_keep_lock_fields_unknown():
    # 既有调用方(#88)不传锁状态时字段应为"未知",不影响原有语义
    sim = identify("460110123456789\r\nOK", "+CREG: 0,1\r\nOK")
    assert sim.lock_state == ""
    assert sim.locked is False
    assert sim.pin_attempts == -1


# ---- modem 层接线:锁卡识别与 unlock_sim ----

import pytest  # noqa: E402


def test_modem_refresh_flags_locked_sim(monkeypatch, caplog):
    """锁卡时 CIMI 必 ERROR:不能报成"未插卡",要报成"等待 PIN"。"""
    modem = _make_modem(monkeypatch, {
        "AT+CPIN?": "+CPIN: SIM PIN\r\nOK",
        "AT+SPIC": "+SPIC: 3,10,1,10\r\nOK",
        "AT+CIMI": "ERROR",
        "AT+CREG?": "+CREG: 0,2\r\nOK",
    })
    with caplog.at_level("WARNING"):
        modem.refresh_sim_identity()
    sim = modem.sim_identity
    assert sim.locked is True and sim.pin_attempts == 3
    assert any("已锁定" in r.getMessage() for r in caplog.records)


def test_modem_refresh_skips_cimi_retries_when_locked(monkeypatch):
    """锁卡时重试 CIMI 三轮纯属白等,应直接跳过。"""
    calls: list[str] = []
    modem = _make_modem(monkeypatch, {})
    monkeypatch.setattr(modem, "_send", lambda cmd: (calls.append(cmd), {
        "AT+CPIN?": "+CPIN: SIM PIN\r\nOK",
        "AT+SPIC": "+SPIC: 3,10,1,10\r\nOK",
    }.get(cmd, "ERROR"))[1])
    modem.refresh_sim_identity()
    assert calls.count("AT+CIMI") == 0


def test_modem_unlock_sends_pin_and_refreshes(monkeypatch):
    state = {"locked": True}

    def fake_send(cmd: str) -> str:
        if cmd == "AT+CPIN?":
            return "+CPIN: SIM PIN\r\nOK" if state["locked"] else "+CPIN: READY\r\nOK"
        if cmd == "AT+SPIC":
            return "+SPIC: 3,10,1,10\r\nOK"
        if cmd == 'AT+CPIN="1234"':
            state["locked"] = False
            return "OK"
        if cmd == "AT+CIMI":
            return "ERROR" if state["locked"] else "460000123456789\r\nOK"
        if cmd == "AT+CREG?":
            return "+CREG: 0,1\r\nOK"
        return "OK"

    modem = _make_modem(monkeypatch, {})
    monkeypatch.setattr(modem, "_send", fake_send)
    sim = modem.unlock_sim("1234")
    assert sim.locked is False
    assert sim.carrier == "中国移动" and sim.service_number == "10086"


def test_modem_unlock_rejects_malformed_pin_without_sending(monkeypatch):
    """格式不对的 PIN 绝不下发——不能白白消耗模组的尝试次数。"""
    calls: list[str] = []
    modem = _make_modem(monkeypatch, {})
    monkeypatch.setattr(modem, "_send", lambda cmd: (calls.append(cmd), "OK")[1])
    for bad in ("", "12", "123456789", "12ab", "  "):
        with pytest.raises(ValueError):
            modem.unlock_sim(bad)
    assert not any(c.startswith('AT+CPIN="') for c in calls)


def test_modem_unlock_refuses_when_attempts_nearly_exhausted(monkeypatch):
    """剩 1 次时拒绝下发:再错一次就是 PUK 锁死,代价远大于让用户拿手机解卡。"""
    calls: list[str] = []

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPIN?":
            return "+CPIN: SIM PIN\r\nOK"
        if cmd == "AT+SPIC":
            return "+SPIC: 1,10,1,10\r\nOK"
        return "OK"

    modem = _make_modem(monkeypatch, {})
    monkeypatch.setattr(modem, "_send", fake_send)
    with pytest.raises(ValueError, match="仅剩 1 次"):
        modem.unlock_sim("1234")
    assert not any(c.startswith('AT+CPIN="') for c in calls)


def test_modem_unlock_refuses_puk_locked_card(monkeypatch):
    modem = _make_modem(monkeypatch, {
        "AT+CPIN?": "+CPIN: SIM PUK\r\nOK",
        "AT+SPIC": "+SPIC: 0,10,1,10\r\nOK",
    })
    with pytest.raises(ValueError, match="PUK"):
        modem.unlock_sim("1234")


def test_modem_unlock_wrong_pin_raises_without_leaking_pin(monkeypatch):
    def fake_send(cmd: str) -> str:
        if cmd == "AT+CPIN?":
            return "+CPIN: SIM PIN\r\nOK"
        if cmd == "AT+SPIC":
            return "+SPIC: 3,10,1,10\r\nOK"
        if cmd.startswith('AT+CPIN="'):
            return "+CME ERROR: incorrect password"
        return "ERROR"

    modem = _make_modem(monkeypatch, {})
    monkeypatch.setattr(modem, "_send", fake_send)
    with pytest.raises(ValueError) as excinfo:
        modem.unlock_sim("9999")
    assert "9999" not in str(excinfo.value)   # PIN 明文不进异常消息
