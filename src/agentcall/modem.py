"""Quectel EG25-G 模组 AT 指令封装。"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any, Callable

import serial

from . import config, modem_hardware, platforms, port_detect
from .sim_identity import (
    UNKNOWN_SIM,
    SimIdentity,
    identify,
    with_eps_registration,
    with_registration,
    with_service_number_override,
)

logger = logging.getLogger(__name__)

CEND_PATTERN = re.compile(r"\+CEND:[^\r\n]*(?=\r?\n)")


@dataclass
class _ReleaseReport:
    """每通独立的诊断快照，后台结果不能写入下一通。"""

    causes: list[dict[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)
    requested: bool = False
    pending: bool = False
    deferred: bool = False
    started: bool = False
    hung_up: bool = False
    before_reset: bool = False
    after_query: Callable[[], None] | None = None

RING_PATTERN = re.compile(r"(?:^|\r\n)RING(?:\r\n|$)", re.MULTILINE)
CLIP_PATTERN = re.compile(r'\+CLIP:\s*"([^"]+)"')
CLCC_PATTERN = re.compile(
    r'\+CLCC:\s*(?P<idx>\d+),(?P<dir>\d+),(?P<stat>\d+),(?P<mode>\d+),'
    r'(?P<mpty>\d+)(?:,"(?P<number>[^"]*)",(?P<type>\d+))?'
)
QPCMV_PATTERN = re.compile(r"\+QPCMV:\s*(\d+),(\d+)")
CMTI_PATTERN = re.compile(r'\+CMTI:\s*"([^"]*)",\s*(\d+)')
CREG_PATTERN = re.compile(r"\+CREG:\s*(?:\d+\s*,\s*)?\d+(?=\s|$)")
# 等完整行到达再消费,避免分片中的状态或位置字段被提前截断。
CEREG_PATTERN = re.compile(r"\+CEREG:[ \t]*\d+(?:[ \t]*,[^\r\n]*)?[ \t]*(?=\r?\n)")
QSIMSTAT_PATTERN = re.compile(r"\+QSIMSTAT:\s*(\d+)\s*,\s*(\d+)")

# GSM 03.38 默认字符表（基础表，用于 7-bit 短信解码）
GSM7_BASIC = (
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\x1b\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
)


def _swap_nibbles(hex_str: str) -> str:
    out: list[str] = []
    for i in range(0, len(hex_str) - 1, 2):
        out.append(hex_str[i + 1])
        out.append(hex_str[i])
    return "".join(out)


def _decode_pdu_address(digits_hex: str, addr_type: int, digit_count: int) -> str:
    """解码 PDU 地址字段（发件号码）。"""
    # 0x50 掩码表示 Alphanumeric（字母数字发件人，如运营商名）
    if (addr_type & 0x70) == 0x50:
        try:
            septets = _unpack_gsm7(bytes.fromhex(digits_hex), (len(digits_hex) * 4) // 7)
            return _map_gsm7(septets)
        except Exception:  # noqa: BLE001
            return ""
    swapped = _swap_nibbles(digits_hex)
    number = swapped[:digit_count].replace("F", "").replace("f", "")
    if addr_type == 0x91:  # 国际号码
        number = "+" + number
    return number


def _decode_pdu_scts(scts_hex: str) -> str:
    """解码 PDU 时间戳 (7 字节, 半字节交换)。"""
    try:
        d = _swap_nibbles(scts_hex)
        yy, mm, dd, hh, mi, ss = d[0:2], d[2:4], d[4:6], d[6:8], d[8:10], d[10:12]
        return f"{yy}/{mm}/{dd},{hh}:{mi}:{ss}"
    except Exception:  # noqa: BLE001
        return ""


def _unpack_gsm7(data: bytes, septet_count: int) -> list[int]:
    res: list[int] = []
    buf = 0
    bits = 0
    for b in data:
        buf |= b << bits
        bits += 8
        while bits >= 7 and len(res) < septet_count:
            res.append(buf & 0x7F)
            buf >>= 7
            bits -= 7
    return res


def _map_gsm7(septets: list[int]) -> str:
    chars: list[str] = []
    skip = False
    for code in septets:
        if skip:
            skip = False
            continue
        if code == 0x1B:  # 扩展转义，简化处理直接跳过下一字符
            skip = True
            continue
        if 0 <= code < len(GSM7_BASIC):
            chars.append(GSM7_BASIC[code])
    return "".join(chars)


def parse_sms_pdu(pdu: str) -> tuple[str | None, str, str] | None:
    """解析一条 SMS-DELIVER PDU，返回 (发件号码, 时间戳, 正文)。失败返回 None。"""
    try:
        pdu = re.sub(r"\s+", "", pdu)
        pos = 0

        def take(n: int) -> str:
            nonlocal pos
            s = pdu[pos:pos + n]
            pos += n
            return s

        smsc_len = int(take(2), 16)
        take(smsc_len * 2)  # 跳过短信中心号码
        first_octet = int(take(2), 16)
        udhi = bool(first_octet & 0x40)

        oa_len = int(take(2), 16)  # 发件号码位数
        oa_type = int(take(2), 16)
        oa_octets = (oa_len + 1) // 2
        oa_digits = take(oa_octets * 2)
        sender = _decode_pdu_address(oa_digits, oa_type, oa_len) or None

        take(2)  # TP-PID
        dcs = int(take(2), 16)
        scts = take(14)
        timestamp = _decode_pdu_scts(scts)

        udl = int(take(2), 16)
        ud_hex = pdu[pos:]
        ud = bytes.fromhex(ud_hex) if ud_hex else b""

        udh_bytes = 0
        if udhi and ud:
            udh_bytes = ud[0] + 1

        coding = dcs & 0x0C
        if coding == 0x08:  # UCS2 (中文)
            body = ud[udh_bytes:].decode("utf-16-be", errors="replace")
        elif coding == 0x04:  # 8-bit
            body = ud[udh_bytes:].decode("latin-1", errors="replace")
        else:  # 7-bit GSM
            if udhi:
                header_septets = (udh_bytes * 8 + 6) // 7
                septets = _unpack_gsm7(ud, udl)[header_septets:]
            else:
                septets = _unpack_gsm7(ud, udl)
            body = _map_gsm7(septets)

        return sender, timestamp, body
    except Exception:  # noqa: BLE001
        return None


# CLCC stat 码 → 可读状态（3GPP TS 27.007）；未知码原样数字化，不猜。
_CLCC_STATES = {
    0: "active", 1: "held", 2: "dialing",
    3: "alerting", 4: "incoming", 5: "waiting",
}


def parse_clcc_lines(response: str) -> list[dict[str, object]]:
    """解析 ``AT+CLCC`` 响应为通话列表（纯函数，供 list_calls 与单测）。

    解析不动的行跳过；multiparty 位（mpty）用于确认 CHLD=3 后确实并成了
    多方通话——这是 Path B「并入成功」的唯一模组侧证据（WIL-120 四期）。
    """
    calls: list[dict[str, object]] = []
    for match in CLCC_PATTERN.finditer(response or ""):
        stat = int(match.group("stat"))
        calls.append({
            "index": int(match.group("idx")),
            "direction": "outgoing" if match.group("dir") == "0" else "incoming",
            "state": _CLCC_STATES.get(stat, str(stat)),
            "multiparty": match.group("mpty") == "1",
            "number": match.group("number") or "",
        })
    return calls


def _looks_like_pdu(body: str) -> bool:
    compact = re.sub(r"\s+", "", body)
    return len(compact) >= 20 and bool(re.fullmatch(r"[0-9A-Fa-f]+", compact))


def parse_csq(response: str) -> tuple[int | None, str]:
    match = re.search(r"\+CSQ:\s*(\d+)\s*,\s*\d+", response)
    csq = int(match.group(1)) if match else None
    if csq is None or not 0 <= csq <= 31:
        return csq, "unknown"
    return csq, "weak" if csq < 10 else "fair" if csq < 15 else "good" if csq < 20 else "excellent"


class SerialModem:
    """串口 AT 模组基类：来电监听、接听、挂断、收发短信、SIM 身份等厂商无关逻辑。

    接打电话/短信走标准 3GPP 指令（``ATA``/``ATD``/``AT+CLCC``/``AT+CMG*``/``AT+CIMI``）。
    厂商专有行为以钩子方法暴露、由子类覆写：语音通道启用 ``initialize_for_voice``、
    单个 DTMF 按键 ``_send_dtmf_digit``、语音通道关闭 ``_disable_voice_channel``、
    带内 PCM 流控 URC 解析 ``_scan_pcm_flow``。Quectel EC20/EG25 见 ``Eg25Modem``。
    """

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self.model = ""
        self.firmware = ""
        self.signal: dict[str, Any] = {"csq": None, "level": "unknown", "updated_at": None}
        self.last_signal = self.signal.copy()
        self._last_signal_poll = 0.0
        self._sim_reread_until = 0.0
        self._hardware_call_present = False
        self.run_idle_hardware: Callable[[Callable[[], None]], bool] | None = None
        # port 为 auto 哨兵时每次打开都重新探测，这里存本次解析出的实际端口（供日志）。
        self._active_port: str | None = None
        # SIM 身份缓存(#88):连接/重连后读一次 CIMI/CREG/CEREG;换卡靠重插/重连触发刷新。
        self._sim_identity: SimIdentity = UNKNOWN_SIM
        self._ser: serial.Serial | None = None
        self._reader_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        # close() 是终态：置位后 start_listener/_reconnect 拒绝再启动，
        # 防止 stop 与后台 supervisor 首连之间的"资源复活"竞态。
        self._closed = False
        self._serial_lock = threading.RLock()
        self._release_report = _ReleaseReport()
        # 串口重连串行化：多线程（读循环/CLCC 轮询/发送）可能同时发现断连，
        # 所有获取都遵循 _serial_lock -> _reconnect_lock，禁止反向锁序。
        self._reconnect_lock = threading.Lock()
        self._reconnect_generation = 0
        self._reconnect_in_progress = False
        self._reconnect_complete = threading.Event()
        self._reconnect_complete.set()
        self._connection_state_lock = threading.Lock()
        self._connection_online = False
        self._connection_disconnected_at: float | None = None
        self._on_connection_state: Callable[[bool], None] | None = None
        self._connection_callback_queue: Queue[bool | None] = Queue()
        self._connection_callback_thread: threading.Thread | None = None
        # 初始化序列进行中：此时 _send 失败不自触发重连（交给 _reconnect 的重试循环），
        # 避免同线程重入 _reconnect 造成死锁。
        self._opening = False
        self._running = False
        self._on_ring: Callable[[str | None], None] | None = None
        self._on_hangup: Callable[[], None] | None = None
        self._on_sms: Callable[[str | None, str, str], None] | None = None
        self._on_call_connected: Callable[[str | None], None] | None = None
        self._on_sim_identity: Callable[[SimIdentity], None] | None = None
        self._sim_state_lock = threading.Lock()
        self._last_notified_sim_identity = UNKNOWN_SIM
        self._sim_refresh_lock = threading.Lock()
        self._sim_refresh_thread: threading.Thread | None = None
        self._sim_refresh_requested_generation: int | None = None
        self._sim_refresh_generation = 0
        self._buffer = ""
        self._maintenance_generation = 0
        self._processing_response_cmti = False
        self._last_caller: str | None = None
        self._last_dialed: str | None = None
        self._incoming_call_ids: set[str] = set()
        self._connected_call_ids: set[str] = set()
        # 通话在线标志：拨号后清除，外呼接通（CLCC dir=0 stat=0）或来电
        # 接听（ATA）时置位。除 _wait_connected 外，还是 CLCC「通话消失」
        # 判定的前提（见 _process_clcc_response）。
        self._call_connected_event = threading.Event()
        self._fast_poll_until = 0.0
        self._poll_wakeup = threading.Event()
        # CLCC 消失/失联计数：通话在线期间，有效 CLCC 连续无活跃通话、或
        # 轮询连续异常达到阈值，判定通话已丢并触发 on_hangup 收尾会话。
        # 真机事故（2026-07-08）：通话中串口断死，NO CARRIER 永远收不到，
        # 重连后 CLCC 每 2s 返回空却无人处理，会话僵尸直到手动挂断。
        self._clcc_absent_count = 0
        self._clcc_fail_count = 0
        # EC20 NMEA PCM 上行流控：默认允许发送，仅在收到 +QPCMV:0,0 时暂停。
        self._pcm_ready_event = threading.Event()
        self._pcm_ready_event.set()

    def connect(self) -> None:
        self._open_serial()
        self._emit_connection_state(True)
        self._emit_current_sim_identity()
        logger.info("模组已连接: %s", self._active_port)

    @property
    def vendor(self) -> str:
        return "quectel"

    def refresh_hardware_info(self) -> None:
        for attr, command in (("model", "AT+CGMM"), ("firmware", "AT+CGMR")):
            value = ""
            try:
                response = self._write_command(command)
                if "OK" in response and "ERROR" not in response:
                    prefix = command.removeprefix("AT") + ":"
                    lines = []
                    for line in response.splitlines():
                        line = line.strip()
                        if line.startswith(prefix):
                            line = line.removeprefix(prefix).strip()
                        elif line in {command, "OK"} or line.startswith("+"):
                            continue
                        if line:
                            lines.append(line)
                    value = " ".join(lines)
            except Exception:  # noqa: BLE001
                pass
            setattr(self, attr, value)

    def poll_signal(self) -> None:
        interval = config.get_int("MODEM_SIGNAL_POLL_SECONDS")
        if interval <= 0 or time.monotonic() - self._last_signal_poll < interval:
            return

        def poll() -> None:
            with self._serial_lock:
                if (not self.is_connected() or self.recovery_in_progress or self._opening
                        or self._hardware_call_present or self._call_connected_event.is_set()):
                    return
                self._last_signal_poll = time.monotonic()
                try:
                    # 可选观测失败不触发重连，更不能重发并干扰通话链路。
                    csq, level = parse_csq(self._write_command("AT+CSQ"))
                    if csq is not None:
                        self.signal = {"csq": csq, "level": level, "updated_at": time.time()}
                        # 通话中暂停 CSQ，传输短暂重连也保留最近成功样本的原始时间。
                        self.last_signal = self.signal.copy()
                except Exception:  # noqa: BLE001
                    pass
        if self.run_idle_hardware is not None:
            self.run_idle_hardware(poll)
        else:
            poll()

    def reread_sim(self) -> None:
        """只重启未就绪的 SIM；不重试 CFUN，防止重枚举后再次重启。"""
        with self._serial_lock:
            if not self.is_connected() or self.recovery_in_progress:
                raise ValueError("modem_unavailable")
            if self.sim_identity.present:
                raise ValueError("sim_already_ready")
            calls = self._write_command("AT+CLCC")
            if "OK" not in calls or parse_clcc_lines(calls) or self._call_connected_event.is_set():
                raise ValueError("line_busy")
            try:
                response = self._write_command("AT+CFUN=1,1")
                if "ERROR" in response:
                    raise ValueError("sim_reset_rejected")
            except (serial.SerialException, OSError):
                # USB 可能先于 OK 消失；交回现有重连线程确认结果，不重发。
                pass
            self._sim_reread_until = time.monotonic() + 8
            self._maintenance_generation += 1
            with self._sim_refresh_lock:
                self._sim_refresh_generation += 1
                self._sim_refresh_requested_generation = None
            self._buffer = ""
            self._set_sim_identity(UNKNOWN_SIM, notify=False)
            self.signal = {"csq": None, "level": "unknown", "updated_at": None}
            self.last_signal = self.signal.copy()
            try:
                if self._ser:
                    self._ser.close()
            finally:
                self._ser = None
        self._emit_connection_state(False)

    def send_command(self, command: str) -> str:
        """发送一条原始 AT 指令，返回模组原始响应文本（最底层原子能力）。

        供诊断/示例脚本做任意 AT 交互（如 ``AT+CSQ`` 查信号、``AT+COPS?`` 查
        网络、``AT+CPIN?`` 查 SIM）。与拨号/短信等共用串口锁，可与监听并发安全调用。
        """
        return self._send(command)

    # 非 owner 等待他方重连的上限。必须有界：调用方可能持有 _serial_lock，
    # 而重连方需要该锁，无限等待会死锁（WIL-111 真机实证）。
    _RECONNECT_WAIT_TIMEOUT = 8.0

    # SIM 上电后 CIMI 可能短暂 ERROR(卡未 ready);重试覆盖上电延迟。
    _SIM_READ_RETRIES = 3
    _SIM_READ_RETRY_DELAY = 1.0

    def refresh_sim_identity(
        self,
        *,
        notify: bool = True,
        expected_generation: int | None = None,
    ) -> None:
        """读 CIMI/CREG/CEREG 刷新 SIM 身份缓存(#88)。CIMI/CREG 传输故障上抛,
        可选 CEREG 失败只降级 EPS 为未知。connect/重连(``_open_serial``,已持串口锁)自动调用,
        换卡经拔插重连天然刷新;也可被上层主动调用重刷。

        CS/EPS 注册状态后续分别跟随 +CREG/+CEREG URC 更新;支持 +QSIMSTAT 的
        模组也会在插卡时刷新身份。不支持相应 URC 的固件、不产生拔插通知的热换 SIM
        仍需重连或主动刷新。CIMI 上电延迟已由本方法内短重试覆盖。
        """
        imsi_raw = ""
        for attempt in range(self._SIM_READ_RETRIES):
            try:
                imsi_raw = self._send("AT+CIMI")
            except (serial.SerialException, OSError):
                # 传输层故障必须上抛给 _open_serial 的退避循环——不能吞成"识别失败"
                # 而让重连误判成功(codex #88 review BLOCK-3)。
                self._set_sim_identity(UNKNOWN_SIM, notify=notify)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("SIM CIMI 读取异常: %s", type(exc).__name__)
                imsi_raw = ""
            if identify(imsi_raw).present:
                break  # 卡已 ready,不必再等
            if attempt < self._SIM_READ_RETRIES - 1:
                time.sleep(self._SIM_READ_RETRY_DELAY)  # 卡未 ready,等上电
        try:
            creg_raw = self._send("AT+CREG?")
        except (serial.SerialException, OSError):
            self._set_sim_identity(UNKNOWN_SIM, notify=notify)
            raise
        except Exception:  # noqa: BLE001
            creg_raw = ""
        # VoLTE-only 卡可能 CS 被拒而 LTE 已注册;不支持 CEREG 的固件沿用 CS 结果。
        try:
            cereg_raw = self._send("AT+CEREG?")
        except Exception as exc:  # noqa: BLE001
            logger.debug("AT+CEREG? 读取失败: %s", type(exc).__name__)
            cereg_raw = ""
        if (
            expected_generation is not None
            and expected_generation != self._sim_refresh_generation
        ):
            return
        # 覆盖在缓存点统一施加：下游（dial_guard 误拨保护、结果校验、
        # /api/meta 展示）拿到的都是同一份「有效身份」，不必各自处理配置。
        self._set_sim_identity(
            with_service_number_override(
                identify(imsi_raw, creg_raw, cereg_raw),
                config.get_str("CARRIER_HOTLINE"),
            ),
            notify=notify,
        )
        sim = self._sim_identity
        if sim.present:
            logger.info(
                "SIM 识别: %s (PLMN %s) → 免费客服 %s | 网络: CS=%s LTE=%s",
                sim.carrier, sim.plmn, sim.service_number or "?", sim.reg_status, sim.eps_status,
            )
        else:
            logger.warning("SIM 识别失败(未插卡/未就绪): %s", sim.reg_status)

    @property
    def sim_identity(self) -> SimIdentity:
        """最近一次连接/重连时读到的 SIM 身份(缓存,不触发 AT)。"""
        return self._sim_identity

    def _set_sim_identity(self, identity: SimIdentity, *, notify: bool = True) -> None:
        callback: Callable[[SimIdentity], None] | None = None
        with self._sim_state_lock:
            previous = self._sim_identity
            self._sim_identity = identity
            if notify and (
                identity != previous or identity != self._last_notified_sim_identity
            ):
                self._last_notified_sim_identity = identity
                callback = self._on_sim_identity
        if callback is not None:
            try:
                callback(identity)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SIM 状态回调异常: error_type=%s", type(exc).__name__)

    def _emit_current_sim_identity(self) -> None:
        self._set_sim_identity(self._sim_identity)

    def _resolve_port(self) -> str:
        """把 auto 哨兵解析为实际串口；每次打开都重扫（Windows 重插后 COM 号会变）。

        探测不到时抛连接异常，交给 supervisor/_reconnect 的退避重试，
        设备后插也能连上。
        """
        if self.port != platforms.AUTO_PORT:
            return self.port
        detected = (port_detect.detect_at_port(vid=modem_hardware.SIM7600_VID)
                    if self.vendor == "simcom" else port_detect.detect_at_port())
        if detected is None:
            raise serial.SerialException("MODEM_PORT=auto 未探测到 Quectel AT 串口")
        return detected

    def _open_serial(self) -> None:
        """打开串口并跑初始化序列（connect 与断线重连共用）。"""
        with self._serial_lock:
            if self.transport_paused or (self.recovery_in_progress and self._closed):
                raise serial.SerialException("SIM7600 模组重启中，暂停打开串口")
            self._opening = True
            try:
                self._active_port = self._resolve_port()
                self._ser = serial.Serial(
                    port=self._active_port,
                    baudrate=self.baudrate,
                    timeout=0.2,
                    write_timeout=2,
                )
                time.sleep(0.5)
                self._drain()
                self._send("AT")
                self._send("ATE0")
                # 型号/固件与信号轮询共用硬件展示开关，默认关闭时保持原连接指令序列。
                if config.get_int("MODEM_SIGNAL_POLL_SECONDS") > 0:
                    self.refresh_hardware_info()
                self._send("AT+CLIP=1")
                self._init_sms()
                self._send("AT+QSIMSTAT=1")
                self._send("AT+CREG=1")
                self._send("AT+CEREG=1")
                self.refresh_sim_identity(notify=False)
                self._init_answer_greeting()
            finally:
                self._opening = False

    def _init_answer_greeting(self) -> None:
        """Vendor hook; Quectel keeps its existing initialization sequence."""

    def _reconnect(self) -> None:
        """串口断连后重开（USB→PTY 桥重插会换新的 /dev/ttys，需重开才能拿到新 fd）。

        多线程可能同时触发，只让第一个真正重连，其余等它完成后返回。锁序固定为
        ``_serial_lock`` → ``_reconnect_lock``，与拨号/短信/挂断事务一致，避免
        发送线程持串口锁等待重连锁、读线程反向持锁形成 ABBA 死锁。
        带指数退避重试，直到成功或服务停止。
        """
        observed_generation = self._reconnect_generation
        owner = False
        with self._serial_lock:
            if self.recovery_in_progress:
                return
            if observed_generation != self._reconnect_generation:
                return
            with self._reconnect_lock:
                if observed_generation != self._reconnect_generation:
                    return
                if not self._reconnect_in_progress:
                    self._reconnect_in_progress = True
                    self._reconnect_complete.clear()
                    with self._sim_refresh_lock:
                        self._sim_refresh_generation += 1
                        self._sim_refresh_requested_generation = None
                    try:
                        if self._ser and self._ser.is_open:
                            self._ser.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._ser = None
                    self._buffer = ""
                    owner = True

        if not owner:
            # 等别人重连完成，但**必须有界**：调用方可能正持有 _serial_lock（如
            # hangup() 为保证挂断原子性持锁，锁内 _send 失败后走到这里），而真正的
            # 重连方需要这把锁才能开串口——无限等待即形成循环等待死锁。真机
            # 2026-08-12（WIL-111）用 faulthandler 抓到：挂断线程持锁等重连、
            # 读线程等锁开串口，全部线程僵死，进程活着却再也接不了电话。
            # 超时返回后本次 _send 会失败上抛，由调用方各自的重试路径兜底。
            deadline = time.monotonic() + self._RECONNECT_WAIT_TIMEOUT
            while self._running and not self._closed:
                if self._reconnect_complete.wait(timeout=0.2):
                    return
                if time.monotonic() >= deadline:
                    logger.warning(
                        "等待他方重连超过 %.0fs，本次放弃（避免持锁死等）",
                        self._RECONNECT_WAIT_TIMEOUT,
                    )
                    return
            return

        self._emit_connection_state(False)
        delay = 1.0
        try:
            while self._running and not self._closed:
                if self.recovery_in_progress:
                    return
                try:
                    self._open_serial()
                    with self._serial_lock:
                        self._reconnect_generation += 1
                    self._emit_connection_state(True)
                    self._emit_current_sim_identity()
                    logger.info("串口已重连: %s", self._active_port)
                    return
                # 捕全部异常而不只是 (SerialException, OSError)：_open_serial 内部的
                # _send 在 _opening=True 时会上抛 RuntimeError("模组未连接")，早先它
                # 会穿透这里、打死调用方线程（真机 2026-08-12：读线程死亡后桥虽已自愈，
                # app 却永远连不回来，来电无人接听，见 WIL-110）。重连必须是「只要没被
                # 停止就一直退避重试」。
                except Exception as exc:  # noqa: BLE001
                    with self._serial_lock:
                        try:
                            if self._ser and self._ser.is_open:
                                self._ser.close()
                        except Exception:  # noqa: BLE001
                            pass
                        self._ser = None
                    logger.warning("串口重连失败，%.0fs 后重试: %s", delay, exc)
                    time.sleep(delay)
                    delay = min(delay * 2, 10.0)
        finally:
            with self._reconnect_lock:
                self._reconnect_in_progress = False
                self._reconnect_complete.set()

    def _init_sms(self) -> None:
        """开启短信文本模式并让模组主动上报新短信 (+CMTI)。"""
        self._send("AT+CMGF=1")
        self._send('AT+CPMS="SM","SM","SM"')
        self._send("AT+CNMI=2,1,0,0,0")
        logger.info("短信接收已启用 (文本模式, +CMTI 上报)")
        self._dump_stored_sms()

    def _dump_stored_sms(self) -> None:
        """启动时读取模组/SIM 已存短信并补收（走 on_sms）。

        +CMTI 实时上报只覆盖服务运行期间新到达的短信；服务重启间隙、或
        CNMI 上报被打断而遗漏的短信，会一直躺在 SIM 里进不了 app。启动时
        全量读一遍补收，重复的靠 EventHub 短信指纹去重（sender+时间戳+正文），
        不会重复入库或重复转发邮件。
        """
        try:
            response = self._send('AT+CMGL="ALL"')
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取已存短信失败: error_type=%s", type(exc).__name__)
            return

        entries = self._parse_cmgl(response)
        if not entries:
            logger.info("模组内暂无已存短信")
            return
        logger.info("模组内已存短信 %d 条，补收（重复自动去重）", len(entries))
        delete_after = config.get_bool("SMS_DELETE_AFTER_INGEST")
        for index, sender, timestamp, body in entries:
            logger.info(
                "[补收短信] 来自=%s 时间=%s 字符数=%d", sender or "未知", timestamp, len(body)
            )
            delivered = False
            if self._on_sms:
                try:
                    from .notification_events import sms_backfill

                    with sms_backfill():
                        self._on_sms(sender, body, timestamp)
                    delivered = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("补收短信回调异常: error_type=%s", type(exc).__name__)
            # 补收进 app（含去重跳过=已在 app）后删 SIM 上这条，腾存储防满。
            if delivered and index and delete_after:
                self._delete_stored_sms(index)

    def _delete_stored_sms(self, index: str) -> None:
        """删除 SIM/模组存储位上的一条短信（AT+CMGD）；失败只告警不影响流程。

        SIM 短信存储容量有限（常 ~20-50 条），满了新短信收不进来。短信补收/
        实时读入 app 后即删，保证存储不堆满、+CMTI 实时上报持续可用。
        """
        try:
            self._send(f"AT+CMGD={index}")
            logger.debug("已删除 SIM 存储短信 index=%s", index)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除已存短信失败 (index=%s): error_type=%s", index, type(exc).__name__)

    def _parse_cmgl(self, response: str) -> list[tuple[str, str | None, str, str]]:
        """解析 ``AT+CMGL`` 响应为 ``(存储索引, 发件方, 时间戳, 正文)`` 列表。

        索引供补收后 ``AT+CMGD`` 删除该条、腾出 SIM 存储（防满导致新短信收不到）。
        """
        lines = response.splitlines()
        entries: list[tuple[str, str | None, str, str]] = []
        idx = 0
        while idx < len(lines):
            if not lines[idx].strip().startswith("+CMGL:"):
                idx += 1
                continue
            header_line = lines[idx]
            index_match = re.match(r"\+CMGL:\s*(\d+)", header_line.strip())
            index = index_match.group(1) if index_match else ""
            body_lines: list[str] = []
            idx += 1
            while idx < len(lines):
                stripped = lines[idx].strip()
                if stripped.startswith("+CMGL:") or stripped in ("OK", "ERROR"):
                    break
                body_lines.append(lines[idx])
                idx += 1
            raw_body = "\n".join(body_lines).strip()
            sender, timestamp, body = self._interpret_sms(header_line, raw_body)
            entries.append((index, sender, timestamp, body))
        return entries

    # 接听后到启用语音通道之间的静置时间（厂商可覆写）。历史上写死 1.0s，
    # 来历不明（仓库重构时带入，无注释）；对自带重试的实现是纯浪费——健康态
    # 「接听→启用」总共才 2~3s，这一秒占三分之一（WIL-104）。
    POST_ANSWER_SETTLE_SECONDS = 1.0

    # 最近一次 initialize_for_voice 的结果快照，供调用方（call_agent）在通话内
    # 读取并落 pcm_enable 事件（WIL-95 D 组：无声通/劣化通的通级指纹）。
    # 只有会失败/会重试的实现（SIM7600 CPCMREG）覆写；Quectel 路径无该信号，
    # 保持 None = 诚实的「未埋点」，metrics 侧记 unavailable 而非假 0。
    last_voice_enable: dict[str, Any] | None = None

    def initialize_for_voice(self, audio_mode: str = "uac") -> None:
        """启用语音 PCM 通道（厂商钩子，子类按各自 AT 指令实现）。"""
        raise NotImplementedError("initialize_for_voice 需由厂商子类实现")

    def on_ring(self, callback: Callable[[str | None], None]) -> None:
        self._on_ring = callback

    def on_hangup(self, callback: Callable[[], None]) -> None:
        self._on_hangup = callback

    def on_sms(self, callback: Callable[[str | None, str, str], None]) -> None:
        """注册收件短信回调，参数 (发件方, 正文, 短信时间戳)。时间戳供去重。"""
        self._on_sms = callback

    def on_call_connected(self, callback: Callable[[str | None], None]) -> None:
        self._on_call_connected = callback

    def on_connection_state(self, callback: Callable[[bool], None]) -> None:
        """Register for initialized serial transport transitions."""
        self._on_connection_state = callback
        if self._connection_callback_thread is None:
            self._connection_callback_thread = threading.Thread(
                target=self._connection_callback_loop,
                name="modem-state-callback",
                daemon=True,
            )
            self._connection_callback_thread.start()

    def on_sim_identity(self, callback: Callable[[SimIdentity], None]) -> None:
        """Register for privacy-safe cached SIM identity transitions."""
        self._on_sim_identity = callback

    def _emit_connection_state(self, online: bool) -> None:
        if not online:
            # 断线后旧 CLCC 不再代表占线；是否仍有会话由服务的占线锁判断。
            self._hardware_call_present = False
            self.signal = {"csq": None, "level": "unknown", "updated_at": None}
        recovery_seconds: float | None = None
        now = time.time()
        with self._connection_state_lock:
            if online == self._connection_online:
                return
            self._connection_online = online
            if online:
                if self._connection_disconnected_at is not None:
                    recovery_seconds = max(0.0, now - self._connection_disconnected_at)
                self._connection_disconnected_at = None
            else:
                self._connection_disconnected_at = now
        if online:
            if recovery_seconds is None:
                logger.info("模组传输已就绪")
            else:
                logger.info("模组传输已恢复: outage_seconds=%.3f", recovery_seconds)
        else:
            logger.warning("模组传输已断开: disconnected_at=%.3f", now)
        if self._on_connection_state is not None:
            self._connection_callback_queue.put(online)

    def _connection_callback_loop(self) -> None:
        while True:
            online = self._connection_callback_queue.get()
            if online is None:
                return
            callback = self._on_connection_state
            if callback is None:
                continue
            try:
                callback(online)
            except Exception as exc:  # noqa: BLE001
                logger.warning("模组连接状态回调异常: error_type=%s", type(exc).__name__)

    def start_listener(self) -> None:
        if self._closed:
            return
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._poll_thread = threading.Thread(target=self._poll_call_status, daemon=True)
        self._poll_thread.start()

    def stop_listener(self) -> None:
        self._running = False
        self._poll_wakeup.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
        if self._poll_thread:
            self._poll_thread.join(timeout=2)

    def answer(self) -> None:
        self._start_fast_clcc_poll()
        if config.get_bool("MODEM_LOG_RELEASE_CAUSE"):
            with self._serial_lock:
                self._start_release_call()
                self._send("ATA")
        else:
            self._send("ATA")
        # 置「通话在线」：让来电同样受 CLCC 消失判定保护（串口死亡场景）。
        self._call_connected_event.set()
        logger.info("已发送 ATA 接听来电")

    def dial(self, number: str, *, retry: bool = True, deadline: float | None = None) -> str:
        """拨打语音电话（ATD<number>; 末尾分号表示语音呼叫）。"""
        number = (number or "").strip()
        if not number:
            raise ValueError("拨号号码为空")
        # 清接通状态与发拨号命令须原子：否则 CLCC 轮询线程可能在两次 clear
        # 之间 set 事件/加 call_id，导致 _wait_connected 永远等不到接通而误判未接。
        # _serial_lock 是 RLock，_send 内部再取同锁可重入。
        with self._serial_lock:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("拨号期限已过，未发出 ATD")
            if config.get_bool("MODEM_LOG_RELEASE_CAUSE"):
                self._start_release_call()
            self._call_connected_event.clear()
            self._connected_call_ids.clear()
            self._clcc_absent_count = 0
            self._clcc_fail_count = 0
            self._last_dialed = number
            self._start_fast_clcc_poll()
            # 自测禁止串口重连后的隐式重拨；普通通话沿用原有重试。
            response = self._send(f"ATD{number};") if retry else self._write_command(f"ATD{number};")
        logger.info("已拨号 -> %s", number)
        return response

    def is_call_connected(self) -> bool:
        return self._call_connected_event.is_set()

    # ---- 三方通话原语（WIL-120 四期 Path B）----
    # 3GPP TS 22.030 补充业务标准指令，非厂商专有。⚠️ 真机未验证：固件是否
    # 完整实现、运营商线路是否开通多方通话业务，都要先跑 scripts/chld_probe.py
    # 探针确认（AT+CHLD=? 能力查询 + 通话中 hold/resume 实测），探针通过前
    # 这些方法不得接入生产链路（「我来说」按钮保持置灰）。

    def query_chld_capabilities(self) -> str:
        """查询模组支持的 CHLD 操作集（``AT+CHLD=?``，空闲态安全）。"""
        return self._send("AT+CHLD=?")

    def hold_toggle(self) -> bool:
        """``AT+CHLD=2``：保持当前通话 / 在保持与恢复间切换。

        标准语义是「把 active 置 held、把 held 置 active」——只有一路通话时
        即 hold/resume 切换。返回模组是否应答 OK；真实状态以 list_calls 为准
        （运营商侧可能应答 OK 但业务未开通，探针要抓的正是这种）。
        """
        return "OK" in self._send("AT+CHLD=2")

    def merge_calls(self) -> bool:
        """``AT+CHLD=3``：把保持中的通话并入当前通话（多方通话）。

        前提：一路 active + 一路 held。网络侧混音，音频仍是一路 PCM，
        宿主侧无需任何改动。返回是否应答 OK。
        """
        return "OK" in self._send("AT+CHLD=3")

    def dial_second(self, number: str) -> str:
        """通话中拨第二路（Path B：拨机主第二号码）。

        调用方须**先** hold_toggle() 保持第一路再拨；本方法刻意不复用
        ``dial()``——那里会清接通状态/CLCC 计数，第二路拨号不能扰动
        第一路的在线判定。
        """
        number = (number or "").strip()
        if not number:
            raise ValueError("第二路号码为空")
        return self._send(f"ATD{number};")

    def list_calls(self) -> list[dict[str, object]]:
        """``AT+CLCC`` 列出当前所有通话；解析失败的行跳过不猜。

        返回项：``{"index", "direction", "state", "multiparty", "number"}``；
        state 语义（3GPP）：active/held/dialing/alerting/incoming/waiting。
        """
        return parse_clcc_lines(self._send("AT+CLCC"))

    def send_dtmf(self, digits: str) -> bool:
        """通话中发送 DTMF 按键音（AT+QVTS），用于 IVR 菜单导航。

        digits 允许 0-9、*、#、A-D；逐位发送（模组一次一音更可靠）。
        任一位失败即返回 False（已发出的按键无法撤回）。
        """
        digits = (digits or "").strip().upper()
        if not digits:
            return False
        valid = set("0123456789*#ABCD")
        if any(ch not in valid for ch in digits):
            logger.warning("DTMF 输入无效: count=%d, result=failure", len(digits))
            return False
        send_seconds = 0.0
        for ch in digits:
            started = time.monotonic()
            ok = self._send_dtmf_digit(ch)
            send_seconds += time.monotonic() - started
            if not ok:
                logger.warning("DTMF 发送失败: count=%d, result=failure", len(digits))
                return False
            time.sleep(0.15)  # 位间间隔，防止连音被 IVR 吞掉
        logger.info(
            "DTMF 发送完成: count=%d, result=success, send_seconds=%.3f",
            len(digits), send_seconds,
        )
        return True

    def _send_dtmf_digit(self, ch: str) -> bool:
        """发送单个 DTMF 按键（厂商钩子，子类按各自 AT 指令实现）；成功返回 True。"""
        raise NotImplementedError("_send_dtmf_digit 需由厂商子类实现")

    def _disable_voice_channel(self) -> None:
        """挂断时关闭语音 PCM 通道（厂商钩子，子类按各自 AT 指令实现）。"""
        raise NotImplementedError("_disable_voice_channel 需由厂商子类实现")

    def _hangup_command(self) -> str:
        """挂断用的 AT 指令（厂商钩子）；默认 ``ATH``。语音挂断不认 ATH 的模组覆写。"""
        return "ATH"

    def hangup(self) -> None:
        report = self._release_report if config.get_bool("MODEM_LOG_RELEASE_CAUSE") else None
        # 仅在已成功挂断且诊断进行中去重；查询后仍允许原有的挂断重试。
        if report is not None and report.hung_up and report.requested and not report.done.is_set():
            return
        # 挂断与关闭语音通道及状态清理须原子：否则 CLCC 轮询线程可能插进挂断与
        # 关闭语音通道之间，扰乱指令/响应配对。_pcm_ready_event.set() 只置位
        # 不等待，持锁调用无死锁风险。
        with self._serial_lock:
            response = self._send(self._hangup_command())
            self._disable_voice_channel()
            self._pcm_ready_event.set()
            self._call_connected_event.clear()
            self._connected_call_ids.clear()
            self._fast_poll_until = 0.0
            self._clcc_absent_count = 0
            self._clcc_fail_count = 0
            if report is not None:
                report.hung_up = "OK" in response.split()
        logger.info("已挂断并关闭语音 PCM 通道")
        self._request_release_cause()

    def begin_release_capture(self) -> _ReleaseReport:
        """会话拥有物理收尾时，先固定本通快照，禁止 URC 抢先查询。"""
        with self._serial_lock:
            self._release_report = _ReleaseReport(deferred=True)
            return self._release_report

    def _start_release_call(self) -> None:
        if not self._release_report.deferred or self._release_report.started:
            self._release_report = _ReleaseReport()
        self._release_report.started = True

    def complete_release_capture(self, report: _ReleaseReport) -> list[dict[str, str]]:
        """物理收尾完成后才允许查询；调用者持有本通句柄，不读取下一通状态。"""
        with report.lock:
            report.deferred = False
        if not config.get_bool("MODEM_LOG_RELEASE_CAUSE"):
            if not report.requested:
                self._run_release_callback(report)
            return []
        if report.pending:
            self._request_release_cause(report)
        return self.wait_release_causes(report)

    def _capture_cend(self, text: str, report: _ReleaseReport | None = None) -> None:
        if not config.get_bool("MODEM_LOG_RELEASE_CAUSE"):
            return
        report = report or self._release_report
        for match in CEND_PATTERN.finditer(text):
            raw = match.group(0)
            fields = raw.partition(":")[2].split(",")
            if len(fields) < 4 or not fields[3].strip():
                continue
            cause = {"raw": raw, "source": "cend", "cause": fields[3].strip()}
            with report.lock:
                if cause in report.causes:
                    continue
                report.causes.append(cause)
            logger.info("通话释放原因: %s", raw)

    def _request_release_cause(self, report: _ReleaseReport | None = None) -> None:
        if not config.get_bool("MODEM_LOG_RELEASE_CAUSE"):
            if report is not None and not report.requested:
                report.done.set()
                self._run_release_callback(report)
            return
        report = report or self._release_report
        with report.lock:
            report.pending = True
            if report.requested or report.deferred:
                return
            report.requested = True
        # 没有传输就没有可查询的释放报告，也不应延后原有重启回调。
        if self._ser is None:
            report.done.set()
            self._run_release_callback(report)
            return
        deadline = time.monotonic() + 2.0
        threading.Thread(
            target=self._query_release_cause, args=(report, deadline),
            name="modem-release-cause", daemon=True,
        ).start()

    def _query_release_cause(self, report: _ReleaseReport, deadline: float) -> None:
        """复用 AT 事务锁；锁等待、写入与读取共享 2 秒预算，不重连、不重试。"""
        acquired = False
        response = ""
        try:
            acquired = self._serial_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
            if (not acquired or report is not self._release_report or self._closed
                    or not config.get_bool("MODEM_LOG_RELEASE_CAUSE")
                    or (self.transport_paused and not report.before_reset)):
                return
            ser = self._ser
            remaining = deadline - time.monotonic()
            if ser is None or not ser.is_open or remaining <= 0:
                return
            # 查询期间缩短串口超时，避免单次空读或阻塞写突破总期限。
            old_timeout = getattr(ser, "timeout", 0.2)
            old_write_timeout = getattr(ser, "write_timeout", 2.0)
            try:
                ser.write_timeout = remaining
                ser.write(b"AT+CEER\r")
                chunks: list[str] = []
                while (remaining := deadline - time.monotonic()) > 0:
                    ser.timeout = min(0.05, remaining)
                    raw = ser.read(ser.in_waiting or 1)
                    if not raw:
                        time.sleep(min(0.005, remaining))
                        continue
                    chunks.append(raw.decode("ascii", errors="replace"))
                    response = "".join(chunks)
                    if re.search(r"(?:^|\r?\n)(?:OK|ERROR|\+CME ERROR:[^\r\n]*)(?:\r?\n)", response):
                        break
                response = "".join(chunks)
            finally:
                ser.timeout, ser.write_timeout = old_timeout, old_write_timeout
            if response:
                self._capture_cend(response, report)
                self._scan_pcm_flow(response)
                with report.lock:
                    report.causes.append({"raw": response, "source": "ceer"})
                logger.info("通话释放原因: %s", response)
            else:
                logger.info("通话释放原因查询超时（AT+CEER 无返回）")
        except (serial.SerialException, OSError, RuntimeError) as exc:
            logger.warning("通话释放原因查询失败: %s", exc)
        finally:
            if acquired:
                self._serial_lock.release()
            report.done.set()
            try:
                # 短信 URC 可能继续做串口 IO，必须在诊断预算和完成信号之外处理。
                sms_urcs = "\r\n".join(match.group(0) for match in CMTI_PATTERN.finditer(response))
                if sms_urcs:
                    self._process_response_urcs(sms_urcs, report)
            except Exception as exc:  # noqa: BLE001
                logger.warning("处理释放报告中的短信通知失败: %s", exc)
            finally:
                self._run_release_callback(report)

    @staticmethod
    def _run_release_callback(report: _ReleaseReport) -> None:
        # 热关闭诊断、查询结束和记录收尾可能竞争；重启回调只能结算一次。
        with report.lock:
            callback, report.after_query = report.after_query, None
        if callback is not None:
            callback()

    def wait_release_causes(self, report: _ReleaseReport | None = None) -> list[dict[str, str]]:
        """仅供挂断后的记录收尾线程等待，不在读循环或挂断事务中调用。"""
        if not config.get_bool("MODEM_LOG_RELEASE_CAUSE"):
            return []
        report = report or self._release_report
        if report.requested:
            report.done.wait(2.0)
        with report.lock:
            return [cause.copy() for cause in report.causes]

    def close(self) -> None:
        self._closed = True  # 终态：阻止后续 start_listener/_reconnect 复活
        with self._sim_refresh_lock:
            self._sim_refresh_generation += 1
            self._sim_refresh_requested_generation = None
        self._reconnect_complete.set()
        self._connection_callback_queue.put(None)
        self.stop_listener()
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def send_sms(self, number: str, text: str) -> bool:
        """发送短信。中文自动走 UCS2 编码，英文/数字走普通 GSM 文本。"""
        if not self._ser:
            raise RuntimeError("模组未连接")
        number = (number or "").strip()
        if not number:
            logger.warning("发送短信失败: 号码为空")
            return False

        use_ucs2 = not text.isascii()
        with self._serial_lock:
            try:
                self._send("AT+CMGF=1")
                if use_ucs2:
                    self._send('AT+CSCS="UCS2"')
                    self._send("AT+CSMP=17,167,0,8")
                    addr = number.encode("utf-16-be").hex().upper()
                    payload = text.encode("utf-16-be").hex().upper()
                else:
                    self._send('AT+CSCS="GSM"')
                    self._send("AT+CSMP=17,167,0,0")
                    addr = number
                    payload = text
                ok = self._send_sms_payload(addr, payload)
            finally:
                # 恢复默认字符集，避免影响来电号码解析。
                self._send('AT+CSCS="GSM"')

        if ok:
            logger.info("短信已发送 -> %s 字符数=%d", number, len(text))
        else:
            logger.warning("短信发送失败 -> %s", number)
        return ok

    def _send_sms_payload(self, addr: str, payload: str) -> bool:
        assert self._ser is not None
        self._ser.reset_input_buffer()
        self._ser.write(f'AT+CMGS="{addr}"\r'.encode("ascii"))
        if not self._wait_for_prompt(timeout=5):
            logger.warning("未收到短信输入提示符 '>'")
            self._ser.write(b"\x1b")  # ESC 取消
            return False
        self._ser.write(payload.encode("ascii") + b"\x1a")  # 正文 + Ctrl-Z
        response = self._read_response(timeout=12)
        return "+CMGS:" in response or "OK" in response

    def _wait_for_prompt(self, timeout: float = 5) -> bool:
        assert self._ser is not None
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            raw = self._ser.read(self._ser.in_waiting or 1)
            if raw:
                buf += raw.decode("ascii", errors="ignore")
                if ">" in buf:
                    return True
                if "ERROR" in buf:
                    return False
        return False

    def _send(self, cmd: str) -> str:
        if self.transport_paused:
            raise serial.SerialException("SIM7600 模组重启中，暂停 AT")
        try:
            return self._write_command(cmd)
        except (serial.SerialException, OSError, RuntimeError) as exc:
            # 初始化序列中失败不自触发重连（由 _reconnect 的重试循环兜底），避免死锁。
            if self._opening or self.recovery_in_progress:
                raise
            logger.warning("串口发送失败，尝试重连后重试: %s", exc)
            self._reconnect()
            return self._write_command(cmd)

    def _write_command(self, cmd: str) -> str:
        started = time.monotonic()
        with self._serial_lock:
            wait_seconds = time.monotonic() - started
            if wait_seconds > 0.2:
                # Extract only an AT verb, never dialed numbers, DTMF or SMS payloads.
                command_name = re.match(r"AT(?:[+&^%][A-Z]+|[A-Z])?", cmd.strip().upper())
                logger.info(
                    "[timing] 串口锁等待 %.3fs cmd=%s",
                    wait_seconds, command_name.group(0) if command_name else "unknown",
                )
            if self.transport_paused:
                raise serial.SerialException("SIM7600 模组重启中，暂停 AT")
            if not self._ser or not self._ser.is_open:
                raise RuntimeError("模组未连接")
            line = cmd if cmd.endswith("\r") else f"{cmd}\r"
            self._ser.write(line.encode("ascii"))
            response = self._read_response(timeout=3)
            if not cmd.strip().upper().startswith("AT+CLCC"):
                self._process_response_urcs(response)
            return response

    def _read_response(self, timeout: float = 2) -> str:
        if not self._ser:
            return ""
        deadline = time.time() + timeout
        chunks: list[str] = []
        while time.time() < deadline:
            raw = self._ser.read(self._ser.in_waiting or 1)
            if not raw:
                continue
            chunks.append(raw.decode("ascii", errors="ignore"))
            joined = "".join(chunks)
            if "OK" in joined or "ERROR" in joined:
                break
        return "".join(chunks)

    def _drain(self) -> None:
        if self._ser:
            self._ser.reset_input_buffer()

    def _read_loop(self) -> None:
        while self._running:
            if self.transport_paused:
                time.sleep(0.2)
                continue
            try:
                with self._serial_lock:
                    if self.transport_paused:
                        continue
                    generation = self._maintenance_generation
                    if not self._ser or not self._ser.is_open:
                        raise serial.SerialException("串口未打开")
                    # Keep the availability check and read atomic with command responses;
                    # never hold the lock waiting for a byte that has not arrived yet.
                    waiting = self._ser.in_waiting
                    raw = self._ser.read(waiting) if waiting else b""
            except (serial.SerialException, OSError) as exc:
                if not self._running:
                    break
                logger.warning("串口读取失败，尝试重连: %s", exc)
                try:
                    self._reconnect()
                except Exception as reconnect_exc:  # noqa: BLE001
                    # 读线程是来电感知的命脉：它一死，模组即便恢复也再没人读 URC，
                    # 电话打进来无人接听（WIL-110）。任何意外都不许让它退出。
                    logger.error(
                        "重连过程异常，读线程继续重试: %s", reconnect_exc
                    )
                    time.sleep(1.0)
                continue
            except Exception as exc:  # noqa: BLE001
                if not self._running:
                    break
                logger.error("读线程意外异常，继续运行: %s", exc)
                time.sleep(1.0)
                continue
            # RLock is not fair: yield outside it even during continuous URC traffic.
            time.sleep(0.01)
            if not raw:
                continue
            if generation != self._maintenance_generation:
                continue
            text = raw.decode("ascii", errors="ignore")
            self._buffer += text
            self._process_buffer()

    def is_connected(self) -> bool:
        """串口传输当前是否在线（供上层看门狗判断是否需要重建连接）。"""
        with self._connection_state_lock:
            return self._connection_online

    @property
    def recovery_in_progress(self) -> bool:
        return time.monotonic() < self._sim_reread_until

    @property
    def transport_paused(self) -> bool:
        return time.monotonic() < self._sim_reread_until

    def pcm_ready(self) -> bool:
        return self._pcm_ready_event.is_set()

    def _scan_pcm_flow(self, text: str) -> None:
        """厂商带内 PCM 上行流控 URC 解析钩子；基类默认无（无带内流控的模组不覆写，
        ``_pcm_ready_event`` 保持初始置位，``pcm_ready`` 恒为 True）。"""

    def _process_response_urcs(self, text: str, report: _ReleaseReport | None = None) -> None:
        self._capture_cend(text, report)
        if "NO CARRIER" in text or CEND_PATTERN.search(text):
            self._request_release_cause(report)
        self._scan_pcm_flow(text)
        if self._processing_response_cmti:
            return
        matches = list(CMTI_PATTERN.finditer(text))
        if not matches:
            return
        self._processing_response_cmti = True
        try:
            for match in matches:
                self._read_sms(match.group(1), match.group(2))
        finally:
            self._processing_response_cmti = False

    def _process_buffer(self) -> None:
        if self.transport_paused:
            self._buffer = ""
            return
        self._scan_pcm_flow(self._buffer)
        self._process_sim_urcs()
        while True:
            clip = CLIP_PATTERN.search(self._buffer)
            if clip:
                self._last_caller = clip.group(1)

            cmti = CMTI_PATTERN.search(self._buffer)
            if cmti:
                mem, index = cmti.group(1), cmti.group(2)
                self._buffer = CMTI_PATTERN.sub("", self._buffer, count=1)
                self._read_sms(mem, index)
                continue

            if RING_PATTERN.search(self._buffer):
                if (config.get_bool("MODEM_LOG_RELEASE_CAUSE")
                        and self._release_report.requested):
                    self._release_report = _ReleaseReport()
                logger.info("检测到来电 RING, 号码=%s", self._last_caller)
                if self._on_ring:
                    self._on_ring(self._last_caller)
                self._buffer = RING_PATTERN.sub("", self._buffer, count=1)
                continue

            if "NO CARRIER" in self._buffer or "+CEND:" in self._buffer:
                partial_cend = ""
                if config.get_bool("MODEM_LOG_RELEASE_CAUSE"):
                    # 等完整 CEND 行，避免分片把最后的 cause 截掉。
                    if "+CEND:" in self._buffer and not CEND_PATTERN.search(self._buffer):
                        if "NO CARRIER" not in self._buffer:
                            return
                        partial_cend = self._buffer[self._buffer.index("+CEND:"):]
                    self._capture_cend(self._buffer)
                trigger = "NO CARRIER" if "NO CARRIER" in self._buffer else "+CEND:"
                logger.info("通话已结束 (触发=%s)", trigger)
                self._buffer = partial_cend
                # 通话结束即清来电号码：隐藏号码来电的 +CLIP 是空引号、匹配不到，
                # 不清的话下一通会把**上一通**的号码当成本通来电（转告短信会把
                # 错误的回拨号码写给机主，review finding #8）。None = 如实的未知。
                self._last_caller = None
                self._fast_poll_until = 0.0
                if self._on_hangup:
                    self._on_hangup()
                self._request_release_cause()
                return

            if len(self._buffer) > 4096:
                self._buffer = self._buffer[-1024:]
            break

    def _process_sim_urcs(self) -> None:
        """Consume SIM/registration URCs without doing serial I/O on the reader."""
        while True:
            creg = CREG_PATTERN.search(self._buffer)
            cereg = CEREG_PATTERN.search(self._buffer)
            qsim = QSIMSTAT_PATTERN.search(self._buffer)
            matches = [match for match in (creg, cereg, qsim) if match is not None]
            if not matches:
                return
            match = min(matches, key=lambda item: item.start())
            self._buffer = self._buffer[:match.start()] + self._buffer[match.end():]
            if match.re is CREG_PATTERN:
                self._set_sim_identity(with_registration(self._sim_identity, match.group(0)))
                continue
            if match.re is CEREG_PATTERN:
                self._set_sim_identity(with_eps_registration(self._sim_identity, match.group(0)))
                continue

            enabled, inserted = match.group(1), match.group(2)
            if enabled != "1":
                continue
            if inserted == "0":
                with self._sim_refresh_lock:
                    self._sim_refresh_generation += 1
                    self._sim_refresh_requested_generation = None
                self._set_sim_identity(UNKNOWN_SIM)
            elif inserted in {"1", "2"}:
                self._schedule_sim_identity_refresh()

    _SIM_REFRESH_DEBOUNCE_SECONDS = 0.3

    def _schedule_sim_identity_refresh(self) -> None:
        with self._sim_refresh_lock:
            self._sim_refresh_requested_generation = self._sim_refresh_generation
            worker = self._sim_refresh_thread
            if worker is not None and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._sim_identity_refresh_worker,
                name="sim-identity-refresh",
                daemon=True,
            )
            self._sim_refresh_thread = worker
            worker.start()

    def _sim_identity_refresh_worker(self) -> None:
        try:
            while True:
                time.sleep(self._SIM_REFRESH_DEBOUNCE_SECONDS)
                with self._sim_refresh_lock:
                    generation = self._sim_refresh_requested_generation
                    self._sim_refresh_requested_generation = None
                    if generation is None:
                        self._sim_refresh_thread = None
                        return
                if self._closed:
                    return
                if generation != self._sim_refresh_generation:
                    continue
                self.refresh_sim_identity(expected_generation=generation)
                with self._sim_refresh_lock:
                    if self._sim_refresh_requested_generation is None:
                        return
        except (serial.SerialException, OSError) as exc:
            logger.warning("SIM 热插拔刷新失败: error_type=%s", type(exc).__name__)
        finally:
            with self._sim_refresh_lock:
                if self._sim_refresh_thread is threading.current_thread():
                    self._sim_refresh_thread = None

    # 通话在线期间 CLCC 轮询连续异常达到该阈值（×2s ≈ 60s），判定串口
    # 已无法恢复、通话必然丢失，放弃等待并收尾会话（覆盖桥永久死亡场景）。
    _CLCC_FAIL_THRESHOLD = 30

    def _start_fast_clcc_poll(self) -> None:
        # 只加速接通检测，不改变 ATA 后的在线标志或来电接通状态机。
        self._fast_poll_until = time.monotonic() + 60.0
        self._poll_wakeup.set()

    def _clcc_poll_interval(self) -> float:
        return 0.25 if time.monotonic() < self._fast_poll_until else 2.0

    def _poll_call_status(self) -> None:
        while self._running:
            if self.transport_paused:
                time.sleep(0.2)
                continue
            generation = self._maintenance_generation
            self._poll_wakeup.clear()
            try:
                response = self._send("AT+CLCC")
            except Exception as exc:  # noqa: BLE001
                logger.debug("轮询 CLCC 失败: %s", exc)
                if self._call_connected_event.is_set():
                    self._clcc_fail_count += 1
                    if self._clcc_fail_count >= self._CLCC_FAIL_THRESHOLD:
                        with self._serial_lock:
                            self._clcc_fail_count = 0
                            self._call_connected_event.clear()
                            self._connected_call_ids.clear()
                            self._fast_poll_until = 0.0
                        logger.warning(
                            "串口失联超 %d 秒且通话在线，判定通话已丢失，收尾会话",
                            self._CLCC_FAIL_THRESHOLD * 2,
                        )
                        if self._on_hangup:
                            self._on_hangup()
                time.sleep(2)
                continue

            self._clcc_fail_count = 0
            if generation != self._maintenance_generation:
                continue
            self._process_clcc_response(response)
            self._hardware_call_present = bool(parse_clcc_lines(response)) or "OK" not in response
            if self._clcc_poll_interval() == 2.0:
                self.poll_signal()
            self._poll_wakeup.wait(self._clcc_poll_interval())

    def _process_clcc_response(self, response: str) -> None:
        generation = self._maintenance_generation
        if self.transport_paused:
            return
        self._process_response_urcs(response)
        seen_incoming_ids: set[str] = set()
        # 与 dial() 对 _connected_call_ids/_call_connected_event 的清除互斥：
        # 状态判定+改写在 _serial_lock 内完成，回调收集后到锁外触发（避免持锁回调）。
        pending_ring: list[str | None] = []
        pending_connected: list[str | None] = []
        call_lost = False
        with self._serial_lock:
            if self.transport_paused or generation != self._maintenance_generation:
                return
            has_call_line = False
            for match in CLCC_PATTERN.finditer(response):
                has_call_line = True
                call_id = match.group("idx")
                direction = match.group("dir")
                status = match.group("stat")
                number = match.group("number") or None
                if status == "0":
                    self._fast_poll_until = 0.0

                if direction == "1" and status == "4":
                    seen_incoming_ids.add(call_id)
                    if call_id not in self._incoming_call_ids:
                        self._incoming_call_ids.add(call_id)
                        self._last_caller = number
                        logger.info("检测到 CLCC 来电, 号码=%s", number or "未知")
                        pending_ring.append(number)

                # 外呼(dir=0)转为 active(stat=0) 即对方已接听。
                if direction == "0" and status == "0":
                    if call_id not in self._connected_call_ids:
                        self._connected_call_ids.add(call_id)
                        connected_number = number or self._last_dialed
                        self._call_connected_event.set()
                        logger.info("外呼已接通, 号码=%s", connected_number or "未知")
                        pending_connected.append(connected_number)

            self._incoming_call_ids.intersection_update(seen_incoming_ids)

            # 通话消失判定：会话认为通话在线，而一次**有效**的 CLCC（回了 OK）
            # 却没有任何通话行——正常挂断走读循环的 NO CARRIER，但串口死亡
            # 期间收不到该事件，重连后只有这里能发现通话早已结束。连续两次
            # 才判死，滤掉模组状态瞬变。
            if self._call_connected_event.is_set() and "OK" in response:
                if has_call_line:
                    self._clcc_absent_count = 0
                else:
                    self._clcc_absent_count += 1
                    if self._clcc_absent_count >= 2:
                        self._clcc_absent_count = 0
                        self._call_connected_event.clear()
                        self._connected_call_ids.clear()
                        self._fast_poll_until = 0.0
                        call_lost = True

        for number in pending_ring:
            if self._on_ring:
                self._on_ring(number)
        for connected_number in pending_connected:
            if self._on_call_connected:
                self._on_call_connected(connected_number)
        if call_lost:
            logger.warning("CLCC 连续无通话记录，判定通话已丢失（串口断连期挂断？），收尾会话")
            if self._on_hangup:
                self._on_hangup()

    def _read_sms(self, mem: str, index: str) -> None:
        """收到 +CMTI 后，按存储位读取短信并解析内容。"""
        try:
            if mem:
                self._send(f'AT+CPMS="{mem}"')
            response = self._send(f"AT+CMGR={index}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取短信失败 (index=%s): error_type=%s", index, type(exc).__name__)
            return

        cmgr_line = next(
            (line for line in response.splitlines() if line.strip().startswith("+CMGR:")),
            "",
        )
        sender, timestamp, body = self._interpret_sms(
            cmgr_line, self._extract_cmgr_body(response)
        )

        logger.info(
            "[短信] 来自=%s 时间=%s 字符数=%d", sender or "未知", timestamp, len(body)
        )
        delivered = False
        if self._on_sms:
            try:
                self._on_sms(sender, body, timestamp)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("短信回调异常: error_type=%s", type(exc).__name__)
        # 实时短信读入 app 后删 SIM 上这条，防存储堆满（默认开，可配关）。
        if delivered and index and config.get_bool("SMS_DELETE_AFTER_INGEST"):
            self._delete_stored_sms(index)

    def _interpret_sms(
        self, header_line: str, raw_body: str
    ) -> tuple[str | None, str, str]:
        """统一解析一条短信：文本模式看头部引号字段，PDU 模式则解码 PDU。"""
        sender, timestamp = self._parse_sms_header(header_line)
        if sender is None and _looks_like_pdu(raw_body):
            parsed = parse_sms_pdu(raw_body)
            if parsed is not None:
                return parsed
        return sender, timestamp, self._decode_sms_body(raw_body)

    @staticmethod
    def _parse_sms_header(line: str) -> tuple[str | None, str]:
        """从 +CMGR/+CMGL 头行提取 (发件号码, 时间戳)。

        文本模式下头部形如：
        +CMGR: "REC UNREAD","+8613800000000",,"26/07/01,14:20:07+32"
        中间的 alpha 名称字段常为空 (,,)，因此直接抽取所有引号内字段：
        [状态, 号码, (可选名称), 时间戳]。
        """
        quoted = re.findall(r'"([^"]*)"', line)
        if len(quoted) >= 2:
            sender = quoted[1] or None
            timestamp = quoted[-1] if len(quoted) >= 3 else ""
            return sender, timestamp
        return None, ""

    @staticmethod
    def _extract_cmgr_body(response: str) -> str:
        """从 +CMGR 响应里提取短信正文（+CMGR 行之后、OK 之前的内容）。"""
        lines = response.splitlines()
        body_lines: list[str] = []
        started = False
        for line in lines:
            if not started:
                if line.strip().startswith("+CMGR:"):
                    started = True
                continue
            if line.strip() in ("OK", "ERROR"):
                break
            body_lines.append(line)
        return "\n".join(body_lines).strip()

    @staticmethod
    def _decode_sms_body(body: str) -> str:
        """中文短信在文本模式下常以 UCS2 十六进制返回，尝试解码为可读文本。"""
        compact = body.strip()
        if (
            compact
            and len(compact) % 4 == 0
            and re.fullmatch(r"[0-9A-Fa-f]+", compact)
        ):
            try:
                return bytes.fromhex(compact).decode("utf-16-be")
            except (ValueError, UnicodeDecodeError):
                return body
        return body


class Eg25Modem(SerialModem):
    """Quectel EC20/EG25 变体：UAC/NMEA 语音通道、``AT+QVTS`` DTMF、``+QPCMV`` 流控。"""

    def initialize_for_voice(self, audio_mode: str = "uac") -> None:
        """启用 EG25 语音 PCM 通道。"""
        selected = audio_mode.lower()
        # uac_ffmpeg 只是宿主侧换 ffmpeg 实现，模组侧同 UAC（AT+QPCMV=1,2）。
        if selected in ("uac", "uac_ffmpeg"):
            self._send('AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1')
            self._send("AT+QPCMV=1,2")
            logger.info("UAC 语音通道已启用 (AT+QPCMV=1,2)")
            return
        if selected == "nmea":
            self._send("AT+QAUDMOD=3")
            self._send('AT+QGPSCFG="outport","none"')
            self._send("AT+QPCMV=1,0")
            logger.info("NMEA PCM 语音通道已启用 (AT+QPCMV=1,0)")
            return
        raise ValueError("audio_mode 只能是 uac、uac_ffmpeg（仅 macOS）或 nmea")

    def _send_dtmf_digit(self, ch: str) -> bool:
        # Quectel EC20/EG25 用 AT+QVTS（部分固件也接受 AT+VTS）。
        response = self._send(f'AT+QVTS="{ch}"')
        if "OK" not in response:
            response = self._send(f'AT+VTS="{ch}"')
        return "OK" in response

    def _disable_voice_channel(self) -> None:
        self._send("AT+QPCMV=0")

    def _scan_pcm_flow(self, text: str) -> None:
        for match in QPCMV_PATTERN.finditer(text):
            enable, mode = match.group(1), match.group(2)
            # +QPCMV: 1,0 => 模组就绪可继续发送；+QPCMV: 0,0 => 模组忙需暂停。
            if enable == "1":
                if not self._pcm_ready_event.is_set():
                    logger.info("模组上行就绪 (+QPCMV: %s,%s)", enable, mode)
                self._pcm_ready_event.set()
            elif enable == "0" and mode == "0":
                if self._pcm_ready_event.is_set():
                    logger.warning("模组上行忙，暂停发送 (+QPCMV: 0,0)")
                self._pcm_ready_event.clear()


class Sim7600Modem(SerialModem):
    """SIMCom SIM7600 变体：``AT+CPCMREG`` PCM-over-USB 语音、``AT+VTS`` DTMF。

    与 Quectel 的关键差异（Phase 0 真机实测，SIM7600G/固件 SIM7600M21-A_V2.0.2）：

    - **音频不走 UAC 声卡**，走 USB 接口 4 的 bulk 端点带内 PCM（8kHz/16-bit/mono）。
      该接口由 USB→PTY 桥暴露成串口，宿主侧用 ``nmea`` 音频模式的
      ``SerialPcmAudioBridge`` 直接读写——故 SIM7600 无需 UAC 声卡逻辑。
    - **``AT+CPCMREG=1`` 只在通话 active 后有效**，无活跃通话时发返回 ERROR。
      ``initialize_for_voice`` 因此不把非 OK 当致命：``_modem_supervisor`` 会在
      连接后（无呼叫时）调一次，那次预期 ERROR，真正使能发生在每通接通后的
      per-call 调用（见 ``call_agent`` 应答/接通之后）。
    - **无 ``+QPCMV`` 式带内流控 URC**，继承基类 ``_scan_pcm_flow`` no-op，
      ``pcm_ready`` 恒 True。
    """

    # 来电常在第二次 CPCMREG 才就绪，不能套用外呼「一次即成」的健康阈值，
    # 否则每通正常来电后都会误触发约 100 秒组合切换自愈。第三次起才判劣化。
    _DEGRADED_ENABLE_ATTEMPTS = 3
    # 通话结束后写这个哨兵文件，由 USB 桥进程读到后执行组合切换自愈（WIL-109）。
    # 只报告不自己动手：桥才是 USB 设备的持有者。
    RECOVERY_REQUEST_PATH = modem_hardware.RECOVERY_REQUEST_PATH

    # 刚接通瞬间 CPCMREG=1 偶发未就绪（真机实测：同样接通，有时首发 OK、有时 ERROR）。
    # 音频桥一旦 start() 就持续往 iface4 写 PCM，若此刻 CPCMREG 未生效、模组不排空该
    # bulk 端点 → USB 写超时 → 整个 USB 桥被判定掉线拆桥（连 AT 口一起塌）。故通话中
    # 必须重试到真正启用，确保写 PCM 前 iface4 已进入 PCM 模式。
    _CPCMREG_ENABLE_ATTEMPTS = 6
    _CPCMREG_ENABLE_RETRY_DELAY = 0.3

    _pcm_degraded = False   # 本通是否判定过劣化（通话结束时据此请求自愈）
    # 本实现对 AT+CPCMREG=1 自带有界重试，模组没就绪时首次失败即刻重试即可，
    # 不必盲等一整秒（WIL-104：健康态可省约 0.8s）。
    POST_ANSWER_SETTLE_SECONDS = 0.2

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        super().__init__(port, baudrate)
        self._voice_call_armed = False
        self._voice_call_generation = 0
        self._call_pcm_enabled = False
        self._reset_in_progress = False
        self._reset_access = threading.local()
        self._on_post_call_reset: Callable[[], None] | None = None
        self._answer_greeting_route_ready = False

    @property
    def reset_in_progress(self) -> bool:
        return self._reset_in_progress

    @property
    def vendor(self) -> str:
        return "simcom"

    @property
    def recovery_in_progress(self) -> bool:
        return self._reset_in_progress or super().recovery_in_progress

    @property
    def transport_paused(self) -> bool:
        return super().transport_paused or (
            self._reset_in_progress and not getattr(self._reset_access, "allowed", False)
        )

    def is_connected(self) -> bool:
        return not self._reset_in_progress and super().is_connected()

    @contextmanager
    def reset_transport_access(self) -> Iterator[None]:
        previous = getattr(self._reset_access, "allowed", False)
        self._reset_access.allowed = True
        try:
            yield
        finally:
            self._reset_access.allowed = previous

    def on_post_call_reset(self, callback: Callable[[], None]) -> None:
        self._on_post_call_reset = callback

    def _arm_voice_call(self) -> None:
        if self._reset_in_progress:
            raise serial.SerialException("SIM7600 模组重启中，不能开始通话")
        self._voice_call_generation += 1
        self._voice_call_armed = True
        self._call_pcm_enabled = False

    def answer(self) -> None:
        if self._on_post_call_reset is None:
            return super().answer()
        with self._serial_lock:
            self._arm_voice_call()
            super().answer()

    def dial(self, number: str, *, retry: bool = True, deadline: float | None = None) -> str:
        if self._on_post_call_reset is None:
            return super().dial(number, retry=retry, deadline=deadline)
        with self._serial_lock:
            self._arm_voice_call()
            return super().dial(number, retry=retry, deadline=deadline)

    def hangup(self) -> None:
        if self._on_post_call_reset is None:
            return super().hangup()
        if (config.get_bool("MODEM_LOG_RELEASE_CAUSE") and self._release_report.hung_up
                and self._release_report.requested and not self._release_report.done.is_set()):
            return
        callback = None
        query_report = None
        with self._serial_lock:
            if self._reset_in_progress:
                return
            super().hangup()
            eligible = self._voice_call_armed and self._call_pcm_enabled
            self._voice_call_armed = False
            self._call_pcm_enabled = False
            if eligible and config.get_bool("MODEM_RESET_AFTER_CALL") and self._on_post_call_reset is not None:
                self._reset_in_progress = True
                self._maintenance_generation += 1
                with self._sim_refresh_lock:
                    self._sim_refresh_generation += 1
                    self._sim_refresh_requested_generation = None
                callback = self._on_post_call_reset
                if config.get_bool("MODEM_LOG_RELEASE_CAUSE"):
                    self._release_report.before_reset = True
                    if not self._release_report.done.is_set():
                        self._release_report.after_query = callback
                        callback = None
                        # 已取得重启所有权，不再依赖上层资源清理成功才执行诊断和重启。
                        query_report = self._release_report
        if query_report is not None:
            with query_report.lock:
                query_report.deferred = False
            self._request_release_cause(query_report)
        if callback is not None:
            callback()

    def close_reset_transport(self) -> None:
        with self._serial_lock:
            try:
                if self._ser is not None:
                    self._ser.close()
            finally:
                self._ser = None
                self._buffer = ""
                self._emit_connection_state(False)

    def _init_answer_greeting(self, *, retry_transport: bool = True) -> None:
        self._answer_greeting_route_ready = False
        if not config.get_bool("PARALLEL_AGENT_CONNECT") or not config.get_str("MODEM_ANSWER_GREETING"):
            return
        commands = ["AT+CDTAM=1"]
        params = config.get_str("MODEM_TTS_PARAMS").strip()
        if params:
            # Restrict to the six ranges reported by AT+CTTSPARAM=?; never accept AT injection.
            if re.fullmatch(r"[0-2],[0-3],[0-3],[0-2],[0-2],[0-3]", params):
                commands.append(f"AT+CTTSPARAM={params}")
            else:
                logger.warning("SIM7600 CTTSPARAM 参数无效，跳过设置")
        for command in commands:
            name = command.partition("=")[0]
            try:
                response = self._send(command) if retry_transport else self._write_command(command)
                ok = "OK" in response and "ERROR" not in response
                if name == "AT+CDTAM":
                    self._answer_greeting_route_ready = ok
                if not ok:
                    logger.warning("SIM7600 %s 未成功，继续连接", name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SIM7600 %s 设置失败 (%s)，继续连接", name, type(exc).__name__)

    def play_answer_greeting(self, text: str) -> bool:
        """Best-effort native TTS; OK is acknowledgement, not proof of far-end playout."""
        started = time.monotonic()
        ok = False
        try:
            if not self._answer_greeting_route_ready:
                # A post-reset connection may precede TTS readiness. Retry once on
                # the live call, without entering an unbounded transport reconnect.
                self._init_answer_greeting(retry_transport=False)
            if not self._answer_greeting_route_ready:
                raise RuntimeError("native greeting route unavailable")
            if not text or any(ord(ch) > 0xFFFF or 0xD800 <= ord(ch) <= 0xDFFF for ch in text):
                raise ValueError("greeting must be nonempty UCS2")
            encoded = text.encode("utf-16-be").hex().upper()
            # Do not reconnect/retry a non-idempotent greeting after an uncertain write.
            # _write_command still owns the normal serial lock, response and URC handling.
            response = self._write_command(f'AT+CTTS=1,"{encoded}"')
            ok = "OK" in response and "ERROR" not in response
            if not ok:
                logger.warning("SIM7600 接听问候未成功，继续准备 PCM")
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIM7600 接听问候失败 (%s)，继续准备 PCM", type(exc).__name__)
        logger.info("[timing] 模组接听问候耗时 %.3fs ok=%s", time.monotonic() - started, ok)
        return ok

    def initialize_for_voice(self, audio_mode: str = "nmea") -> None:
        """使能 PCM-over-USB 语音通道（``AT+CPCMREG=1``）。

        SIM7600 仅此一条 USB 音频路径，与 ``audio_mode`` 取值无关（宿主侧桥的
        选择由 ``MODEM_AUDIO_MODE`` 单独决定，SIM7600 须用 ``nmea``）。

        有界重试至启用（``OK`` 即成）——**不**依赖 ``is_call_connected``：主叫链路
        上接通事件与本调用存在时序竞态（真机实测该标志会短暂读到 False），一旦漏判
        就只发一次而启用失败→下行写 iface4 全超时被丢→对端听不到 AI。无活跃通话时
        （supervisor 启动期）几次都 ERROR，属正常，不抛异常（否则 supervisor 误判连
        接失败无限重试）；``AT+CPCMREG=1`` 返回 OK 本身即证明有活跃通话。
        """
        # 曾在此发 AT+CLVL=2「消上行削波」——后证实削波是主机侧字节错位（已在
        # audio_bridge 修复），与音量无关；而实测这条指令本身要花 **2.3 秒**，
        # 占「接听→启用」总耗时的 83%（WIL-104）。治的是不存在的病，故移除。
        started = time.monotonic()
        call_generation = self._voice_call_generation
        call_armed = self._voice_call_armed
        # 先清快照：中途串口异常时读取方拿到 None（未知），而非上一次的旧结果。
        self.last_voice_enable = None
        if config.get_bool("MODEM_PCM_PIN_8K"):
            # VoLTE 出厂为 16k（0,1）；必须在每通启用 PCM 前对齐宿主的 8k 链路。
            try:
                response = self._send("AT+CPCMBANDWIDTH=1,1")
                if "OK" in response:
                    logger.info("SIM7600 PCM 已钉定 8kHz (AT+CPCMBANDWIDTH=1,1)")
                else:
                    logger.warning("AT+CPCMBANDWIDTH=1,1 未成功，继续尝试 CPCMREG")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AT+CPCMBANDWIDTH=1,1 失败 (%s)，继续尝试 CPCMREG", type(exc).__name__,
                )
        hold_uplink = audio_mode.lower() == "nmea" and config.get_int("MODEM_PCM_UPLINK_HOLD_MS") > 0
        for i in range(self._CPCMREG_ENABLE_ATTEMPTS):
            attempt_started = time.monotonic()
            ok = "OK" in self._send("AT+CPCMREG=1")
            enabled_mono = time.monotonic() if ok and hold_uplink else None
            logger.info(
                "[timing] CPCMREG 第 %d 次耗时 %.2fs -> %s",
                i + 1, time.monotonic() - attempt_started, "OK" if ok else "ERROR",
            )
            if ok:
                if call_armed:
                    with self._serial_lock:
                        if self._voice_call_armed and call_generation == self._voice_call_generation:
                            self._call_pcm_enabled = True
                attempts = i + 1
                logger.info(
                    "[timing] 启用总耗时 %.2fs", time.monotonic() - started
                )
                logger.info(
                    "PCM-over-USB 语音通道已启用 (AT+CPCMREG=1，第 %d 次)", attempts
                )
                if attempts >= self._DEGRADED_ENABLE_ATTEMPTS:
                    # 多次重试才就绪，标记待恢复；一次重试仍属来电正常波动。
                    # 真正的恢复在通话结束时请求，绝不打断进行中的通话。
                    self._pcm_degraded = True
                    logger.warning(
                        "PCM 启用重试 %d 次，判定模组已劣化，本通结束后请求自愈", attempts
                    )
                self.last_voice_enable = {
                    "ok": True,
                    "attempts": attempts,
                    "degraded": self._pcm_degraded,
                }
                if enabled_mono is not None:
                    self.last_voice_enable["enabled_mono"] = enabled_mono
                return
            if i < self._CPCMREG_ENABLE_ATTEMPTS - 1:
                time.sleep(self._CPCMREG_ENABLE_RETRY_DELAY)
        # 全部尝试失败：通话中意味着整通无声（下行写不进 iface4）；supervisor
        # 启动期（无活跃通话）属预期。区分交给读取方——有活跃通话才会落事件。
        self.last_voice_enable = {
            "ok": False,
            "attempts": self._CPCMREG_ENABLE_ATTEMPTS,
            "degraded": True,
        }
        logger.info("AT+CPCMREG=1 未启用（无活跃通话时属正常，接通后 per-call 会再试）")

    def _send_dtmf_digit(self, ch: str) -> bool:
        # SIM7600 用标准 3GPP AT+VTS；不同固件对引号宽容度不一，先无引号再回退带引号。
        # （DTMF 具体形态随固件差异，真机 IVR 导航时以实际响应为准复核。）
        response = self._send(f"AT+VTS={ch}")
        if "OK" not in response:
            response = self._send(f'AT+VTS="{ch}"')
        return "OK" in response

    def _disable_voice_channel(self) -> None:
        self._send("AT+CPCMREG=0")
        self._request_recovery_if_degraded()

    def _request_recovery_if_degraded(self) -> None:
        """通话结束时，若本通判定过劣化就写哨兵文件请求桥执行自愈。

        写文件而非直接动手：USB 设备由桥进程持有，组合切换必须由它来做。
        写失败不致命（下通若仍劣化会再写），绝不因此影响挂断流程。
        """
        if not self._pcm_degraded:
            return
        self._pcm_degraded = False
        if (self._on_post_call_reset is not None and self._voice_call_armed and self._call_pcm_enabled
                and config.get_bool("MODEM_RESET_AFTER_CALL")):
            # hangup 随后取得 CRESET 所有权；不能同时让桥拆掉它正在使用的 AT 口。
            logger.info("PCM 劣化由通话后 CRESET 接管，不并发请求 USB 组合恢复")
            return
        try:
            Path(self.RECOVERY_REQUEST_PATH).write_text(
                f"degraded at {time.time():.0f}\n", encoding="utf-8"
            )
            logger.warning("已请求 USB 桥执行 PCM 自愈: %s", self.RECOVERY_REQUEST_PATH)
        except OSError as exc:
            logger.warning("写恢复请求失败（不影响挂断）: %s", exc)

    def _hangup_command(self) -> str:
        # SIM7600 语音通话 ATH 不可靠（真机：ATH 返回 OK 但 CLCC 仍显示 active，
        # 电话不挂）；SIMCom 语音挂断用 AT+CHUP（挂断所有通话）。
        return "AT+CHUP"


def create_modem(
    port: str, baudrate: int = 115200, vendor: str = "quectel"
) -> SerialModem:
    """按厂商创建 modem 实例。

    支持 ``quectel``（EC20/EG25，默认）、``simcom``（SIM7600）及 Linux ``bluetooth``。
    未识别的 vendor
    回退 quectel 并告警，保证现网行为不变。
    """
    normalized = (vendor or "quectel").strip().lower()
    if normalized == "auto":
        normalized, port, _, _ = modem_hardware.resolve_settings(
            normalized, port, config.get_str("MODEM_AUDIO_MODE"), None,
        )
    if normalized in ("quectel", "ec20", "eg25"):
        return Eg25Modem(port, baudrate)
    if normalized in ("simcom", "sim7600", "sim7600g"):
        return Sim7600Modem(port, baudrate)
    if normalized == "bluetooth":
        import sys

        if sys.platform != "linux":
            raise RuntimeError("MODEM_VENDOR=bluetooth is supported on Linux only")
        from .bluetooth_modem import BluetoothModem

        return BluetoothModem(config.get_str("BLUETOOTH_PHONE_ADDRESS"))
    logger.warning("MODEM_VENDOR=%r 暂未支持，回退 quectel", vendor)
    return Eg25Modem(port, baudrate)
