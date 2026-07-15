"""Quectel EG25-G 模组 AT 指令封装。"""

from __future__ import annotations

import logging
import re
import threading
import time
from queue import Queue
from typing import Callable

import serial

from . import config, platforms, port_detect
from .sim_identity import UNKNOWN_SIM, SimIdentity, identify, with_registration

logger = logging.getLogger(__name__)

RING_PATTERN = re.compile(r"(?:^|\r\n)RING(?:\r\n|$)", re.MULTILINE)
CLIP_PATTERN = re.compile(r'\+CLIP:\s*"([^"]+)"')
CLCC_PATTERN = re.compile(
    r'\+CLCC:\s*(?P<idx>\d+),(?P<dir>\d+),(?P<stat>\d+),(?P<mode>\d+),'
    r'(?P<mpty>\d+)(?:,"(?P<number>[^"]*)",(?P<type>\d+))?'
)
QPCMV_PATTERN = re.compile(r"\+QPCMV:\s*(\d+),(\d+)")
CMTI_PATTERN = re.compile(r'\+CMTI:\s*"([^"]*)",\s*(\d+)')
CREG_PATTERN = re.compile(r"\+CREG:\s*(?:\d+\s*,\s*)?\d+(?=\s|$)")
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


def _looks_like_pdu(body: str) -> bool:
    compact = re.sub(r"\s+", "", body)
    return len(compact) >= 20 and bool(re.fullmatch(r"[0-9A-Fa-f]+", compact))


class Eg25Modem:
    """通过串口控制 EG25 模组：监听来电、接听、挂断、启用 UAC 音频。"""

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        # port 为 auto 哨兵时每次打开都重新探测，这里存本次解析出的实际端口（供日志）。
        self._active_port: str | None = None
        # SIM 身份缓存(#88):连接/重连后读一次 CIMI/CREG;换卡靠重插/重连触发刷新。
        self._sim_identity: SimIdentity = UNKNOWN_SIM
        self._ser: serial.Serial | None = None
        self._reader_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        # close() 是终态：置位后 start_listener/_reconnect 拒绝再启动，
        # 防止 stop 与后台 supervisor 首连之间的"资源复活"竞态。
        self._closed = False
        self._serial_lock = threading.RLock()
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
        self._processing_response_cmti = False
        self._last_caller: str | None = None
        self._last_dialed: str | None = None
        self._incoming_call_ids: set[str] = set()
        self._connected_call_ids: set[str] = set()
        # 通话在线标志：拨号后清除，外呼接通（CLCC dir=0 stat=0）或来电
        # 接听（ATA）时置位。除 _wait_connected 外，还是 CLCC「通话消失」
        # 判定的前提（见 _process_clcc_response）。
        self._call_connected_event = threading.Event()
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

    def send_command(self, command: str) -> str:
        """发送一条原始 AT 指令，返回模组原始响应文本（最底层原子能力）。

        供诊断/示例脚本做任意 AT 交互（如 ``AT+CSQ`` 查信号、``AT+COPS?`` 查
        网络、``AT+CPIN?`` 查 SIM）。与拨号/短信等共用串口锁，可与监听并发安全调用。
        """
        return self._send(command)

    # SIM 上电后 CIMI 可能短暂 ERROR(卡未 ready);重试覆盖上电延迟。
    _SIM_READ_RETRIES = 3
    _SIM_READ_RETRY_DELAY = 1.0

    def refresh_sim_identity(
        self,
        *,
        notify: bool = True,
        expected_generation: int | None = None,
    ) -> None:
        """读 CIMI/CREG 刷新 SIM 身份缓存(#88);AT 逻辑失败降级为 UNKNOWN_SIM,
        传输层故障上抛。connect/重连(``_open_serial``,已持串口锁)自动调用,
        换卡经拔插重连天然刷新;也可被上层主动调用重刷。

        已知限制(follow-up):不拔卡热换 SIM、注册状态后续变化不会自动上报刷新
        (需 +QSIMSTAT / +CREG=1 URC,列入 #88 follow-up)。CIMI 上电延迟已由
        本方法内短重试覆盖。
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
        if (
            expected_generation is not None
            and expected_generation != self._sim_refresh_generation
        ):
            return
        self._set_sim_identity(identify(imsi_raw, creg_raw), notify=notify)
        sim = self._sim_identity
        if sim.present:
            logger.info(
                "SIM 识别: %s (PLMN %s) → 免费客服 %s | 网络: %s",
                sim.carrier, sim.plmn, sim.service_number or "?", sim.reg_status,
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
        detected = port_detect.detect_at_port()
        if detected is None:
            raise serial.SerialException("MODEM_PORT=auto 未探测到 Quectel AT 串口")
        return detected

    def _open_serial(self) -> None:
        """打开串口并跑初始化序列（connect 与断线重连共用）。"""
        with self._serial_lock:
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
                self._send("AT+CLIP=1")
                self._init_sms()
                self._send("AT+QSIMSTAT=1")
                self._send("AT+CREG=1")
                self.refresh_sim_identity(notify=False)
            finally:
                self._opening = False

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
            while self._running and not self._closed:
                if self._reconnect_complete.wait(timeout=0.2):
                    return
            return

        self._emit_connection_state(False)
        delay = 1.0
        try:
            while self._running and not self._closed:
                try:
                    self._open_serial()
                    with self._serial_lock:
                        self._reconnect_generation += 1
                    self._emit_connection_state(True)
                    self._emit_current_sim_identity()
                    logger.info("串口已重连: %s", self._active_port)
                    return
                except (serial.SerialException, OSError) as exc:
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
            logger.warning("读取已存短信失败: %s", exc)
            return

        entries = self._parse_cmgl(response)
        if not entries:
            logger.info("模组内暂无已存短信")
            return
        logger.info("模组内已存短信 %d 条，补收（重复自动去重）", len(entries))
        delete_after = config.get_bool("SMS_DELETE_AFTER_INGEST")
        for index, sender, timestamp, body in entries:
            logger.info(
                "[补收短信] 来自 %s (%s): %s", sender or "未知", timestamp, body or "(空)"
            )
            delivered = False
            if self._on_sms:
                try:
                    self._on_sms(sender, body, timestamp)
                    delivered = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("补收短信回调异常: %s", exc)
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
            logger.warning("删除已存短信失败 (index=%s): %s", index, exc)

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
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
        if self._poll_thread:
            self._poll_thread.join(timeout=2)

    def answer(self) -> None:
        self._send("ATA")
        # 置「通话在线」：让来电同样受 CLCC 消失判定保护（串口死亡场景）。
        self._call_connected_event.set()
        logger.info("已发送 ATA 接听来电")

    def dial(self, number: str) -> str:
        """拨打语音电话（ATD<number>; 末尾分号表示语音呼叫）。"""
        number = (number or "").strip()
        if not number:
            raise ValueError("拨号号码为空")
        # 清接通状态与发拨号命令须原子：否则 CLCC 轮询线程可能在两次 clear
        # 之间 set 事件/加 call_id，导致 _wait_connected 永远等不到接通而误判未接。
        # _serial_lock 是 RLock，_send 内部再取同锁可重入。
        with self._serial_lock:
            self._call_connected_event.clear()
            self._connected_call_ids.clear()
            self._clcc_absent_count = 0
            self._clcc_fail_count = 0
            self._last_dialed = number
            response = self._send(f"ATD{number};")
        logger.info("已拨号 -> %s", number)
        return response

    def is_call_connected(self) -> bool:
        return self._call_connected_event.is_set()

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
        for ch in digits:
            # Quectel EC20/EG25 用 AT+QVTS（部分固件也接受 AT+VTS）。
            response = self._send(f'AT+QVTS="{ch}"')
            if "OK" not in response:
                response = self._send(f'AT+VTS="{ch}"')
            if "OK" not in response:
                logger.warning("DTMF 发送失败: count=%d, result=failure", len(digits))
                return False
            time.sleep(0.15)  # 位间间隔，防止连音被 IVR 吞掉
        logger.info("DTMF 发送完成: count=%d, result=success", len(digits))
        return True

    def hangup(self) -> None:
        # 两条指令与状态清理须原子：否则 CLCC 轮询线程可能插进 ATH 与
        # AT+QPCMV=0 之间，扰乱指令/响应配对。_pcm_ready_event.set() 只置位
        # 不等待，持锁调用无死锁风险。
        with self._serial_lock:
            self._send("ATH")
            self._send("AT+QPCMV=0")
            self._pcm_ready_event.set()
            self._call_connected_event.clear()
            self._connected_call_ids.clear()
            self._clcc_absent_count = 0
            self._clcc_fail_count = 0
        logger.info("已挂断并关闭语音 PCM 通道")

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
            logger.info("短信已发送 -> %s: %s", number, text)
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
        try:
            return self._write_command(cmd)
        except (serial.SerialException, OSError, RuntimeError) as exc:
            # 初始化序列中失败不自触发重连（由 _reconnect 的重试循环兜底），避免死锁。
            if self._opening:
                raise
            logger.warning("串口发送失败，尝试重连后重试: %s", exc)
            self._reconnect()
            return self._write_command(cmd)

    def _write_command(self, cmd: str) -> str:
        with self._serial_lock:
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
            try:
                with self._serial_lock:
                    if not self._ser or not self._ser.is_open:
                        raise serial.SerialException("串口未打开")
                    raw = self._ser.read(self._ser.in_waiting or 1)
            except (serial.SerialException, OSError) as exc:
                if not self._running:
                    break
                logger.warning("串口读取失败，尝试重连: %s", exc)
                self._reconnect()
                continue
            if not raw:
                continue
            text = raw.decode("ascii", errors="ignore")
            self._buffer += text
            self._process_buffer()

    def pcm_ready(self) -> bool:
        return self._pcm_ready_event.is_set()

    def _scan_qpcmv(self, text: str) -> None:
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

    def _process_response_urcs(self, text: str) -> None:
        self._scan_qpcmv(text)
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
        self._scan_qpcmv(self._buffer)
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
                logger.info("检测到来电 RING, 号码=%s", self._last_caller)
                if self._on_ring:
                    self._on_ring(self._last_caller)
                self._buffer = RING_PATTERN.sub("", self._buffer, count=1)
                continue

            if "NO CARRIER" in self._buffer or "+CEND:" in self._buffer:
                trigger = "NO CARRIER" if "NO CARRIER" in self._buffer else "+CEND:"
                logger.info("通话已结束 (触发=%s)", trigger)
                self._buffer = ""
                if self._on_hangup:
                    self._on_hangup()
                return

            if len(self._buffer) > 4096:
                self._buffer = self._buffer[-1024:]
            break

    def _process_sim_urcs(self) -> None:
        """Consume SIM/registration URCs without doing serial I/O on the reader."""
        while True:
            creg = CREG_PATTERN.search(self._buffer)
            qsim = QSIMSTAT_PATTERN.search(self._buffer)
            matches = [match for match in (creg, qsim) if match is not None]
            if not matches:
                return
            match = min(matches, key=lambda item: item.start())
            self._buffer = self._buffer[:match.start()] + self._buffer[match.end():]
            if match.re is CREG_PATTERN:
                self._set_sim_identity(with_registration(self._sim_identity, match.group(0)))
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

    def _poll_call_status(self) -> None:
        while self._running:
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
                        logger.warning(
                            "串口失联超 %d 秒且通话在线，判定通话已丢失，收尾会话",
                            self._CLCC_FAIL_THRESHOLD * 2,
                        )
                        if self._on_hangup:
                            self._on_hangup()
                time.sleep(2)
                continue

            self._clcc_fail_count = 0
            self._process_clcc_response(response)
            time.sleep(2)

    def _process_clcc_response(self, response: str) -> None:
        self._process_response_urcs(response)
        seen_incoming_ids: set[str] = set()
        # 与 dial() 对 _connected_call_ids/_call_connected_event 的清除互斥：
        # 状态判定+改写在 _serial_lock 内完成，回调收集后到锁外触发（避免持锁回调）。
        pending_ring: list[str | None] = []
        pending_connected: list[str | None] = []
        call_lost = False
        with self._serial_lock:
            has_call_line = False
            for match in CLCC_PATTERN.finditer(response):
                has_call_line = True
                call_id = match.group("idx")
                direction = match.group("dir")
                status = match.group("stat")
                number = match.group("number") or None

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
            logger.warning("读取短信失败 (index=%s): %s", index, exc)
            return

        cmgr_line = next(
            (line for line in response.splitlines() if line.strip().startswith("+CMGR:")),
            "",
        )
        sender, timestamp, body = self._interpret_sms(
            cmgr_line, self._extract_cmgr_body(response)
        )

        logger.info(
            "[短信] 来自 %s (%s): %s", sender or "未知", timestamp, body or "(空)"
        )
        delivered = False
        if self._on_sms:
            try:
                self._on_sms(sender, body, timestamp)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("短信回调异常: %s", exc)
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
