"""Eg25Modem 解析逻辑单测（不开串口，直接测纯解析路径）。"""

from __future__ import annotations

import logging
import threading
import time

import pytest
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
    modem.on_sms(lambda sender, body, ts="": messages.append((sender, body)))
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

    assert sent == ['AT+CPMS="SM"', "AT+CMGR=5", "AT+CMGD=5"]  # 读完删 SIM 副本
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


def test_send_dtmf_sends_each_digit_without_logging_plaintext(monkeypatch, caplog):
    modem = make_modem()
    sent_cmds = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent_cmds.append(cmd) or "OK")
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)

    with caplog.at_level(logging.INFO):
        assert modem.send_dtmf("1a#") is True  # 小写自动转大写

    assert sent_cmds == ['AT+QVTS="1"', 'AT+QVTS="A"', 'AT+QVTS="#"']
    assert "1A#" not in caplog.text
    assert "count=3" in caplog.text
    assert "result=success" in caplog.text


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


def test_send_dtmf_rejects_invalid_without_logging_plaintext(monkeypatch, caplog):
    modem = make_modem()
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    with caplog.at_level(logging.WARNING):
        assert modem.send_dtmf("12x") is False

    assert modem.send_dtmf("") is False
    assert "12X" not in caplog.text
    assert "count=3" in caplog.text
    assert "result=failure" in caplog.text


def test_send_dtmf_modem_failure_does_not_log_failed_digit(monkeypatch, caplog):
    modem = make_modem()
    monkeypatch.setattr(modem, "_send", lambda cmd: "ERROR")

    with caplog.at_level(logging.WARNING):
        assert modem.send_dtmf("#") is False

    assert "#" not in caplog.text
    assert "count=1" in caplog.text
    assert "result=failure" in caplog.text


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


def test_call_status_poll_does_not_hold_serial_lock_while_sending(monkeypatch):
    """A reader-owned reconnect must not deadlock against the CLCC poller."""
    modem = make_modem()
    modem._running = True
    observed: list[bool] = []

    def send_once(_command: str) -> str:
        observed.append(modem._serial_lock._is_owned())  # type: ignore[attr-defined]
        modem._running = False
        return "OK"

    monkeypatch.setattr(modem, "_send", send_once)
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda _seconds: None)

    modem._poll_call_status()

    assert observed == [False]


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


def test_reconnect_emits_transition_only_callbacks_outside_modem_locks(monkeypatch):
    modem = make_modem()
    modem._ser = FakeSerial()
    modem._running = True
    modem._connection_online = True
    attempts = 0
    transitions: list[bool] = []
    online_seen = threading.Event()

    def on_state(online: bool) -> None:
        assert not modem._serial_lock._is_owned()  # type: ignore[attr-defined]
        assert not modem._reconnect_lock.locked()
        transitions.append(online)
        if online:
            online_seen.set()

    def flaky_open() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise serial.SerialException("bridge not ready")
        modem._ser = FakeSerial()

    modem.on_connection_state(on_state)
    monkeypatch.setattr(modem, "_open_serial", flaky_open)
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda _seconds: None)

    modem._reconnect()
    modem._running = False

    assert online_seen.wait(timeout=1)
    assert transitions == [False, True]
    assert attempts == 3


def test_stale_concurrent_reconnect_does_not_flap_connection_state(monkeypatch):
    modem = make_modem()
    modem._ser = FakeSerial()
    modem._running = True
    modem._connection_online = True
    transitions: list[bool] = []
    online_seen = threading.Event()
    entered_open = threading.Event()
    release_open = threading.Event()

    def reopen() -> None:
        entered_open.set()
        assert release_open.wait(timeout=2)
        modem._ser = FakeSerial()

    def on_state(online: bool) -> None:
        transitions.append(online)
        if online:
            online_seen.set()

    modem.on_connection_state(on_state)
    monkeypatch.setattr(modem, "_open_serial", reopen)

    first = threading.Thread(target=modem._reconnect, daemon=True)
    second = threading.Thread(target=modem._reconnect, daemon=True)
    first.start()
    assert entered_open.wait(timeout=1)
    second.start()
    release_open.set()
    first.join(timeout=2)
    second.join(timeout=2)
    modem._running = False

    assert not first.is_alive() and not second.is_alive()
    assert online_seen.wait(timeout=1)
    assert transitions == [False, True]


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
    modem.on_sms(lambda sender, body, ts="": messages.append((sender, body)))

    response = modem.send_command("AT+CSQ")

    assert "+CSQ:" in response
    assert modem._ser.writes == ["AT+CSQ", 'AT+CPMS="SM"', "AT+CMGR=5", "AT+CMGD=5"]  # 读完删 SIM 副本
    assert messages == [("+8613800000000", "hello from send")]


def test_response_without_cmti_has_no_sms_side_effect():
    modem = make_modem()
    modem._ser = FakeSerial()
    messages: list[tuple[str | None, str]] = []
    modem.on_sms(lambda sender, body, ts="": messages.append((sender, body)))

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


_CMGL_TWO = (
    '+CMGL: 1,"REC READ","10086",,"26/07/08,20:03:24+32"\r\n'
    "余额100元\r\n"
    '+CMGL: 3,"REC READ","10001",,"26/07/09,14:01:34+32"\r\n'
    "剩余1.00GB\r\n"
    "OK\r\n"
)


def test_dump_stored_sms_backfills_and_deletes(monkeypatch):
    """补收 SIM 已存短信入库后，逐条 AT+CMGD 删除腾存储（默认开）。"""
    monkeypatch.setenv("SMS_DELETE_AFTER_INGEST", "true")
    modem = make_modem()
    sent: list[str] = []
    received: list[tuple[str | None, str, str]] = []
    modem.on_sms(lambda s, b, ts="": received.append((s, b, ts)))

    def fake_send(cmd: str) -> str:
        sent.append(cmd)
        return _CMGL_TWO if cmd == 'AT+CMGL="ALL"' else "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem._dump_stored_sms()

    # 两条都补收（走 on_sms，带时间戳）
    assert [(s, b) for s, b, _ in received] == [("10086", "余额100元"), ("10001", "剩余1.00GB")]
    # 补收后按 index 删除两条
    assert "AT+CMGD=1" in sent and "AT+CMGD=3" in sent


def test_dump_stored_sms_keeps_sim_when_delete_disabled(monkeypatch):
    """SMS_DELETE_AFTER_INGEST=false 时补收但不删 SIM。"""
    monkeypatch.setenv("SMS_DELETE_AFTER_INGEST", "false")
    modem = make_modem()
    sent: list[str] = []
    modem.on_sms(lambda s, b, ts="": None)

    def fake_send(cmd: str) -> str:
        sent.append(cmd)
        return _CMGL_TWO if cmd == 'AT+CMGL="ALL"' else "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem._dump_stored_sms()

    assert not any(c.startswith("AT+CMGD") for c in sent)


# ---- 语音 PCM 通道的 AT 方言：Quectel(QPCMV) vs SIMCom(CPCMREG) ----


def _recording_modem(monkeypatch, responses=None):
    """记录所有下发指令的 modem；responses 可指定个别指令的返回。"""
    responses = responses or {}
    calls: list[str] = []
    modem = make_modem()
    # hangup 后 simcom 沉降不应拖慢单测。
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd in responses:
            return responses[cmd]
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    return modem, calls


def test_initialize_for_voice_simcom_sends_cpcmreg(monkeypatch):
    modem, calls = _recording_modem(monkeypatch)
    modem.initialize_for_voice("simcom_pcm")
    assert "AT+CPCMREG=1" in calls
    assert "AT+CPCMREG?" in calls
    # 不能误发 Quectel 指令：SIM7600 不认，且会把 AT 队列搅乱
    assert not any(c.startswith("AT+QPCMV") for c in calls)


def test_reset_module_uses_non_retrying_creset_and_clears_voice_state(monkeypatch):
    modem = make_modem()
    modem._voice_pcm_active = True
    modem._call_connected_event.set()
    sent: list[str] = []

    def fake_write(command: str) -> str:
        sent.append(command)
        return "\r\nOK\r\n"

    monkeypatch.setattr(modem, "_write_command", fake_write)

    assert modem.reset_module() is True
    assert sent == ["AT+CRESET"]
    assert modem.voice_pcm_active is False
    assert modem.is_call_connected() is False


def test_initialize_for_voice_simcom_tolerates_error_without_call(monkeypatch, caplog):
    """无通话时 AT+CPCMREG=1 必回 ERROR（启动期 supervisor 就是这种情形）。

    这不是故障：真正生效的是每通电话接通后的那次调用，因此不得抛异常，
    否则模组 supervisor 会把它当连接失败而无限重试。
    """
    modem, _ = _recording_modem(monkeypatch, {"AT+CPCMREG=1": "+CME ERROR: 3"})
    with caplog.at_level("INFO"):
        modem.initialize_for_voice("simcom_pcm")  # 不抛
    assert any("接通后再启用" in r.getMessage() for r in caplog.records)


def test_simcom_dial_enables_usb_audio_immediately_after_atd(monkeypatch):
    """官方示例时序：8k 在 ATD 前，CPCMREG 紧跟 ATD，不等待物理接通。"""
    modem, calls = _recording_modem(monkeypatch)
    modem._audio_mode = "simcom_pcm"

    response = modem.dial("10000")

    assert response == "OK"
    assert calls[:7] == [
        "AT+CLCC",
        "AT+CHUP",
        "AT+CPCMREG=0,1",
        "AT+CLCC",
        "AT+CPCMBANDWIDTH=1,1",
        "AT+CPCMBANDWIDTH?",
        "ATD10000;",
    ]
    assert calls[7:9] == ["AT+CPCMREG=1", "AT+CPCMREG?"]
    assert modem.voice_pcm_active is True
    assert not modem.is_call_connected()


def test_simcom_dial_early_enable_failure_falls_back_after_connect(monkeypatch):
    """预启用只是优化：短窗口失败仍返回 ATD 结果，接通后走原 12s 兜底。"""
    clock = {"now": 0.0}
    monkeypatch.setattr(modem_time_sleep_target(), "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        modem_time_sleep_target(),
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )
    modem = make_modem()
    modem._audio_mode = "simcom_pcm"
    calls: list[str] = []

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd in {"AT+CLCC", "AT+CHUP", "ATD10000;"}:
            return "OK"
        return "ERROR"

    monkeypatch.setattr(modem, "_send", fake_send)

    assert modem.dial("10000") == "OK"
    assert calls[0:7] == [
        "AT+CLCC",
        "AT+CHUP",
        "AT+CPCMREG=0,1",
        "AT+CLCC",
        "AT+CPCMBANDWIDTH=1,1",
        "AT+CPCMBANDWIDTH?",
        "ATD10000;",
    ]
    assert calls.count("AT+CPCMREG=1") > 1
    assert clock["now"] >= modem._SIMCOM_PCM_EARLY_ENABLE_TIMEOUT
    assert modem.voice_pcm_active is False


def test_quectel_dial_does_not_send_simcom_early_audio_commands(monkeypatch):
    modem, calls = _recording_modem(monkeypatch)
    modem._audio_mode = "uac"

    assert modem.dial("10000") == "OK"
    assert calls == ["ATD10000;"]


def test_simcom_dial_clears_stale_calls_before_atd(monkeypatch):
    modem = make_modem()
    modem._audio_mode = "simcom_pcm"
    calls: list[str] = []
    clcc_responses = iter(
        [
            '+CLCC: 5,0,0,0,0,"redacted",129\r\nOK',
            "OK",
        ]
    )

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CLCC":
            return next(clcc_responses)
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda _delay: None)

    assert modem.dial("10000") == "OK"
    assert calls.index("AT+CHUP") < calls.index("ATD10000;")
    assert calls.index("AT+CPCMREG=0,1") < calls.index("ATD10000;")


def test_simcom_dial_refuses_to_stack_call_that_cannot_be_cleared(monkeypatch):
    clock = {"now": 0.0}
    modem = make_modem()
    modem._audio_mode = "simcom_pcm"
    calls: list[str] = []

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CLCC":
            return '+CLCC: 5,0,0,0,0,"redacted",129\r\nOK'
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    monkeypatch.setattr(modem_time_sleep_target(), "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        modem_time_sleep_target(),
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )

    with pytest.raises(RuntimeError, match="拒绝叠加"):
        modem.dial("10000")
    assert "ATD10000;" not in calls


def test_hangup_closes_channel_with_matching_dialect(monkeypatch):
    modem, calls = _recording_modem(monkeypatch)
    modem.initialize_for_voice("simcom_pcm")
    calls.clear()
    modem.hangup()
    assert "AT+CHUP" in calls and "AT+CPCMREG=0,1" in calls
    assert "ATH" not in calls
    assert "AT+QPCMV=0" not in calls


def test_simcom_hangup_falls_back_to_ath_when_chup_is_rejected(monkeypatch):
    modem, calls = _recording_modem(monkeypatch, {"AT+CHUP": "ERROR"})
    modem.initialize_for_voice("simcom_pcm")
    calls.clear()

    modem.hangup()

    assert calls[:3] == ["AT+CHUP", "ATH", "AT+CPCMREG=0,1"]


def test_hangup_invalidates_clcc_response_started_by_older_call(monkeypatch):
    modem, _ = _recording_modem(monkeypatch)
    modem._audio_mode = "simcom_pcm"
    old_generation = modem._call_state_generation

    modem.hangup()
    modem._process_clcc_response(
        CLCC_ACTIVE_OUTBOUND,
        expected_generation=old_generation,
    )

    assert not modem.is_call_connected()
    assert modem._connected_call_ids == set()


def test_hangup_after_uac_still_uses_quectel_dialect(monkeypatch):
    """回归锁：EC20 路径不能被 SIMCom 改动带偏。"""
    modem, calls = _recording_modem(monkeypatch)
    modem.initialize_for_voice("uac")
    calls.clear()
    modem.hangup()
    assert "AT+QPCMV=0" in calls
    assert "AT+CPCMREG=0,1" not in calls


def test_hangup_without_init_defaults_to_quectel(monkeypatch):
    """从未 initialize 就挂断（异常路径）时沿用历史默认，不静默跳过关闭。"""
    modem, calls = _recording_modem(monkeypatch)
    modem.hangup()
    assert "AT+QPCMV=0" in calls


def test_initialize_for_voice_rejects_unknown_mode(monkeypatch):
    modem, _ = _recording_modem(monkeypatch)
    with pytest.raises(ValueError, match="simcom_pcm"):
        modem.initialize_for_voice("not_a_mode")


# ---- simcom PCM 启用的通话中/无通话两条路径（真机 2026-08-01 拖死 AT 链路的回归锁）----


def test_simcom_pcm_retries_while_in_call(monkeypatch):
    """接通后允许短暂 pending（=1 已 OK、读回未齐）；=1 曾被拒则拒启桥。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    calls: list[str] = []
    modem = make_modem()
    modem._call_connected_event.set()          # 模拟通话中
    modem._pcm_endpoint_clean = True
    queries = {"n": 0}

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG?":
            queries["n"] += 1
            return f"+CPCMREG: {1 if queries['n'] >= 3 else 0}\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")
    assert modem.voice_pcm_active is True
    assert "AT+CPCMBANDWIDTH=1,1" in calls
    assert "AT+CPCMREG=0,1" not in calls


def test_simcom_pcm_skips_proactive_reset_when_endpoint_already_clean(monkeypatch):
    """挂断已干净关闭端点时，来电启用不得再无条件 =0,1。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    calls: list[str] = []
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = True

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")
    assert "AT+CPCMREG=0,1" not in calls
    assert calls.count("AT+CPCMREG=1") == 1


def test_simcom_pcm_proactive_reset_only_when_endpoint_dirty(monkeypatch):
    """端点未干净时启用前才预复位，并沉降后再 =1。"""
    sleeps: list[float] = []
    monkeypatch.setattr(
        modem_time_sleep_target(), "sleep", lambda s: sleeps.append(s)
    )
    calls: list[str] = []
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = False

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")
    assert "AT+CPCMREG=0,1" in calls
    assert calls.index("AT+CPCMREG=0,1") < calls.index("AT+CPCMREG=1")
    assert any(
        abs(s - modem._SIMCOM_PREDIAL_SETTLE_DELAY) < 1e-9 for s in sleeps
    ), sleeps


def test_simcom_pcm_dirty_reset_settle_does_not_consume_retry_budget(monkeypatch):
    """脏端点上的慢预复位不得吃掉 12s 窗口；预复位后首次 =1 仍可成功。"""
    clock = {"now": 0.0}
    monkeypatch.setattr(modem_time_sleep_target(), "monotonic", lambda: clock["now"])

    def fake_sleep(delay: float) -> None:
        clock["now"] += delay

    monkeypatch.setattr(modem_time_sleep_target(), "sleep", fake_sleep)
    calls: list[str] = []
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = False

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG=0,1":
            clock["now"] += 11.0
            return "OK"
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")
    assert modem.voice_pcm_active is True
    assert calls.count("AT+CPCMREG=1") == 1
    assert clock["now"] >= 11.0


def test_simcom_pcm_reject_uses_hooked_at_reset_not_host_reclaim_only(monkeypatch):
    """clean 首拒：带钩子 =0,1 后进 mode=1 就启桥——拒接只是把卡顿换成报错。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    calls: list[str] = []
    hooks: list[str] = []
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = True
    modem.set_pcm_endpoint_reset_hooks(
        before=lambda: hooks.append("before"),
        after=lambda: hooks.append("after"),
    )

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG=1":
            return "OK" if calls.count("AT+CPCMREG=1") >= 2 else "ERROR"
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")

    reg = [c for c in calls if c.startswith("AT+CPCMREG")]
    assert reg[:4] == [
        "AT+CPCMREG=1",
        "AT+CPCMREG=0,1",
        "AT+CPCMREG=1",
        "AT+CPCMREG?",
    ]
    assert hooks == ["before", "after"]
    assert modem.voice_pcm_active is True


def test_simcom_pcm_keeps_retrying_after_reset_reject(monkeypatch):
    """预复位后仍被拒也要重试到窗口耗尽：早退等于把可用通道判死。"""
    clock = {"now": 0.0}
    monkeypatch.setattr(modem_time_sleep_target(), "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        modem_time_sleep_target(),
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = False
    calls: list[str] = []

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG=1":
            return "ERROR"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    with pytest.raises(RuntimeError, match="仍未确认 mode=1"):
        modem.initialize_for_voice("simcom_pcm")
    assert modem.voice_pcm_active is False
    assert calls.count("AT+CPCMREG=1") > 1


def test_simcom_pcm_dirty_reset_invokes_host_hooks(monkeypatch):
    """dirty 启用前预复位也必须走宿主钩子（先释放 COM）。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    hooks: list[str] = []
    calls: list[str] = []
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = False
    modem.set_pcm_endpoint_reset_hooks(
        before=lambda: hooks.append("before"),
        after=lambda: hooks.append("after"),
    )

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")

    assert hooks == ["before", "after"]
    assert calls.index("AT+CPCMREG=0,1") < calls.index("AT+CPCMREG=1")


def test_simcom_pcm_never_resets_while_registration_is_pending(monkeypatch):
    """=1 已 OK、只是读回未就绪时，绝不能发 =0,1。"""
    clock = {"now": 0.0}
    queries = {"count": 0}
    monkeypatch.setattr(modem_time_sleep_target(), "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        modem_time_sleep_target(),
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )
    calls: list[str] = []
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = True

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG?":
            queries["count"] += 1
            return f"+CPCMREG: {1 if queries['count'] >= 3 else 0}\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")

    assert modem.voice_pcm_active is True
    assert "AT+CPCMREG=0,1" not in calls
    assert calls.count("AT+CPCMREG=1") == 3


def test_simcom_pcm_stops_retrying_when_call_drops_mid_enable(monkeypatch):
    """通话中途挂断必须立刻停 CPCMREG，不能拖到 12s 超时再抛失败。

    真机 2026-08-11 17:03：CLCC 已判通话丢失后仍重试到第 4 次，随后 AT 口
    Write timeout。启用循环必须每轮检查 connected。
    """
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    calls: list[str] = []
    modem = make_modem()
    modem._call_connected_event.set()

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG=1":
            # 第一次失败后模拟对方挂断
            if calls.count("AT+CPCMREG=1") >= 1:
                modem._call_connected_event.clear()
            return "ERROR"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")  # 不抛

    assert modem.voice_pcm_active is False
    assert calls.count("AT+CPCMREG=1") == 1  # 挂断后不再打第 2 次


def test_simcom_hangup_is_idempotent(monkeypatch):
    """连续两次 hangup 只应真正拆线一次。"""
    modem, calls = _recording_modem(monkeypatch)
    modem._audio_mode = "simcom_pcm"
    modem._call_connected_event.set()
    modem._voice_pcm_active = True
    modem._connected_call_ids = {"1"}
    modem._call_hangup_done = False
    modem._hangup_complete = False
    calls.clear()

    modem.hangup()
    first = list(calls)
    modem.hangup()

    assert first.count("AT+CHUP") == 1
    assert first.count("AT+CPCMREG=0,1") == 1
    assert calls == first  # 第二次零 AT
    assert modem._hangup_complete is True


def test_simcom_hangup_can_defer_pcm_release(monkeypatch):
    """CLCC 丢线路径：先只 CHUP，等宿主关 PCM 口后再 CPCMREG=0,1。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem, calls = _recording_modem(
        monkeypatch,
        {"AT+CPCMREG?": "+CPCMREG: 0\r\nOK"},
    )
    modem._audio_mode = "simcom_pcm"
    modem._call_connected_event.set()
    modem._voice_pcm_active = True
    modem._call_hangup_done = False
    modem._hangup_complete = False
    calls.clear()

    modem.hangup(release_pcm=False)
    assert "AT+CHUP" in calls
    assert "AT+CPCMREG=0,1" not in calls
    assert modem._call_hangup_done is True
    assert modem._hangup_complete is False
    assert modem.voice_pcm_active is True  # 宿主尚未 release

    modem.hangup(release_pcm=True)
    assert calls.count("AT+CHUP") == 1
    assert "AT+CPCMREG=0,1" in calls
    assert "AT+CPCMREG?" in calls
    assert modem._hangup_complete is True
    assert modem.voice_pcm_active is False
    # 即使读回 mode=0 也标脏，下一通强制带钩子预复位。
    assert modem._pcm_endpoint_clean is False


def test_simcom_hangup_marks_dirty_even_when_stop_readback_zero(monkeypatch):
    """挂断读回 mode=0 仍标脏：下一通必须预复位，禁止跳过。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem, _ = _recording_modem(
        monkeypatch,
        {"AT+CPCMREG?": "+CPCMREG: 0\r\nOK"},
    )
    modem._audio_mode = "simcom_pcm"
    modem._call_hangup_done = False
    modem._hangup_complete = False

    modem.hangup()

    assert modem._pcm_endpoint_clean is False


def test_simcom_hangup_marks_dirty_when_stop_readback_not_zero(monkeypatch):
    """关通道 OK 但读回仍非 mode=0 → 下一通必须预复位。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem, _ = _recording_modem(
        monkeypatch,
        {"AT+CPCMREG?": "+CPCMREG: 1\r\nOK"},
    )
    modem._audio_mode = "simcom_pcm"
    modem._call_hangup_done = False
    modem._hangup_complete = False

    modem.hangup()

    assert modem._pcm_endpoint_clean is False


def test_simcom_pcm_does_not_reset_endpoint_when_no_call(monkeypatch):
    """无通话路径只试一次，不该多发复位——那会拖慢每次服务启动。"""
    calls: list[str] = []
    modem = make_modem()
    assert not modem._call_connected_event.is_set()
    monkeypatch.setattr(modem, "_send", lambda cmd: (calls.append(cmd), "ERROR")[1])

    modem.initialize_for_voice("simcom_pcm")

    assert calls.count("AT+CPCMREG=1") == 1
    assert "AT+CPCMREG=0,1" not in calls


def test_simcom_pcm_retries_until_readback_confirms_mode_one(monkeypatch):
    """写命令的 OK 不是充分条件：必须读回 mode=1 才能启动 USB 音频桥。"""
    clock = {"now": 0.0}
    queries = {"count": 0}
    monkeypatch.setattr(modem_time_sleep_target(), "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        modem_time_sleep_target(),
        "sleep",
        lambda delay: clock.__setitem__("now", clock["now"] + delay),
    )
    modem = make_modem()
    modem._call_connected_event.set()

    def fake_send(cmd: str) -> str:
        if cmd == "AT+CPCMREG?":
            queries["count"] += 1
            mode = 1 if queries["count"] >= 2 else 0
            return f"+CPCMREG: {mode}\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")

    assert queries["count"] == 2
    assert modem.voice_pcm_active is True


def test_simcom_pcm_write_ok_without_active_readback_is_not_enabled(monkeypatch):
    """无通话路径也不能把裸 OK 当作 mode=1，且仍只尝试一次。"""
    modem = make_modem()
    monkeypatch.setattr(
        modem,
        "_send",
        lambda cmd: "+CPCMREG: 0\r\nOK" if cmd == "AT+CPCMREG?" else "OK",
    )

    modem.initialize_for_voice("simcom_pcm")

    assert modem.voice_pcm_active is False


def test_simcom_pcm_raises_in_call_when_never_enabled(monkeypatch):
    """通话中始终 ERROR 必须抛：带着"PCM 没开"去启音频桥会写死 USB 端点，
    连带把 AT 口的桥拖死（真机实测通话中途模组掉线）。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem = make_modem()
    modem._call_connected_event.set()
    monkeypatch.setattr(modem, "_send", lambda cmd: "ERROR")
    with pytest.raises(RuntimeError, match="AT\\+CPCMREG=1"):
        modem.initialize_for_voice("simcom_pcm")
    assert modem.voice_pcm_active is False


def test_simcom_pcm_silent_when_no_call(monkeypatch):
    """启动期 supervisor 无通话，ERROR 是正常的：只试一次且不抛。"""
    calls: list[str] = []
    modem = make_modem()
    assert not modem._call_connected_event.is_set()
    monkeypatch.setattr(modem, "_send", lambda cmd: (calls.append(cmd), "ERROR")[1])
    modem.initialize_for_voice("simcom_pcm")   # 不抛
    assert calls.count("AT+CPCMREG=1") == 1
    assert modem.voice_pcm_active is False


def test_voice_pcm_active_cleared_on_hangup(monkeypatch):
    """挂断即关通道：状态不能泄漏到下一通，否则第二通会跳过启用直接写端点。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem = make_modem()
    modem._call_connected_event.set()
    monkeypatch.setattr(
        modem,
        "_send",
        lambda cmd: "+CPCMREG: 1\r\nOK" if cmd == "AT+CPCMREG?" else "OK",
    )
    modem.initialize_for_voice("simcom_pcm")
    assert modem.voice_pcm_active is True
    modem.hangup()
    assert modem.voice_pcm_active is False


def modem_time_sleep_target():
    from agentcall import modem as modem_mod
    return modem_mod.time


def test_simcom_pcm_retry_window_covers_slow_modems(monkeypatch):
    """真机成功点散布在接通后 1.3s / 4.9s / 8.0s，窗口必须覆盖到最慢那种。

    回归锁：窗口曾是 5×0.5s=2.5s，导致慢启动的通话整通无声（2026-08-01）。
    慢但可信的路径是 =1 已受理、读回异步变 1（不得用 ERROR 空转）。
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(modem_time_sleep_target(), "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        modem_time_sleep_target(), "sleep",
        lambda s: clock.__setitem__("now", clock["now"] + s),
    )
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = True

    def fake_send(cmd: str) -> str:
        if cmd == "AT+CPCMREG?":
            return f"+CPCMREG: {1 if clock['now'] >= 8.0 else 0}\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")
    assert modem.voice_pcm_active is True
    assert clock["now"] >= 8.0


def test_simcom_pcm_gives_up_after_window(monkeypatch):
    """读回长期不成 mode=1（accepted 挂起）时必须吃满窗口再抛，不能无限占线。"""
    clock = {"now": 0.0}
    monkeypatch.setattr(modem_time_sleep_target(), "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        modem_time_sleep_target(), "sleep",
        lambda s: clock.__setitem__("now", clock["now"] + s),
    )
    modem = make_modem()
    modem._call_connected_event.set()
    modem._pcm_endpoint_clean = True
    monkeypatch.setattr(
        modem,
        "_send",
        lambda cmd: "+CPCMREG: 0\r\nOK" if cmd == "AT+CPCMREG?" else "OK",
    )
    with pytest.raises(RuntimeError, match="仍未确认 mode=1"):
        modem.initialize_for_voice("simcom_pcm")
    assert clock["now"] >= modem._SIMCOM_PCM_ENABLE_TIMEOUT - 1


def test_simcom_pcm_no_call_tries_once_only(monkeypatch):
    """启动期无通话：只试一次，不能白等 12 秒拖慢服务启动。"""
    calls: list[str] = []
    modem = make_modem()
    monkeypatch.setattr(modem, "_send", lambda cmd: (calls.append(cmd), "ERROR")[1])
    modem.initialize_for_voice("simcom_pcm")
    assert calls.count("AT+CPCMREG=1") == 1
    assert "AT+CPCMFRM=0" not in calls          # 无通话时连帧格式都不必发


# ---- PCM 采样率必须钉成 8k（VoLTE 默认 16K 会被误解成噪声）----


def test_simcom_pcm_forces_8k_sampling(monkeypatch):
    """默认 pcm_rate=8000 时钉 AT+CPCMBANDWIDTH=1,1（双 8k）。

    历史：不设时 VoLTE 出厂偏 16K，按 8k 解会成宽带噪声（HANDOVER §3.1）。
    """
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem = make_modem()
    modem._call_connected_event.set()
    calls: list[str] = []

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")

    assert "AT+CPCMBANDWIDTH=1,1" in calls
    assert "AT+CPCMBANDWIDTH?" in calls
    assert calls.index("AT+CPCMBANDWIDTH=1,1") < calls.index("AT+CPCMREG=1")
    assert calls.index("AT+CPCMBANDWIDTH=1,1") < calls.index("AT+CPCMBANDWIDTH?")
    assert not any(c.startswith("AT+CPCMFRM") for c in calls)


def test_simcom_pcm_forces_16k_sampling_when_configured(monkeypatch):
    """MODEM_PCM_RATE=16000 时钉 AT+CPCMBANDWIDTH=0,0（双 16k）。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem = Eg25Modem(port="/dev/null-not-used", pcm_rate=16000)
    modem._call_connected_event.set()
    calls: list[str] = []

    def fake_send(cmd: str) -> str:
        calls.append(cmd)
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        if cmd == "AT+CPCMBANDWIDTH?":
            return "+CPCMBANDWIDTH: 0,0\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")

    assert "AT+CPCMBANDWIDTH=0,0" in calls
    assert "AT+CPCMBANDWIDTH=1,1" not in calls
    assert calls.index("AT+CPCMBANDWIDTH=0,0") < calls.index("AT+CPCMREG=1")


def test_simcom_pcm_bandwidth_readback_logged(monkeypatch, caplog):
    """通话中设置后读回 1,1，确认 VoLTE/非 VoLTE 都钉在 8k。"""
    import logging

    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem = make_modem()
    modem._call_connected_event.set()

    def fake_send(cmd: str) -> str:
        if cmd == "AT+CPCMBANDWIDTH?":
            return "+CPCMBANDWIDTH: 1,1\r\nOK"
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    with caplog.at_level(logging.INFO, logger="agentcall.modem"):
        modem.initialize_for_voice("simcom_pcm")
    assert any("PCM 带宽已确认 8kHz" in r.message for r in caplog.records)


def test_bandwidth_failure_does_not_block_pcm(monkeypatch):
    """老固件可能没这条命令；失败只降级，不能让整通电话没音频。"""
    monkeypatch.setattr(modem_time_sleep_target(), "sleep", lambda s: None)
    modem = make_modem()
    modem._call_connected_event.set()

    def fake_send(cmd: str) -> str:
        if cmd.startswith("AT+CPCMBANDWIDTH"):
            raise RuntimeError("unsupported")
        if cmd == "AT+CPCMREG?":
            return "+CPCMREG: 1\r\nOK"
        return "OK"

    monkeypatch.setattr(modem, "_send", fake_send)
    modem.initialize_for_voice("simcom_pcm")     # 不抛
    assert modem.voice_pcm_active is True
