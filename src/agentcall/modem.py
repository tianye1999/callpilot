"""Quectel EG25-G 模组 AT 指令封装。"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any, Callable, Literal

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
from .sms_concat import SmsConcat, SmsDelivery
from .sms_pdu import SmsDeliver, SubmitPdu, decode_deliver, encode_submit

logger = logging.getLogger(__name__)

CEND_PATTERN = re.compile(r"\+CEND:[^\r\n]*(?=\r?\n)")


@dataclass
class _SmsJob:
    """只在内存保留已编码短信，重试从失败段继续，避免重发成功段。"""

    number: str
    characters: int
    parts: list[SubmitPdu]
    message_id: str = field(default_factory=lambda: "msg_" + secrets.token_urlsafe(18))
    on_queue_result: Callable[[dict[str, Any]], None] | None = None
    next_part: int = 0
    failures: int = 0
    retry_at: float = 0.0


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

def parse_sms_pdu(pdu: str) -> tuple[str | None, str, str] | None:
    """保留 events.py 使用的旧接口；分段元数据由接收链路直接读取。"""
    message = decode_deliver(pdu)
    if message is None:
        return None
    return message.sender, message.timestamp, message.body


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
        self._sms_concat = SmsConcat()
        self._sms_condition = threading.Condition()
        self._sms_queue: deque[_SmsJob] = deque()
        self._sms_delivery_queue: list[SmsDelivery] = []
        self._sms_read_queue: deque[tuple[str, str]] = deque()
        self._sms_dispatch_lock = threading.Lock()
        self._sms_invalid_slots: set[tuple[str, str]] = set()
        self._sms_thread: threading.Thread | None = None
        self._sms_reference = 0
        self._sms_prompt_response = ""
        self._sms_call_was_present = False
        self._sms_settle_until = 0.0
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
        """收发统一 PDU；字符集固定 GSM，避免影响 CLIP/CLCC 号码。"""
        self._send("AT+CMGF=0")
        self._send('AT+CSCS="GSM"')
        self._send('AT+CPMS="SM","SM","SM"')
        self._send("AT+CNMI=2,1,0,0,0")
        logger.info("短信接收已启用 (PDU 模式, +CMTI 上报)")
        self._dump_stored_sms()

    def _dump_stored_sms(self) -> None:
        """补收 SIM 残段；交付成功前不删除，重启后可从 SIM 免费恢复重组。"""
        with self._serial_lock:
            try:
                selected = self._send('AT+CPMS="SM"')
                if "OK" not in selected or "ERROR" in selected:
                    raise RuntimeError("选择短信存储区失败")
                response = self._send("AT+CMGL=4")
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取已存短信失败: error_type=%s", type(exc).__name__)
                return
            entries = self._parse_cmgl(response)
            logger.info("模组内已存短信 %d 条，补收（重复自动去重）", len(entries))
            for index, message in entries:
                self._ingest_sms(message, storage=("SM", index), backfill=True)
        self._drain_sms_work()

    def _deliver_sms(self, message: SmsDeliver, *, backfill: bool = False) -> bool:
        logger.info("[短信] 来自=%s 时间=%s 字符数=%d",
                    message.sender or "未知", message.timestamp, len(message.body))
        if self._on_sms is None:
            return False
        try:
            from .notification_events import sms_backfill

            if backfill:
                with sms_backfill():
                    self._on_sms(message.sender, message.body, message.timestamp)
            else:
                self._on_sms(message.sender, message.body, message.timestamp)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("短信回调异常: error_type=%s", type(exc).__name__)
            return False

    def _ingest_sms(
        self, message: SmsDeliver, *, storage: tuple[str, str] | None = None, backfill: bool = False,
    ) -> bool:
        complete = self._sms_concat.add(message, time.monotonic(), storage=storage, backfill=backfill)
        if complete is not None:
            with self._sms_condition:
                if not any(item is complete for item in self._sms_delivery_queue):
                    self._sms_delivery_queue.append(complete)
        return True

    def _drain_sms_work(self) -> None:
        """嵌套 AT/初始化持锁时仅积累工作，由锁外入口或周期维护交付。"""
        with self._sms_condition:
            if not self._sms_read_queue and not self._sms_delivery_queue:
                return
        # CPython 的 RLock 所有权检查在 Windows/macOS 一致，不能只退出内层 with。
        # 测量锁等包装若未暴露所有权接口，保守延后，不能误在外层锁内回调。
        is_owned = getattr(self._serial_lock, "_is_owned", None)
        if is_owned is None or is_owned():
            return
        if not self._sms_dispatch_lock.acquire(blocking=False):
            return
        try:
            with self._sms_condition:
                reads = list(self._sms_read_queue)
                self._sms_read_queue.clear()
            for mem, index in reads:
                self._read_sms(mem, index)
            with self._sms_condition:
                deliveries, self._sms_delivery_queue = self._sms_delivery_queue, []
            for delivery in deliveries:
                if not delivery.delivered:
                    delivery.delivered = self._deliver_sms(delivery.message, backfill=delivery.backfill)
                if delivery.delivered:
                    self._delete_sms_delivery(delivery)
                if not delivery.delivered or delivery.storage:
                    with self._sms_condition:
                        self._sms_delivery_queue.append(delivery)
        finally:
            self._sms_dispatch_lock.release()

    def _delete_sms_delivery(self, delivery: SmsDelivery) -> None:
        if not config.get_bool("SMS_DELETE_AFTER_INGEST"):
            delivery.storage.clear()
            return
        # 回调完全结束后才短暂取锁；每个位置重选存储区，不依赖之前的 CPMS 状态。
        for mem, index in list(delivery.storage):
            with self._serial_lock:
                try:
                    response = self._send(f'AT+CPMS="{mem}"')
                    if "OK" not in response or "ERROR" in response:
                        raise RuntimeError("选择短信存储区失败")
                    if self._delete_stored_sms(index):
                        delivery.storage.discard((mem, index))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("清理短信副本失败 index=%s error_type=%s", index, type(exc).__name__)

    def _delete_stored_sms(self, index: str) -> bool:
        """成功交付后删副本；失败保留交付凭据供下一轮仅重试删除。"""
        try:
            response = self._send(f"AT+CMGD={index}")
            if "OK" not in response or "ERROR" in response:
                raise RuntimeError("删除短信失败")
            logger.debug("已删除 SIM 存储短信 index=%s", index)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除已存短信失败 (index=%s): error_type=%s", index, type(exc).__name__)
            return False

    def _warn_invalid_sms(self, mem: str, index: str) -> None:
        slot = (mem, index)
        if slot not in self._sms_invalid_slots:
            self._sms_invalid_slots.add(slot)
            logger.warning("短信 PDU 无效或编码不支持，保留副本 mem=%s index=%s", mem, index)

    def _parse_cmgl(self, response: str) -> list[tuple[str, SmsDeliver]]:
        """PDU 模式的数字状态头之后紧跟一行 PDU，忽略夹带的 URC。"""
        entries = []
        lines = response.splitlines()
        for index, line in enumerate(lines[:-1]):
            match = re.match(r"\+CMGL:\s*(\d+),\s*\d+,", line.strip())
            if match:
                message = decode_deliver(lines[index + 1].strip())
                if message is not None:
                    self._sms_invalid_slots.discard(("SM", match.group(1)))
                    entries.append((match.group(1), message))
                else:
                    self._warn_invalid_sms("SM", match.group(1))
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
        with self._sms_condition:
            if self._sms_queue:
                logger.warning("退出时仍有未发送短信 %d 条，不持久化", len(self._sms_queue))
            self._sms_condition.notify_all()
        if self._sms_thread and self._sms_thread is not threading.current_thread():
            self._sms_thread.join(timeout=21)
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
        """兼容旧调用方：已发送、已排队均为 True。"""
        return self.send_sms_status(number, text) != "failed"

    def send_sms_status(
        self, number: str, text: str, *, message_id: str | None = None,
        on_queue_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> Literal["sent", "queued", "failed"]:
        """三态发送；每条独立回调按 queued → sent/failed 发布，不绑定会话生命周期。"""
        if self._closed:
            return "failed"
        if not self._ser:
            raise RuntimeError("模组未连接")
        number = (number or "").strip()
        try:
            with self._sms_condition:
                parts = encode_submit(number, text, self._sms_reference)
                if len(parts) > 1:
                    self._sms_reference = (self._sms_reference + 1) % 256
        except (ValueError, UnicodeError):
            logger.warning("短信编码失败 -> %s 字符数=%d", number, len(text))
            return "failed"
        job = _SmsJob(number, len(text), parts)
        if message_id is not None:
            job.message_id = message_id
        job.on_queue_result = on_queue_result
        while job.next_part < len(parts):
            # 多段间让出串口；新通话出现时将剩余段交给挂断后调度。
            ok = self._send_sms_part(job)
            if ok is None:
                return "queued" if self._enqueue_sms(job) else "failed"
            if not ok:
                with self._sms_condition:
                    now = time.monotonic()
                    busy = self._sms_observe_call(now) or now < self._sms_settle_until
                if len(parts) == 1 and busy:
                    return "queued" if self._enqueue_sms(job) else "failed"
                return "failed"
            job.next_part += 1
            if job.next_part < len(parts):
                time.sleep(0.01)
        logger.info("短信已发送 -> %s 字符数=%d 段数=%d", number, len(text), len(parts))
        return "sent"

    _SMS_RETRY_SECONDS = 3.0

    def _sms_observe_call(self, now: float) -> bool:
        """由短信条件锁保护；接通、拨号、来电及保持都算占用通话资源。"""
        present = self._call_connected_event.is_set() or self._hardware_call_present
        if present:
            self._sms_call_was_present = True
        elif self._sms_call_was_present:
            self._sms_call_was_present = False
            self._sms_settle_until = now + max(0.0, config.get_float("SMS_CALL_SETTLE_SECONDS"))
        return present

    def sms_queue_status(self) -> list[dict[str, object]]:
        """只返回队列元数据，不泄露正文或编码后的 PDU。"""
        with self._sms_condition:
            return [
                {"message_id": job.message_id, "number": job.number,
                 "characters": job.characters, "segments": len(job.parts),
                 "sent_segments": job.next_part, "failures": job.failures}
                for job in self._sms_queue
            ]

    def _enqueue_sms(self, job: _SmsJob) -> bool:
        if self._closed:
            return False
        # 先发布 queued，再让后台看到任务，避免静置为零时终态抢在入队事件之前。
        self._notify_sms_queue(job, "queued")
        with self._sms_condition:
            if self._closed:
                self._notify_sms_queue(job, "failed")
                return False
            self._sms_queue.append(job)
            if self._sms_thread is None or not self._sms_thread.is_alive():
                self._sms_thread = threading.Thread(target=self._sms_send_loop, name="sms-send", daemon=True)
                self._sms_thread.start()
            self._sms_condition.notify_all()
        logger.info("短信已排队，挂断后发送 -> %s 字符数=%d 段数=%d",
                    job.number, job.characters, len(job.parts))
        return True

    @staticmethod
    def _notify_sms_queue(job: _SmsJob, status: str) -> None:
        if job.on_queue_result is not None:
            try:
                job.on_queue_result({"type": "sms_out", "message_id": job.message_id, "status": status})
            except Exception as exc:  # noqa: BLE001
                logger.warning("短信队列状态回调失败: error_type=%s", type(exc).__name__)

    def _send_sms_part(self, job: _SmsJob, *, queued: bool = False) -> bool | None:
        # 先检查通话，避免入队也阻塞在其它 AT 指令持有的串口锁上。
        with self._sms_condition:
            now = time.monotonic()
            present = self._sms_observe_call(now)
            if (queued or len(job.parts) > 1) and (
                present or now < self._sms_settle_until or (self._sms_queue and not queued)
            ):
                return None
        with self._serial_lock:
            with self._sms_condition:
                now = time.monotonic()
                present = self._sms_observe_call(now)
                if self._closed:
                    return False
                if (queued or len(job.parts) > 1) and (present or now < self._sms_settle_until):
                    return None
            if self.transport_paused:
                return None
            part = job.parts[job.next_part]
            try:
                # 不在此触发漫长重连；恢复由既有读循环负责，队列仅有限重试。
                mode = self._write_command("AT+CMGF=0")
                ok = "OK" in mode and "ERROR" not in mode and self._send_sms_payload(part.tpdu_length, part.pdu)
            except Exception as exc:  # noqa: BLE001
                logger.warning("短信发送异常: error_type=%s", type(exc).__name__)
                ok = False
        self._drain_sms_work()
        if not ok:
            logger.warning("短信发送失败 -> %s 字符数=%d 第%d/%d段",
                           job.number, job.characters, job.next_part + 1, len(job.parts))
        return ok

    def _sms_send_loop(self) -> None:
        while True:
            with self._sms_condition:
                if self._closed:
                    return
                if not self._sms_queue:
                    self._sms_condition.wait()
                    continue
                now = time.monotonic()
                present = self._sms_observe_call(now)
                if (present or now < self._sms_settle_until
                        or now < self._sms_queue[0].retry_at or self.transport_paused):
                    self._sms_condition.wait(timeout=0.1)
                    continue
                job = self._sms_queue[0]
            ok = self._send_sms_part(job, queued=True)
            terminal = ""
            with self._sms_condition:
                if self._closed:
                    return
                if ok:
                    job.next_part += 1
                    if job.next_part == len(job.parts):
                        self._sms_queue.popleft()
                        terminal = "sent"
                        logger.info("排队短信已发送 -> %s 字符数=%d 段数=%d",
                                    job.number, job.characters, len(job.parts))
                elif ok is False:
                    job.failures += 1
                    if job.failures >= 2:
                        self._sms_queue.popleft()
                        terminal = "failed"
                        logger.warning("排队短信重试后仍失败 -> %s 第%d/%d段",
                                       job.number, job.next_part + 1, len(job.parts))
                    else:
                        job.retry_at = time.monotonic() + self._SMS_RETRY_SECONDS
            if terminal:
                self._notify_sms_queue(job, terminal)
            with self._sms_condition:
                # 每段释放串口锁并主动让出执行，不能连续占住 CLCC/接听指令。
                self._sms_condition.wait(timeout=0.01)

    def _sms_maintenance(self) -> None:
        """周期检查重组超时并唤醒发送线程，所有消费者回调均在串口锁外。"""
        now = time.monotonic()
        with self._sms_condition:
            self._sms_observe_call(now)
            self._sms_condition.notify_all()
            self._sms_delivery_queue.extend(self._sms_concat.flush_expired(now))
        self._drain_sms_work()

    def _send_sms_payload(self, length: int, payload: str) -> bool:
        assert self._ser is not None
        self._sms_prompt_response = ""
        self._ser.write(f"AT+CMGS={length}\r".encode("ascii"))
        if not self._wait_for_prompt(timeout=5):
            logger.warning("未收到短信输入提示符 '>'")
            self._ser.write(b"\x1b")  # ESC 取消
            self._process_response_urcs(self._sms_prompt_response)
            return False
        self._ser.write(payload.encode("ascii") + b"\x1a")
        response = self._read_response(timeout=12)
        # 输入 PDU 期间不能插入 CMGR，必须提交结束后再消费提示符前的 CMTI。
        prompt_response, self._sms_prompt_response = self._sms_prompt_response, ""
        self._process_response_urcs(prompt_response + response)
        return "+CMGS:" in response and "OK" in response and "ERROR" not in response

    def _wait_for_prompt(self, timeout: float = 5) -> bool:
        assert self._ser is not None
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            raw = self._ser.read(self._ser.in_waiting or 1)
            if raw:
                buf += raw.decode("ascii", errors="ignore")
                self._sms_prompt_response = buf
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
        self._drain_sms_work()
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
        matches = list(CMTI_PATTERN.finditer(text))
        if not matches:
            return
        with self._sms_condition:
            for match in matches:
                slot = (match.group(1), match.group(2))
                if slot not in self._sms_read_queue:
                    self._sms_read_queue.append(slot)
        self._drain_sms_work()

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
            self._sms_maintenance()
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
            self._sms_maintenance()
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
        """锁内选择存储区、读取、解码和重组；锁外交付，再重选存储区删除。"""
        mem = mem or "SM"
        with self._serial_lock:
            try:
                selected = self._send(f'AT+CPMS="{mem}"')
                if "OK" not in selected or "ERROR" in selected:
                    raise RuntimeError("选择短信存储区失败")
                response = self._send(f"AT+CMGR={index}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取短信失败 (index=%s): error_type=%s", index, type(exc).__name__)
                return
            lines = response.splitlines()
            message = None
            for offset, line in enumerate(lines[:-1]):
                if re.match(r"\+CMGR:\s*\d+,", line.strip()):
                    message = decode_deliver(lines[offset + 1].strip())
                    break
            if message is None:
                self._warn_invalid_sms(mem, index)
                return
            self._sms_invalid_slots.discard((mem, index))
            self._ingest_sms(message, storage=(mem, index))
        self._drain_sms_work()

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
