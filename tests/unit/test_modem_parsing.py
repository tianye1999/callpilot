"""Eg25Modem 解析逻辑单测（不开串口，直接测纯解析路径）。"""

from __future__ import annotations

from agentcall.modem import Eg25Modem, parse_sms_pdu


def make_modem() -> Eg25Modem:
    """构造未连接串口的 modem 实例，仅用于测解析。"""
    return Eg25Modem(port="/dev/null-not-used")


# ---- SMS PDU 解码 ----

# SMS-DELIVER：发件 +8613800000000，UCS2 正文"你好"，时间 26/07/07,12:30:00
UCS2_PDU = "00040D91683108000000F0000862707021030023044F60597D"


def test_parse_sms_pdu_ucs2():
    parsed = parse_sms_pdu(UCS2_PDU)
    assert parsed is not None
    sender, timestamp, body = parsed
    assert sender == "+8613800000000"
    assert timestamp == "26/07/07,12:30:00"
    assert body == "你好"


def test_parse_sms_pdu_garbage_returns_none():
    assert parse_sms_pdu("ZZZZ") is None
    assert parse_sms_pdu("") is None


# ---- 文本模式短信解析 ----

def test_parse_sms_header_text_mode():
    line = '+CMGR: "REC UNREAD","+8613800000000",,"26/07/01,14:20:07+32"'
    sender, timestamp = Eg25Modem._parse_sms_header(line)
    assert sender == "+8613800000000"
    assert timestamp == "26/07/01,14:20:07+32"


def test_decode_sms_body_ucs2_hex():
    assert Eg25Modem._decode_sms_body("4F60597D") == "你好"


def test_decode_sms_body_plain_text_passthrough():
    assert Eg25Modem._decode_sms_body("hello 123") == "hello 123"


# ---- CLCC 来电检测与去重 ----

CLCC_INCOMING = '\r\n+CLCC: 1,1,4,0,0,"13900000000",129\r\n\r\nOK\r\n'
CLCC_EMPTY = "\r\nOK\r\n"


def test_clcc_incoming_triggers_ring_once():
    modem = make_modem()
    rings: list[str | None] = []
    modem.on_ring(rings.append)

    modem._process_clcc_response(CLCC_INCOMING)
    modem._process_clcc_response(CLCC_INCOMING)  # 同一通来电重复上报

    assert rings == ["13900000000"]


def test_clcc_ring_again_after_call_cleared():
    modem = make_modem()
    rings: list[str | None] = []
    modem.on_ring(rings.append)

    modem._process_clcc_response(CLCC_INCOMING)
    modem._process_clcc_response(CLCC_EMPTY)  # 通话消失，去重集合清空
    modem._process_clcc_response(CLCC_INCOMING)  # 新来电

    assert len(rings) == 2


def test_clcc_outbound_connected_sets_event():
    modem = make_modem()
    connected: list[str | None] = []
    modem.on_call_connected(connected.append)
    modem._last_dialed = "13700000000"

    modem._process_clcc_response('\r\n+CLCC: 1,0,0,0,0,"13700000000",129\r\nOK\r\n')

    assert modem.is_call_connected()
    assert connected == ["13700000000"]


# ---- URC 缓冲处理：RING / CLIP / NO CARRIER ----

def test_ring_urc_with_clip_carries_caller():
    modem = make_modem()
    rings: list[str | None] = []
    modem.on_ring(rings.append)

    modem._buffer = '\r\n+CLIP: "13600000000",129\r\n\r\nRING\r\n'
    modem._process_buffer()

    assert rings == ["13600000000"]


# ---- 断连自愈：_send 写失败后重连并重试 ----

def test_send_reconnects_and_retries_on_io_error():
    """模拟 USB 桥重连导致的写失败：_send 应触发一次重连后重试成功。"""
    import serial

    modem = make_modem()
    calls = {"write": 0, "reconnect": 0}

    def fake_write_command(cmd: str) -> str:
        calls["write"] += 1
        if calls["write"] == 1:
            raise serial.SerialException("write failed: [Errno 5] Input/output error")
        return "OK"

    def fake_reconnect() -> None:
        calls["reconnect"] += 1

    modem._write_command = fake_write_command
    modem._reconnect = fake_reconnect

    assert modem._send("ATD10086;") == "OK"
    assert calls["reconnect"] == 1  # 触发了一次重连
    assert calls["write"] == 2      # 首次失败 + 重连后重试


def test_send_during_init_does_not_self_reconnect():
    """初始化序列中（_opening=True）写失败应直接抛出，不自触发重连（防死锁）。"""
    import serial

    modem = make_modem()
    modem._opening = True
    reconnected = {"n": 0}
    modem._reconnect = lambda: reconnected.__setitem__("n", reconnected["n"] + 1)

    def always_fail(cmd: str) -> str:
        raise serial.SerialException("boom")

    modem._write_command = always_fail

    try:
        modem._send("AT")
        assert False, "应抛出异常"
    except serial.SerialException:
        pass
    assert reconnected["n"] == 0  # 初始化期间不重连


def test_no_carrier_triggers_hangup():
    modem = make_modem()
    hangups: list[bool] = []
    modem.on_hangup(lambda: hangups.append(True))

    modem._buffer = "\r\nNO CARRIER\r\n"
    modem._process_buffer()

    assert hangups == [True]
    assert modem._buffer == ""  # 挂断后缓冲清空
