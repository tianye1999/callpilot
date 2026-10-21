"""Eg25Modem 解析逻辑单测（不开串口，直接测纯解析路径）。"""

from __future__ import annotations

import logging
import threading
import time

import pytest
import serial

from agentcall.modem import Eg25Modem, parse_sms_pdu


@pytest.fixture(autouse=True)
def _disable_pcm_pin_by_default(monkeypatch):
    # Keep existing command/retry tests independent of the host platform.
    monkeypatch.setenv("MODEM_PCM_PIN_8K", "false")


def make_modem() -> Eg25Modem:
    """构造未连接串口的 modem 实例，仅用于测解析。"""
    return Eg25Modem(port="/dev/null-not-used")


# ---- create_modem 厂商工厂 ----

def test_create_modem_defaults_to_quectel():
    from agentcall.modem import SerialModem, create_modem

    modem = create_modem("/dev/null-not-used")
    assert isinstance(modem, Eg25Modem)
    assert isinstance(modem, SerialModem)


def test_create_modem_unknown_vendor_falls_back_to_quectel(caplog):
    """未识别 vendor 回退 quectel 并告警，保证现网行为不变。"""
    from agentcall.modem import create_modem

    with caplog.at_level(logging.WARNING):
        modem = create_modem("/dev/null-not-used", vendor="no-such-vendor")
    assert isinstance(modem, Eg25Modem)
    assert "回退 quectel" in caplog.text


def test_create_modem_simcom_returns_sim7600():
    from agentcall.modem import Sim7600Modem, create_modem

    for vendor in ("simcom", "sim7600", "SIM7600G"):
        modem = create_modem("/dev/null-not-used", vendor=vendor)
        assert isinstance(modem, Sim7600Modem), vendor


# ---- Sim7600Modem 厂商钩子 ----

def make_sim7600():
    from agentcall.modem import Sim7600Modem

    return Sim7600Modem(port="/dev/null-not-used")


def test_sim7600_initialize_for_voice_enables_cpcmreg(monkeypatch, caplog):
    modem = make_sim7600()
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")
    with caplog.at_level(logging.INFO):
        modem.initialize_for_voice("nmea")
    # 不再发 AT+CLVL（治的是不存在的削波，且每通白花 2.3s，见 WIL-104）。
    assert sent == ["AT+CPCMREG=1"]
    assert "AT+CPCMREG=1" in caplog.text


def test_sim7600_pins_pcm_before_enable_on_every_call(monkeypatch):
    monkeypatch.setenv("MODEM_PCM_PIN_8K", "true")
    modem = make_sim7600()
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")

    modem.initialize_for_voice("nmea")
    modem._disable_voice_channel()
    modem.initialize_for_voice("nmea")

    assert sent == [
        "AT+CPCMBANDWIDTH=1,1", "AT+CPCMREG=1", "AT+CPCMREG=0",
        "AT+CPCMBANDWIDTH=1,1", "AT+CPCMREG=1",
    ]
    assert modem.last_voice_enable == {"ok": True, "attempts": 1, "degraded": False}


@pytest.mark.parametrize("failure", ["ERROR", "", serial.SerialException("unavailable")])
def test_sim7600_pcm_pin_failure_preserves_enable_retries(monkeypatch, caplog, failure):
    monkeypatch.setenv("MODEM_PCM_PIN_8K", "true")
    modem = make_sim7600()
    sent = []
    responses = iter(["ERROR", "OK"])

    def fake_send(cmd):
        sent.append(cmd)
        if cmd == "AT+CPCMBANDWIDTH=1,1":
            if isinstance(failure, Exception):
                raise failure
            return failure
        return next(responses)

    monkeypatch.setattr(modem, "_send", fake_send)
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    with caplog.at_level(logging.WARNING):
        modem.initialize_for_voice("nmea")

    assert sent == ["AT+CPCMBANDWIDTH=1,1", "AT+CPCMREG=1", "AT+CPCMREG=1"]
    assert modem.last_voice_enable == {"ok": True, "attempts": 2, "degraded": True}
    assert "CPCMBANDWIDTH" in caplog.text


def test_sim7600_initialize_for_voice_all_error_does_not_raise(monkeypatch):
    """全 ERROR（无活跃通话，supervisor 启动期）有界尝试后放弃、不抛（否则误判连接失败）。"""
    modem = make_sim7600()
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "ERROR")
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    modem.initialize_for_voice("nmea")  # 不抛即通过
    assert sent == ["AT+CPCMREG=1"] * modem._CPCMREG_ENABLE_ATTEMPTS


def test_sim7600_initialize_for_voice_retries_until_enabled(monkeypatch):
    """不依赖 is_call_connected（接通事件有竞态）：ERROR 时重试到 OK 即停。"""
    modem = make_sim7600()  # 不 set 接通事件，证明启用不依赖它
    responses = iter(["ERROR", "ERROR", "OK", "OK"])
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or next(responses))
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    modem.initialize_for_voice("nmea")
    assert sent == ["AT+CPCMREG=1"] * 3  # 到 OK 即停


def test_sim7600_voice_enable_snapshot_success_first_try(monkeypatch):
    """健康态一次 OK：快照 ok=True/attempts=1/degraded=False（WIL-95 v3 pcm 指纹）。"""
    modem = make_sim7600()
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    modem.initialize_for_voice("nmea")
    assert modem.last_voice_enable == {"ok": True, "attempts": 1, "degraded": False}


def test_sim7600_voice_enable_snapshot_marks_degraded_on_retry(monkeypatch):
    """要重试才成 = 劣化：快照 degraded=True，与 _pcm_degraded 恢复标记一致。"""
    modem = make_sim7600()
    responses = iter(["ERROR", "OK"])
    monkeypatch.setattr(modem, "_send", lambda cmd: next(responses))
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    modem.initialize_for_voice("nmea")
    assert modem.last_voice_enable == {"ok": True, "attempts": 2, "degraded": True}
    assert modem._pcm_degraded is True


def test_sim7600_voice_enable_snapshot_records_total_failure(monkeypatch):
    """全败（通话中=整通无声）：ok=False；快照总被覆写，不残留上一次的值。"""
    modem = make_sim7600()
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    modem.initialize_for_voice("nmea")  # 先造一个成功快照
    monkeypatch.setattr(modem, "_send", lambda cmd: "ERROR")
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    modem.initialize_for_voice("nmea")
    assert modem.last_voice_enable == {
        "ok": False,
        "attempts": modem._CPCMREG_ENABLE_ATTEMPTS,
        "degraded": True,
    }


def test_sim7600_disable_voice_channel_sends_cpcmreg_off(monkeypatch):
    modem = make_sim7600()
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")
    modem._disable_voice_channel()
    assert sent == ["AT+CPCMREG=0"]


def test_sim7600_dtmf_uses_vts(monkeypatch):
    modem = make_sim7600()
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    assert modem.send_dtmf("2") is True
    assert sent == ["AT+VTS=2"]


def test_sim7600_dtmf_falls_back_to_quoted_vts(monkeypatch):
    modem = make_sim7600()
    sent = []

    def fake_send(cmd):
        sent.append(cmd)
        return "OK" if '"' in cmd else "ERROR"

    monkeypatch.setattr(modem, "_send", fake_send)
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    assert modem.send_dtmf("5") is True
    assert sent == ["AT+VTS=5", 'AT+VTS="5"']


def test_sim7600_pcm_ready_defaults_true():
    """SIM7600 无 +QPCMV 带内流控 URC，继承基类 no-op，pcm_ready 恒 True。"""
    modem = make_sim7600()
    assert modem.pcm_ready() is True


def test_sim7600_hangup_uses_chup(monkeypatch):
    """SIM7600 语音挂断用 AT+CHUP（ATH 不可靠），随后关 PCM 通道。"""
    modem = make_sim7600()
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")
    modem.hangup()
    assert sent == ["AT+CHUP", "AT+CPCMREG=0"]


def test_eg25_hangup_still_uses_ath(monkeypatch):
    """回归：Quectel 挂断仍用 ATH（默认钩子不变）。"""
    modem = make_modem()
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")
    modem.hangup()
    assert sent == ["ATH", "AT+QPCMV=0"]


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


def test_hangup_clears_last_caller_so_withheld_call_is_not_misattributed():
    """挂断必须清 _last_caller：隐藏号码来电的 +CLIP 是空引号、匹配不到，
    不清的话下一通会把上一通的号码当成本通来电（转告短信会写错回拨号码）。"""
    modem = make_modem()
    rings: list[str | None] = []
    modem.on_ring(rings.append)

    modem._buffer = '\r\n+CLIP: "13600000000",129\r\n\r\nRING\r\n'
    modem._process_buffer()
    modem._buffer = "\r\nNO CARRIER\r\n"
    modem._process_buffer()
    modem._buffer = "\r\nRING\r\n"  # 第二通：隐藏号码，无 CLIP
    modem._process_buffer()

    assert rings == ["13600000000", None]  # 未知就是未知，不冒充上一通


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


@pytest.mark.parametrize("factory", [make_modem, make_sim7600])
@pytest.mark.parametrize("fallback", [False, True])
def test_dtmf_completion_sums_send_time_without_logging_digits(factory, fallback, monkeypatch, caplog):
    modem = factory()
    now = 0.0
    commands: list[str] = []

    def send(cmd: str) -> str:
        nonlocal now
        commands.append(cmd)
        now += 0.125
        if fallback and (cmd.startswith("AT+QVTS") or '"' not in cmd):
            return "ERROR"
        return "OK"

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(modem, "_send", send)
    monkeypatch.setattr("agentcall.modem.time.monotonic", lambda: now)
    monkeypatch.setattr("agentcall.modem.time.sleep", sleep)
    with caplog.at_level(logging.INFO):
        assert modem.send_dtmf("7#A") is True

    expected_seconds = 0.750 if fallback else 0.375
    assert len(commands) == (6 if fallback else 3)
    assert [record.getMessage() for record in caplog.records] == [
        f"DTMF 发送完成: count=3, result=success, send_seconds={expected_seconds:.3f}"
    ]


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


class BlockingFakeSerial(FakeSerial):
    """Model the port's 0.2s empty-read timeout, without opening any device."""

    def __init__(self) -> None:
        super().__init__()
        self.reader_checked = threading.Event()
        self.empty_reads = 0

    @property
    def in_waiting(self) -> int:
        with self._lock:
            waiting = len(self._pending)
        if threading.current_thread().name == "modem-reader":
            self.reader_checked.set()
        return waiting

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            available = len(self._pending)
        if size > available:
            self.empty_reads += 1
            time.sleep(0.2)
        return super().read(size)


@pytest.mark.parametrize("factory", [make_modem, make_sim7600])
def test_reader_does_not_starve_repeated_sends(factory):
    modem = factory()
    fake = BlockingFakeSerial()
    modem._ser = fake
    modem._running = True
    elapsed: list[float] = []
    responses: list[str] = []

    def send_repeatedly() -> None:
        for _ in range(15):
            started = time.monotonic()
            responses.append(modem._send("AT+CSQ"))
            elapsed.append(time.monotonic() - started)
            time.sleep(0.02)  # Let the reader run between commands.

    reader = threading.Thread(target=modem._read_loop, name="modem-reader", daemon=True)
    sender = threading.Thread(target=send_repeatedly, daemon=True)
    reader.start()
    try:
        assert fake.reader_checked.wait(timeout=1)
        sender.start()
        sender.join(timeout=3)
        finished = not sender.is_alive()
    finally:
        modem._running = False
        reader.join(timeout=2)
        if sender.ident is not None:
            sender.join(timeout=2)

    assert not reader.is_alive() and not sender.is_alive()
    assert finished, "reader starved the sender"
    assert responses == ["\r\nOK\r\n"] * 15  # No command response was stolen.
    assert max(elapsed) < 0.5, elapsed  # Bounds lock wait AND command completion.
    assert fake.empty_reads == 0, "reader must not block on an empty port"


@pytest.mark.parametrize("has_data", [False, True])
def test_reader_yields_outside_serial_lock(monkeypatch, has_data):
    modem = make_modem()
    polls = 0
    reads: list[int] = []
    sleeps: list[float] = []

    class IdleSerial(FakeSerial):
        @property
        def in_waiting(self) -> int:
            nonlocal polls
            assert modem._serial_lock._is_owned()  # type: ignore[attr-defined]
            polls += 1
            if polls == 3:
                modem._running = False
            return 6 if has_data else 0

        def read(self, size: int = 1) -> bytes:
            assert modem._serial_lock._is_owned()  # type: ignore[attr-defined]
            reads.append(size)
            return b"\r\nOK\r\n" if has_data else b""

    def sleep(seconds: float) -> None:
        assert not modem._serial_lock._is_owned()  # type: ignore[attr-defined]
        sleeps.append(seconds)

    modem._ser = IdleSerial()
    modem._running = True
    monkeypatch.setattr("agentcall.modem.time.sleep", sleep)
    modem._read_loop()

    assert reads == ([6] * 3 if has_data else [])
    assert polls == 3
    assert len(sleeps) == polls
    assert all(0.01 <= seconds <= 0.05 for seconds in sleeps)


@pytest.mark.parametrize("invalidate", ["generation", "pause"])
def test_reader_drops_data_invalidated_during_yield(monkeypatch, invalidate):
    modem = make_sim7600()
    fake = FakeSerial()
    fake._pending = b"\r\nRING\r\n"
    modem._ser = fake
    modem._running = True
    rings: list[str | None] = []
    modem.on_ring(rings.append)

    def start_maintenance(_seconds: float) -> None:
        with modem._serial_lock:
            if invalidate == "generation":
                modem._maintenance_generation += 1
            else:
                modem._reset_in_progress = True
        modem._running = False

    monkeypatch.setattr("agentcall.modem.time.sleep", start_maintenance)
    modem._read_loop()

    assert fake._pending == b""
    assert rings == []
    assert modem._buffer == ""


@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        ("at+vts=9\r", "AT+VTS"),
        ('AT+VTS="9"', "AT+VTS"),
        ('AT+QVTS="8"', "AT+QVTS"),
        ("ATD+8613800138000;", "ATD"),
        ('AT+CMGS="13800138000"\rprivate sms', "AT+CMGS"),
        ('AT+CPMS="private"', "AT+CPMS"),
        ("ATE0", "ATE"),
        ("AT+CLCC", "AT+CLCC"),
        ("private sms 12345", "unknown"),
    ],
)
@pytest.mark.parametrize("wait_seconds", [0.2, 0.25])
def test_write_command_lock_timing_logs_only_safe_prefix(monkeypatch, caplog, command, prefix, wait_seconds):
    modem = make_modem()
    modem._ser = FakeSerial()
    timestamps = iter([0.0, wait_seconds])
    monkeypatch.setattr("agentcall.modem.time.monotonic", lambda: next(timestamps))

    with caplog.at_level(logging.INFO):
        assert "OK" in modem._write_command(command)

    messages = [record.getMessage() for record in caplog.records]
    assert messages == (
        [f"[timing] 串口锁等待 {wait_seconds:.3f}s cmd={prefix}"] if wait_seconds > 0.2 else []
    )


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


def test_write_command_measures_real_lock_contention(caplog):
    modem = make_modem()
    lock = TrackingRLock("dtmf-sender")
    modem._serial_lock = lock  # type: ignore[assignment]
    modem._ser = FakeSerial()
    responses: list[str] = []
    sender = threading.Thread(
        target=lambda: responses.append(modem._send('AT+VTS="9"')),
        name="dtmf-sender",
        daemon=True,
    )

    with caplog.at_level(logging.INFO):
        try:
            with lock:
                sender.start()
                assert lock.waiting.wait(timeout=1)
                time.sleep(0.25)
        finally:
            sender.join(timeout=2)

    assert not sender.is_alive()
    assert responses == ["\r\nOK\r\n"]
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith("[timing] 串口锁等待 ")
    assert record.getMessage().endswith("s cmd=AT+VTS")
    assert record.args[0] >= 0.25


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


@pytest.mark.parametrize("factory", [make_modem, make_sim7600])
def test_reader_preserves_fragmented_urcs_and_sms(factory):
    modem = factory()

    class FragmentedSerial(CMTIFakeSerial):
        def __init__(self) -> None:
            super().__init__()
            self.fragments = [b"\r\nRI", b"NG\r\n+CMT", b'I: "SM",5\r\n']

        @property
        def in_waiting(self) -> int:
            with self._lock:
                if not self._pending and self.fragments:
                    self._pending = self.fragments.pop(0)
                return len(self._pending)

    fake = FragmentedSerial()
    modem._ser = fake
    modem._running = True
    rings: list[str | None] = []
    messages: list[tuple[str | None, str]] = []
    deleted = threading.Event()
    modem.on_ring(rings.append)
    modem.on_sms(lambda sender, body, ts: messages.append((sender, body)))
    delete_sms = modem._delete_stored_sms

    def delete_and_notify(index: str) -> None:
        delete_sms(index)
        deleted.set()

    modem._delete_stored_sms = delete_and_notify
    reader = threading.Thread(target=modem._read_loop, daemon=True)
    reader.start()
    try:
        assert deleted.wait(timeout=2)
    finally:
        modem._running = False
        reader.join(timeout=2)

    assert not reader.is_alive()
    assert rings == [None]
    assert messages == [("+8613800000000", "hello from send")]
    assert fake.writes == ['AT+CPMS="SM"', "AT+CMGR=5", "AT+CMGD=5"]
    assert fake.fragments == []


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


# ---- WIL-110：长断连后必须能自愈（桥自愈了服务也要回来）----

def test_reconnect_retries_on_runtime_error(monkeypatch):
    """_open_serial 抛 RuntimeError（_send 在 _opening 期上抛）时必须退避重试，
    不能穿透打死调用方线程——真机曾因此在桥恢复后永远连不回来。"""
    modem = make_modem()
    modem._running = True
    attempts = []

    def flaky_open():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("模组未连接")   # 早先会穿透 except 子句

    monkeypatch.setattr(modem, "_open_serial", flaky_open)
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    modem._reconnect()                          # 不抛即通过
    assert len(attempts) == 3                   # 前两次失败后仍继续重试


def test_read_loop_survives_reconnect_exception(monkeypatch):
    """读线程是来电感知命脉：重连里抛任何异常都不许让它退出。"""
    modem = make_modem()
    modem._running = True
    calls = {"reconnect": 0}

    class _DeadSerial:
        is_open = True

        @property
        def in_waiting(self):
            raise serial.SerialException("串口未打开")

    modem._ser = _DeadSerial()

    def boom():
        calls["reconnect"] += 1
        if calls["reconnect"] >= 3:
            modem._running = False              # third round ends the loop
        raise RuntimeError("模组未连接")

    monkeypatch.setattr(modem, "_reconnect", boom)
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    modem._read_loop()                          # 不抛、能正常退出即通过
    assert calls["reconnect"] == 3              # 异常后继续重试而非死掉


def test_is_connected_reflects_transport_state():
    """看门狗据此判断是否需要重建连接。"""
    modem = make_modem()
    assert modem.is_connected() is False
    modem._emit_connection_state(True)
    assert modem.is_connected() is True
    modem._emit_connection_state(False)
    assert modem.is_connected() is False


# ---- WIL-109 续：劣化检测 + 自愈请求（覆盖「收数据但播放卡」形态）----

def test_sim7600_marks_degraded_when_enable_needs_retry(monkeypatch, tmp_path, caplog):
    """CPCMREG 需重试 = 模组已劣化（真机：好通 1 次即成，坏通要重试）。"""
    modem = make_sim7600()
    monkeypatch.setattr(modem, "RECOVERY_REQUEST_PATH", str(tmp_path / "req"))
    responses = iter(["ERROR", "OK"])            # 第1次失败、第2次成功
    monkeypatch.setattr(modem, "_send", lambda cmd: next(responses))
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    with caplog.at_level(logging.WARNING):
        modem.initialize_for_voice("nmea")
    assert modem._pcm_degraded is True
    assert "判定模组已劣化" in caplog.text


def test_sim7600_first_try_enable_not_degraded(monkeypatch, tmp_path):
    """一次就 OK（健康态）不得误判劣化——否则每通都触发 ~100s 自愈。"""
    modem = make_sim7600()
    monkeypatch.setattr(modem, "RECOVERY_REQUEST_PATH", str(tmp_path / "req"))
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    modem.initialize_for_voice("nmea")
    assert modem._pcm_degraded is False
    modem._disable_voice_channel()
    assert not (tmp_path / "req").exists()        # 健康态不写请求


def test_sim7600_writes_recovery_request_on_hangup(monkeypatch, tmp_path):
    """劣化通话结束时写哨兵，由桥进程读到后执行组合切换自愈。"""
    modem = make_sim7600()
    req = tmp_path / "req"
    monkeypatch.setattr(modem, "RECOVERY_REQUEST_PATH", str(req))
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    modem._pcm_degraded = True
    modem._disable_voice_channel()
    assert req.exists()                            # 已请求自愈
    assert modem._pcm_degraded is False             # 标志已消费，不重复请求


def test_sim7600_recovery_request_failure_does_not_break_hangup(monkeypatch):
    """写哨兵失败不得影响挂断流程。"""
    modem = make_sim7600()
    monkeypatch.setattr(modem, "RECOVERY_REQUEST_PATH", "/nonexistent-dir/req")
    monkeypatch.setattr(modem, "_send", lambda cmd: "OK")
    modem._pcm_degraded = True
    modem._disable_voice_channel()                  # 不抛即通过


def test_reconnect_non_owner_wait_is_bounded(monkeypatch):
    """非 owner 等待必须有界：调用方可能持有 _serial_lock，无限等会死锁（WIL-111）。

    真机死锁形态：hangup() 持 _serial_lock → 锁内 _send 失败 → _reconnect 作为
    非 owner 无限等；而 owner 需要同一把锁开串口 → 循环等待，全线程僵死。
    """
    modem = make_modem()
    modem._running = True
    modem._reconnect_in_progress = True      # 假装别人正在重连
    modem._reconnect_complete.clear()        # 且永不完成
    monkeypatch.setattr(modem, "_RECONNECT_WAIT_TIMEOUT", 0.3)

    start = time.monotonic()
    with modem._serial_lock:                 # 模拟持锁调用（hangup 的形态）
        modem._reconnect()
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"应有界返回，实际等了 {elapsed:.1f}s"


def test_settle_delay_shorter_for_sim7600_than_quectel():
    """SIM7600 自带重试，不必盲等一整秒；Quectel 维持原 1.0s 不回归（WIL-104）。"""
    from agentcall.modem import Sim7600Modem

    assert make_modem().POST_ANSWER_SETTLE_SECONDS == 1.0
    assert Sim7600Modem("/dev/null-not-used").POST_ANSWER_SETTLE_SECONDS < 0.5


# ---- WIL-120 四期：三方通话原语（真机验证 gate 在 chld_probe） ----


def test_parse_clcc_lines_maps_states_and_multiparty():
    from agentcall.modem import parse_clcc_lines

    response = (
        '+CLCC: 1,0,1,0,0,"18005551234",129\r\n'
        '+CLCC: 2,0,0,0,1,"16125550000",129\r\n'
        "OK"
    )
    calls = parse_clcc_lines(response)
    assert calls == [
        {"index": 1, "direction": "outgoing", "state": "held",
         "multiparty": False, "number": "18005551234"},
        {"index": 2, "direction": "outgoing", "state": "active",
         "multiparty": True, "number": "16125550000"},
    ]
    # 未知 stat 码原样数字化，不猜语义。
    weird = parse_clcc_lines("+CLCC: 1,1,7,0,0")
    assert weird[0]["state"] == "7" and weird[0]["direction"] == "incoming"
    # 空/纯 OK/坏行：安静返回空表。
    assert parse_clcc_lines("OK") == []
    assert parse_clcc_lines("") == []
    assert parse_clcc_lines("+CLCC: garbage") == []


def test_three_way_primitives_send_standard_chld_commands(monkeypatch):
    modem = make_sim7600()
    sent = []
    monkeypatch.setattr(
        modem, "_send",
        lambda cmd: sent.append(cmd) or (
            '+CLCC: 1,0,0,0,0,"611",129\r\nOK' if cmd == "AT+CLCC" else "OK"
        ),
    )
    assert modem.hold_toggle() is True
    assert modem.merge_calls() is True
    modem.query_chld_capabilities()
    calls = modem.list_calls()
    assert sent == ["AT+CHLD=2", "AT+CHLD=3", "AT+CHLD=?", "AT+CLCC"]
    assert calls[0]["number"] == "611" and calls[0]["state"] == "active"


def test_dial_second_does_not_disturb_first_call_state(monkeypatch):
    """第二路拨号绝不复用 dial()：不清接通事件/CLCC 计数（第一路仍在线）。"""
    modem = make_sim7600()
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")
    modem._call_connected_event.set()  # 第一路在线
    modem.dial_second("16125550000")
    assert sent == ["ATD16125550000;"]
    assert modem.is_call_connected() is True  # 未被清掉

    import pytest as _pytest
    with _pytest.raises(ValueError):
        modem.dial_second("  ")
