"""Eg25Modem 解析逻辑单测（不开串口，直接测纯解析路径）。"""

from __future__ import annotations

import threading
import time

import serial

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


def test_clcc_response_with_cmti_reads_sms():
    modem = make_modem()
    messages: list[tuple[str | None, str]] = []
    modem.on_sms(lambda sender, body: messages.append((sender, body)))
    sent: list[str] = []

    def fake_send(cmd: str) -> str:
        sent.append(cmd)
        if cmd == 'AT+CPMS="SM"':
            return "\r\nOK\r\n"
        if cmd == "AT+CMGR=5":
            return (
                '\r\n+CMGR: "REC UNREAD","+8613800000000",,"26/07/01,14:20:07+32"\r\n'
                "hello from cmti\r\n"
                "OK\r\n"
            )
        return "\r\nOK\r\n"

    modem._send = fake_send  # type: ignore[method-assign]

    modem._process_clcc_response(
        '\r\n+CLCC: 1,1,4,0,0,"13900000000",129\r\n'
        '+CMTI: "SM",5\r\n'
        "OK\r\n"
    )

    assert sent == ['AT+CPMS="SM"', "AT+CMGR=5"]
    assert messages == [("+8613800000000", "hello from cmti")]


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

    def close(self) -> None:
        self.is_open = False


class TrackingRLock:
    """RLock wrapper that exposes when a named thread starts waiting for it."""

    def __init__(self, watched_thread: str) -> None:
        self._lock = threading.RLock()
        self._watched_thread = watched_thread
        self.waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if threading.current_thread().name == self._watched_thread:
            self.waiting.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def test_send_and_reader_reconnect_do_not_deadlock_on_opposite_lock_order(monkeypatch):
    """发送线程持串口锁失败时，与读线程并发重连不得形成 ABBA 死锁。"""
    modem = make_modem()
    serial_lock = TrackingRLock("reader-reconnect")
    modem._serial_lock = serial_lock  # type: ignore[assignment]
    modem._ser = FakeSerial()
    modem._running = True
    write_calls = 0
    open_calls = 0
    results: list[str] = []

    def flaky_write(_cmd: str) -> str:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            raise serial.SerialException("forced write failure")
        return "OK"

    def reopen() -> None:
        nonlocal open_calls
        open_calls += 1
        modem._ser = FakeSerial()

    monkeypatch.setattr(modem, "_write_command", flaky_write)
    monkeypatch.setattr(modem, "_open_serial", reopen)

    sender_holds_serial = threading.Event()

    def sender() -> None:
        with modem._serial_lock:
            sender_holds_serial.set()
            assert serial_lock.waiting.wait(timeout=1)
            results.append(modem._send("AT"))

    sender_thread = threading.Thread(target=sender, name="sender", daemon=True)
    sender_thread.start()
    assert sender_holds_serial.wait(timeout=1)

    reader_thread = threading.Thread(
        target=modem._reconnect, name="reader-reconnect", daemon=True
    )
    reader_thread.start()

    sender_thread.join(timeout=1)
    reader_thread.join(timeout=1)
    modem._running = False

    assert not sender_thread.is_alive(), "发送线程与读线程发生 ABBA 死锁"
    assert not reader_thread.is_alive(), "重连线程未能退出"
    assert results == ["OK"]
    assert open_calls == 1


def test_reconnect_state_machine_retries_and_replaces_serial(monkeypatch):
    modem = make_modem()
    old_serial = FakeSerial()
    modem._ser = old_serial
    modem._buffer = "stale URC"
    modem._running = True
    attempts = 0
    delays: list[float] = []

    def flaky_open() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise serial.SerialException("bridge not ready")
        modem._ser = FakeSerial()

    monkeypatch.setattr(modem, "_open_serial", flaky_open)
    monkeypatch.setattr("agentcall.modem.time.sleep", delays.append)

    modem._reconnect()
    modem._running = False

    assert old_serial.is_open is False
    assert modem._ser is not old_serial
    assert modem._ser is not None and modem._ser.is_open
    assert modem._buffer == ""
    assert attempts == 3
    assert delays == [1.0, 2.0]


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


# ---- send_command：原始 AT 原子能力 ----


def test_send_command_returns_raw_response():
    """send_command 透传 _send：发出原始指令并返回模组原始响应。"""
    modem = make_modem()
    modem._ser = FakeSerial()
    resp = modem.send_command("AT+CSQ")
    assert "OK" in resp
    assert modem._ser.writes == ["AT+CSQ"]


class CMTIFakeSerial(FakeSerial):
    """假串口：普通 AT 响应里夹带 CMTI，随后支持真实 _read_sms 指令序列。"""

    def write(self, data: bytes) -> int:
        cmd = data.decode("ascii").strip()
        with self._lock:
            self.writes.append(cmd)
            if cmd == "AT+CSQ":
                self._pending = (
                    b'\r\n+CSQ: 20,99\r\n+CMTI: "SM",5\r\nOK\r\n'
                )
            elif cmd == 'AT+CPMS="SM"':
                self._pending = b"\r\nOK\r\n"
            elif cmd == "AT+CMGR=5":
                self._pending = (
                    b'\r\n+CMGR: "REC UNREAD","+8613800000000",,"26/07/01,14:20:07+32"\r\n'
                    b"hello from send\r\nOK\r\n"
                )
            else:
                self._pending = b"\r\nOK\r\n"
        return len(data)


def test_send_response_with_cmti_reads_sms():
    modem = make_modem()
    modem._ser = CMTIFakeSerial()
    messages: list[tuple[str | None, str]] = []
    modem.on_sms(lambda sender, body: messages.append((sender, body)))

    response = modem.send_command("AT+CSQ")

    assert "+CSQ:" in response
    assert modem._ser.writes == ["AT+CSQ", 'AT+CPMS="SM"', "AT+CMGR=5"]
    assert messages == [("+8613800000000", "hello from send")]


def test_response_without_cmti_has_no_sms_side_effect():
    modem = make_modem()
    modem._ser = FakeSerial()
    messages: list[tuple[str | None, str]] = []
    modem.on_sms(lambda sender, body: messages.append((sender, body)))

    response = modem.send_command("AT")

    assert "OK" in response
    assert modem._ser.writes == ["AT"]
    assert messages == []


# ---- P0 会话僵尸：串口断连期通话消失，CLCC 恢复后必须触发 on_hangup ----
# 真机事故（2026-07-08）：通话中 USB 断死→NO CARRIER 收不到→重连后 CLCC
# 每 2s 返回空却无人处理，会话僵尸直到手动挂断。


CLCC_ACTIVE_OUTBOUND = '+CLCC: 1,0,0,0,0,"10000",129\r\nOK\r\n'
CLCC_EMPTY_OK = "OK\r\n"


def _connected_modem() -> tuple[Eg25Modem, list[str]]:
    """返回「外呼已接通」状态的 modem 与挂断回调记录。"""
    modem = make_modem()
    hangups: list[str] = []
    modem.on_hangup(lambda: hangups.append("hangup"))
    modem._process_clcc_response(CLCC_ACTIVE_OUTBOUND)
    assert modem.is_call_connected()
    return modem, hangups


def test_clcc_absent_twice_fires_hangup():
    """有效 CLCC 连续两次无通话行 → 判定通话丢失，触发一次 on_hangup。"""
    modem, hangups = _connected_modem()
    modem._process_clcc_response(CLCC_EMPTY_OK)
    assert hangups == []  # 第一次不判死（滤瞬变）
    modem._process_clcc_response(CLCC_EMPTY_OK)
    assert hangups == ["hangup"]
    assert not modem.is_call_connected()
    # 已收尾后继续空响应不再重复触发
    modem._process_clcc_response(CLCC_EMPTY_OK)
    assert hangups == ["hangup"]


def test_clcc_absent_reset_when_call_reappears():
    """一次空响应后通话行重新出现 → 计数复位，不误挂。"""
    modem, hangups = _connected_modem()
    modem._process_clcc_response(CLCC_EMPTY_OK)
    modem._process_clcc_response(CLCC_ACTIVE_OUTBOUND)
    modem._process_clcc_response(CLCC_EMPTY_OK)
    assert hangups == []
    assert modem.is_call_connected()


def test_clcc_invalid_response_not_counted():
    """无 OK 的响应（超时/垃圾）不参与消失判定。"""
    modem, hangups = _connected_modem()
    modem._process_clcc_response("")
    modem._process_clcc_response("\r\n+QIND: something\r\n")
    modem._process_clcc_response(CLCC_EMPTY_OK)
    assert hangups == []  # 只累计到 1 次有效空响应


def test_clcc_absent_without_active_call_is_noop():
    """无通话在线时空 CLCC 属正常待机，绝不触发挂断。"""
    modem = make_modem()
    hangups: list[str] = []
    modem.on_hangup(lambda: hangups.append("hangup"))
    for _ in range(5):
        modem._process_clcc_response(CLCC_EMPTY_OK)
    assert hangups == []


def test_answer_marks_call_connected_for_loss_detection():
    """来电 ATA 后同样进入消失判定保护。"""
    modem = make_modem()

    sent: list[str] = []
    modem._send = lambda cmd, **kw: sent.append(cmd) or "OK"  # type: ignore[method-assign]
    hangups: list[str] = []
    modem.on_hangup(lambda: hangups.append("hangup"))

    modem.answer()
    assert modem.is_call_connected()
    modem._process_clcc_response(CLCC_EMPTY_OK)
    modem._process_clcc_response(CLCC_EMPTY_OK)
    assert hangups == ["hangup"]


def test_poll_failure_threshold_fires_hangup(monkeypatch):
    """通话在线期串口持续失联达阈值 → 放弃等待，收尾会话（跑真实轮询循环）。"""
    modem, hangups = _connected_modem()

    def boom(cmd, **kw):
        raise OSError("串口已死")

    modem._send = boom  # type: ignore[method-assign]
    monkeypatch.setattr(type(modem), "_CLCC_FAIL_THRESHOLD", 3)

    ticks = {"n": 0}

    def fake_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] >= 6:  # 越过阈值后再跑几轮，验证不重复触发
            modem._running = False

    monkeypatch.setattr("agentcall.modem.time.sleep", fake_sleep)
    modem._running = True
    modem._poll_call_status()  # 同步跑完（fake_sleep 负责终止）

    assert hangups == ["hangup"]
    assert not modem.is_call_connected()
