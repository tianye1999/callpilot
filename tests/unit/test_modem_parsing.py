"""Eg25Modem 解析逻辑单测（不开串口，直接测纯解析路径）。"""

from __future__ import annotations

import threading
import time

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


# ---- DTMF 发送 ----


def test_send_dtmf_sends_each_digit(monkeypatch):
    modem = make_modem()
    sent_cmds = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent_cmds.append(cmd) or "OK")
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)

    assert modem.send_dtmf("1a#") is True  # 小写自动转大写
    assert sent_cmds == ['AT+QVTS="1"', 'AT+QVTS="A"', 'AT+QVTS="#"']


def test_send_dtmf_falls_back_to_vts(monkeypatch):
    modem = make_modem()
    sent_cmds = []

    def fake_send(cmd):
        sent_cmds.append(cmd)
        return "OK" if cmd.startswith("AT+VTS") else "ERROR"

    monkeypatch.setattr(modem, "_send", fake_send)
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)

    assert modem.send_dtmf("5") is True
    assert sent_cmds == ['AT+QVTS="5"', 'AT+VTS="5"']


def test_send_dtmf_rejects_invalid(monkeypatch):
    modem = make_modem()
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    assert modem.send_dtmf("12x") is False
    assert modem.send_dtmf("") is False


# ---- hangup 原子性：指令序列不被并发 _send 插队 ----


class FakeSerial:
    """记录写入顺序的假串口：每次 write 后排一条 OK 响应供 _read_response 读取。"""

    def __init__(self) -> None:
        self.is_open = True
        self.writes: list[str] = []
        self._pending = b""
        self._lock = threading.Lock()

    @property
    def in_waiting(self) -> int:
        return len(self._pending)

    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append(data.decode("ascii").strip())
            self._pending = b"\r\nOK\r\n"
        return len(data)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            out, self._pending = self._pending[:size], self._pending[size:]
        return out

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._pending = b""


def test_hangup_commands_not_interleaved_by_concurrent_send():
    """hangup 持锁期间，并发线程的 _send（如 CLCC 轮询）不得插进 ATH 与 AT+QPCMV=0 之间。"""
    modem = make_modem()
    fake = FakeSerial()
    modem._ser = fake

    ath_sent = threading.Event()
    orig_send = modem._send

    def send_with_race_window(cmd: str) -> str:
        response = orig_send(cmd)
        if cmd == "ATH":
            # 撑大 ATH 与 AT+QPCMV=0 之间的窗口：若 hangup 未整体持有
            # _serial_lock，竞争线程会在此窗口内拿到锁插队。
            ath_sent.set()
            time.sleep(0.1)
        return response

    modem._send = send_with_race_window

    def contender() -> None:
        ath_sent.wait(timeout=2)
        orig_send("AT+CLCC")  # 模拟 CLCC 轮询线程的并发指令

    thread = threading.Thread(target=contender)
    thread.start()
    modem.hangup()
    thread.join(timeout=2)
    assert not thread.is_alive()

    writes = fake.writes
    assert "AT+CLCC" in writes  # 竞争线程的指令最终发出，未被饿死
    ath_idx = writes.index("ATH")
    assert writes[ath_idx + 1] == "AT+QPCMV=0"  # 两条挂断指令相邻
    assert modem.pcm_ready()
    assert not modem.is_call_connected()
