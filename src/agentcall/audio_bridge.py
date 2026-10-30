"""8kHz 模组音频 ↔ AI 音频格式桥接。"""

from __future__ import annotations

import errno
import logging
import os
import re
import select
import subprocess
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Iterable, cast

import numpy as np
import serial

# 导入模块而非 from-import 常量：让测试能 monkeypatch platforms.IS_MACOS。
from . import config, platforms
from .pcm_stats import PcmFlowStats

if TYPE_CHECKING:
    from .bluetooth_audio import BluetoothAudioBridge
    from .modem import SerialModem

logger = logging.getLogger(__name__)

MODEM_RATE = 8000
MODEM_CHANNELS = 1
MODEM_DTYPE = "int16"
MODEM_BLOCK_MS = 20
NMEA_READ_SIZE = 640
NMEA_WRITE_SIZE = 1600
NMEA_WRITE_INTERVAL_SECONDS = 0.1
# 经串口/PTY 的带内 PCM 下行（SIM7600 CPCMREG over USB→PTY 桥）用更小的帧稳定
# 进送：100ms 大帧会让模组播放缓冲在帧间饿死——真机实测对端听到断续（连续音变
# “嘟嘟嘟”）、语音直接听不清。20ms 小帧≈连续喂，缓冲不饿死。仅 SerialPcmAudioBridge
# 用，ffmpeg/UAC(Quectel) 路径仍用上面的 NMEA_WRITE_SIZE。
# 下行帧大小对齐 USB bulk 最大包（512B）：桥每次从 PTY 最多读 max_packet=512，
# 若按 640B/帧写，每帧会被拆成 512+128 两个 USB 包，而 128B 是**短包**——USB 语义
# 上表示「一次传输结束」，模组音频缓冲会据此做帧边界处理，每 40ms 一次 → 真机表现
# 为「每个字都卡」。写成 512B/32ms 即每帧恰好一个满包，不产生短包。
SERIAL_PCM_WRITE_SIZE = 512  # 32ms @ 8kHz/16-bit mono，= USB bulk 满包
# 上行对齐自检参数：攒够 ~1s 且有明显声音才判定，避免拿静音瞎判。
_ALIGN_PROBE_BYTES = 4000       # ~0.25s @ 8kHz/16-bit，避免开场长时间等待语音
_ALIGN_MIN_PEAK = 800           # 低于此峰值视为静音，继续攒
_ALIGN_RAIL_PEAK = 16000        # 静音错位后的大幅噪声，与安静偏移形成明确证据
_ALIGN_MARGIN = 0.15            # 偏移1 需明显优于对齐0 才纠正
_ALIGN_CLEAR_WIN = 0.6          # 判定所需的「明确赢家」相关性下限；两侧都含糊不锁定
_ALIGN_BAD_CORR = 0.3           # 锁定后监控：有声段相关性低于此=疑似中途错位，重探
_ALIGN_MONITOR_BYTES = 32000    # 锁定后每 ~2s 有声数据复核一次相关性
SERIAL_PCM_WRITE_INTERVAL_SECONDS = 0.032   # 512B @ 8kHz/16-bit = 32ms 实时
_UPLINK_PREBUFFER_BYTES = MODEM_RATE * 2 * 3
_UPLINK_BUFFER_BYTES = 64 * 1024
_UPLINK_CATCHUP_BYTES = NMEA_READ_SIZE * 8  # 每轮最多 320ms，兼顾追平速度与主循环响应。


def find_device_index(keyword: str, kind: str | None = None) -> int | None:
    # sounddevice 延迟导入：import 即初始化 CoreAudio/PortAudio，NMEA 串口
    # 模式完全用不到；顶层导入曾在 coreaudiod 异常时把整个进程卡死在启动。
    import sounddevice as sd

    keyword_lower = keyword.lower()
    for idx, dev in enumerate(sd.query_devices()):
        name = str(dev.get("name", "")).lower()
        if keyword_lower in name:
            if kind == "input" and dev.get("max_input_channels", 0) <= 0:
                continue
            if kind == "output" and dev.get("max_output_channels", 0) <= 0:
                continue
            logger.info("找到音频设备 [%s]: %s", idx, dev["name"])
            return idx
    return None


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return b""
    dst_len = max(1, int(len(samples) * dst_rate / src_rate))
    src_x = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
    resampled = np.interp(dst_x, src_x, samples)
    return resampled.astype(np.int16).tobytes()


def apply_pcm_gain(pcm: bytes, gain: float) -> bytes:
    if gain == 1.0 or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    amplified = np.clip(samples * gain, -32768, 32767)
    return amplified.astype(np.int16).tobytes()


class AgentPcm(bytes):
    """PCM eligible for AI playout observation; ordinary bytes include DTMF."""

    on_written: Callable[[], None] | None = None
    # Confirmed modem-rate bytes, including partial writes; never queue/dequeue counts.
    on_played: Callable[[int], None] | None = None

    def written_callback(self, count: int) -> Callable[[], None]:
        def notify() -> None:
            if self.on_written is not None:
                self.on_written()
            if self.on_played is not None:
                self.on_played(count)
        return notify


class PrebufferedPcm(bytes):
    """Historical uplink, not evidence of a live interruption of the AI opening."""


class _OutputObserver:
    """Opt-in source spans follow queued bytes, including drops and partial writes."""

    _output_observer: Callable[[bytes], None] | None = None
    _output_ranges: tuple[tuple[int, int, Callable[[], None] | None], ...] = ()
    _output_progress = 0
    output_write_errors = 0

    def set_output_observer(self, observer: Callable[[bytes], None]) -> None:
        """Before start(), observe successfully written AI bytes, excluding filler/DTMF.

        This is a local output boundary, not confirmation of far-end audibility.
        """
        self._output_observer = observer
        self._output_spans: deque[tuple[int, AgentPcm | None]] = deque()
        self._output_progress = 0
        self.output_write_errors = 0

    def output_progress_bytes(self) -> int:
        """Confirmed non-filler writes, including DTMF; dequeues/drops aren't progress."""
        return self._output_progress

    def _note_output_progress(self, count: int) -> None:
        if self._output_observer is not None:
            self._output_progress += count

    def _queue_output_span(self, chunk: bytes) -> None:
        if self._output_observer is not None and chunk:
            self._output_spans.append((len(chunk), chunk if isinstance(chunk, AgentPcm) else None))

    def _take_output_ranges(self, count: int) -> tuple[tuple[int, int, Callable[[], None] | None], ...]:
        if self._output_observer is None:
            return ()
        offset = 0
        ranges = []
        while count and self._output_spans:
            length, agent_audio = self._output_spans.popleft()
            take = min(count, length)
            if agent_audio:
                ranges.append((offset, offset + take, agent_audio.written_callback(take)))
            if take < length:
                self._output_spans.appendleft((length - take, agent_audio))
            offset += take
            count -= take
        return tuple(ranges)

    def _notify_output(
        self, payload: bytes, ranges: tuple[tuple[int, int, Callable[[], None] | None], ...],
    ) -> None:
        observer = self._output_observer
        if observer is None:
            return
        for start, end, on_written in ranges:
            pcm = payload[start:end]
            if pcm:
                try:
                    if on_written is not None:
                        on_written()
                    observer(pcm)
                except Exception:  # noqa: BLE001
                    logger.warning("下行写出观测失败（不影响播放）", exc_info=True)


class ModemAudioBridge(_OutputObserver):
    """在 EG25 USB 声卡与 Agent 之间转发 PCM 音频（PortAudio 直连）。

    Windows（WASAPI）/ Linux（ALSA）的标准路径；macOS 上 PortAudio 打不开
    EC20 UAC（AUHAL -66740），须改用 FfmpegAudioBridge。设备按驱动上报的
    名称做子串匹配：Windows 官方驱动下 UAC 设备名可能与 macOS/Linux 不同，
    且 MME host API 会把名称截断到 31 字符，必要时调整 MODEM_AUDIO_KEYWORD。
    Windows/WASAPI 行为待硬件验证。
    """

    def __init__(self, device_keyword: str) -> None:
        self.input_device_index = find_device_index(device_keyword, "input")
        self.output_device_index = find_device_index(device_keyword, "output")
        if self.input_device_index is None or self.output_device_index is None:
            raise RuntimeError(
                f"未找到包含 '{device_keyword}' 的 UAC 输入/输出设备，请检查 EG25 UAC 是否启用"
            )
        self._input_stream: Any = None
        self._output_stream: Any = None
        self._block_size = int(MODEM_RATE * MODEM_BLOCK_MS / 1000)

    def start(self) -> None:
        import sounddevice as sd

        self._input_stream = sd.RawInputStream(
            samplerate=MODEM_RATE,
            blocksize=self._block_size,
            dtype=MODEM_DTYPE,
            channels=MODEM_CHANNELS,
            device=self.input_device_index,
        )
        self._output_stream = sd.RawOutputStream(
            samplerate=MODEM_RATE,
            blocksize=self._block_size,
            dtype=MODEM_DTYPE,
            channels=MODEM_CHANNELS,
            device=self.output_device_index,
        )
        self._input_stream.start()
        self._output_stream.start()
        logger.info("模组音频流已启动 (8kHz mono)")

    def stop(self) -> None:
        for stream in (self._input_stream, self._output_stream):
            if stream:
                stream.stop()
                stream.close()
        self._input_stream = None
        self._output_stream = None

    def read_modem_chunk(self) -> bytes:
        if not self._input_stream:
            return b""
        data, _overflow = self._input_stream.read(self._block_size)
        return bytes(data)

    def pending_output_bytes(self) -> int:
        return 0

    def write_modem_chunks(self, chunks: Iterable[bytes]) -> None:
        if not self._output_stream:
            return
        for chunk in chunks:
            if chunk:
                try:
                    self._output_stream.write(chunk)
                except Exception:
                    if self._output_observer is not None:
                        self.output_write_errors += 1
                    raise
                self._note_output_progress(len(chunk))
                if isinstance(chunk, AgentPcm):
                    self._notify_output(chunk, ((0, len(chunk), chunk.written_callback(len(chunk))),))

    @staticmethod
    def modem_to_agent(pcm_8k: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_8k, MODEM_RATE, agent_rate)

    @staticmethod
    def agent_to_modem(pcm_agent: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_agent, agent_rate, MODEM_RATE)


class SerialPcmAudioBridge(_OutputObserver):
    """通过 EG25 USB NMEA 口传输 Voice over USB PCM。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 921600,
        tx_gain: float = 1.0,
        write_size: int = SERIAL_PCM_WRITE_SIZE,
        write_interval: float = SERIAL_PCM_WRITE_INTERVAL_SECONDS,
        uplink_hold_ms: int = 0,
        pcm_enabled_at: float | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.tx_gain = tx_gain
        self._uplink_hold_ms = uplink_hold_ms
        self._pcm_enabled_at = pcm_enabled_at
        self._first_downlink = threading.Event()
        self._first_downlink_bytes = 0
        self._uplink_started = False
        # 下行帧大小/节流：小帧稳定进送，避免模组播放缓冲在帧间饿死（真机调参点）。
        self._write_size = write_size
        self._write_interval = write_interval
        # 上行读 carry：串口/PTY 单次 read 可能返回奇数字节（16-bit 采样被拆到两次
        # 读），直接喂 np.frombuffer(int16) 会 ValueError 崩掉整通。留 1 字节到下次。
        self._rx_carry = b""
        # 上行字节对齐自愈：carry 只保证「相对流首」的一致，保证不了流首本身是否
        # 落在采样边界上。真机实测（2026-08-12）整条上行恒定偏移 1 字节——高低字节
        # 颠倒后正常语音变成振幅顶满的垃圾：VAD 以为有人说话、ASR 一个字认不出，
        # 长期被误判成「模组上行削波」。这里用相邻样本相关性判定真实对齐并一次性纠正。
        self._align_locked = False
        self._align_drop = False
        self._align_probe = bytearray()
        self._align_monitor = bytearray()
        # 上行积压观测：读前 tty 队列与读后进程缓冲分开聚合，每 5s 同行报告。
        self._inwaiting_max = 0
        self._inwaiting_sum = 0
        self._inwaiting_n = 0
        self._rx_buffer_max = 0
        self._rx_buffer_sum = 0
        self._rx_buffer_n = 0
        self._inwaiting_logged_at = 0.0
        # 下行首帧观测：最近一次真实（非补零）帧进串口的时刻。
        self._last_real_payload_at = 0.0
        self._ready_check: "Callable[[], bool] | None" = None
        self._ser: serial.Serial | None = None
        self._tx_buffer = bytearray()
        self._tx_lock = threading.Lock()
        # 串口写入与挂断清空共用此锁，禁止已经取出的旧帧在挂断清空后补写。
        self._write_lock = threading.Lock()
        self._hangup_silenced = False
        self._writer_thread: threading.Thread | None = None
        self._running = False
        self._written_bytes = 0
        self._queued_bytes = 0
        self._last_stats_at = 0.0
        self._write_timeouts = 0
        self._last_uplink_at: float | None = None
        self._prebuffer_enabled = False
        self._prebuffer_dropped = 0
        self._rx_buffer = bytearray()
        self._rx_condition = threading.Condition()
        self._reader_thread: threading.Thread | None = None
        self._reader_alive = False
        self._opening_pending = True
        self._rx_dropped = 0
        self._rx_drop_logged_at = float("-inf")
        # 消费/对齐、故障回退读与启停互斥；读线程只拿 condition，join 不会死锁。
        self._rx_lock = threading.Lock()

    def start(self) -> None:
        with self._rx_lock:
            self._start_serial()

    def reset_stall_clock(self) -> None:
        """启动或重挂后重新给上行完整观察窗口，并清连续写超时。"""
        self._last_uplink_at = time.monotonic()
        self._write_timeouts = 0

    def pcm_stalled(self) -> str | None:
        if not self._running or self._last_uplink_at is None:
            return None
        if self._write_timeouts >= 3:
            return "consecutive_write_timeouts"
        if time.monotonic() - self._last_uplink_at >= config.get_float("PCM_STALL_SECONDS"):
            return "no_uplink_bytes"
        return None

    def _start_serial(self) -> None:
        self._ser = self._open_serial()
        self._rx_carry = b""
        self._align_locked = False
        self._align_drop = False
        self._align_probe.clear()
        self._align_monitor.clear()
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
        self._running = True
        self.reset_stall_clock()
        self._written_bytes = 0
        self._queued_bytes = 0
        self._write_timeouts = 0
        self._last_stats_at = time.monotonic()
        self._first_downlink.clear()
        self._first_downlink_bytes = 0
        self._uplink_started = False
        self._hangup_silenced = False
        self._last_real_payload_at = 0.0
        self._inwaiting_max = self._inwaiting_sum = self._inwaiting_n = 0
        self._rx_buffer_max = self._rx_buffer_sum = self._rx_buffer_n = 0
        self._inwaiting_logged_at = time.monotonic()
        self._prebuffer_enabled = config.get_bool("MODEM_PCM_UPLINK_PREBUFFER")
        if self._uplink_hold_ms > 0:
            # 首帧探测始终由常驻读线程完成，不依赖 Agent 连接进度。
            self._prebuffer_enabled = True
        with self._rx_condition:
            self._rx_buffer.clear()
            self._prebuffer_dropped = 0
            self._rx_dropped = 0
            self._rx_drop_logged_at = float("-inf")
            self._opening_pending = True
            self._reader_alive = True
        self._reader_thread = threading.Thread(
            target=self._read_loop, name="pcm-uplink-reader", daemon=True,
        )
        self._reader_thread.start()
        self._writer_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._writer_thread.start()
        logger.info(
            "NMEA PCM 音频流已启动: %s (8kHz mono, tx_gain=%.2f)",
            self.port,
            self.tx_gain,
        )

    def _open_serial(self) -> serial.Serial:
        """打开 PCM 数据串口；macOS 的 USB→PTY 桥不支持自定义波特率 ioctl 时回退。

        SIM7600(simcom) 在 macOS 上，PCM 口是 ``sim7600_usb_pty`` 桥出来的伪终端
        (PTY)。pyserial 为非标准波特率(如 921600)走 macOS ``IOSSIOSPEED`` ioctl，
        PTY 不支持会抛 ``ENOTTY``；而 PTY 上波特率本无意义(字节按桥的 USB 泵速流动)。
        故 ENOTTY 时退回标准 115200(与 AT 口同、PTY 可接受)重开。真串口(Windows/
        Linux 的 Quectel NMEA 口)波特率有效，正常路径不触发回退。
        """
        try:
            return serial.Serial(
                port=self.port, baudrate=self.baudrate, timeout=0.02, write_timeout=0.2,
            )
        except OSError as exc:
            if exc.errno != errno.ENOTTY:
                raise
            logger.info(
                "PCM 口 %s 为 PTY，自定义波特率 %d 不适用(ENOTTY)，回退 115200 打开",
                self.port, self.baudrate,
            )
            return serial.Serial(
                port=self.port, baudrate=115200, timeout=0.02, write_timeout=0.2,
            )

    def stop(self) -> None:
        with self._rx_lock:
            self._stop_serial()

    def _stop_serial(self) -> None:
        with self._rx_condition:
            self._running = False
            self._rx_condition.notify_all()
        # 串口读有 20ms 超时；必须确认退出后才能关 fd，不能超时后带着在途读关闭。
        if self._reader_thread:
            self._reader_thread.join()
        if self._writer_thread:
            self._writer_thread.join()
        with self._write_lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            self._ser = None
        with self._tx_lock:
            self._take_output_ranges(len(self._tx_buffer))
            self._tx_buffer.clear()
        with self._rx_condition:
            self._rx_buffer.clear()
            if self._rx_dropped:
                logger.info("上行缓冲丢弃汇总: bytes=%d(≈%.0fms)",
                            self._rx_dropped, self._rx_dropped / (MODEM_RATE * 2) * 1000)
                self._rx_dropped = 0

    def read_modem_chunk(self) -> bytes:
        with self._rx_lock:
            return self._read_modem_chunk()

    @property
    def uplink_prebuffer_enabled(self) -> bool:
        return self._prebuffer_enabled

    def _read_loop(self) -> None:
        try:
            while self._running:
                data = self._read_serial_chunk()
                with self._rx_condition:
                    self._rx_buffer.extend(data)
                    cap = (_UPLINK_PREBUFFER_BYTES
                           if self._opening_pending and self._prebuffer_enabled
                           else _UPLINK_BUFFER_BYTES)
                    excess = len(self._rx_buffer) - cap
                    if excess > 0:
                        # 仅丢整样本，保持 carry 与剩余流的字节相位。
                        drop = excess + excess % 2
                        del self._rx_buffer[:drop]
                        self._rx_dropped += drop
                        if self._opening_pending:
                            self._prebuffer_dropped += drop
                        now = time.monotonic()
                        if now - self._rx_drop_logged_at >= 5:
                            logger.warning("上行缓冲溢出，丢弃最旧整样本: bytes=%d 累计=%d",
                                           drop, self._rx_dropped)
                            self._rx_drop_logged_at = now
                    self._log_uplink_backlog(len(self._rx_buffer))
                    self._rx_condition.notify_all()
        except (OSError, serial.SerialException) as exc:
            logger.warning("上行常驻读取失败，将回退串口直读: error_type=%s", type(exc).__name__)
        finally:
            with self._rx_condition:
                self._reader_alive = False
                self._rx_condition.notify_all()

    def _read_modem_chunk(self) -> bytes:
        if not self._ser:
            return b""
        historical = False
        raw = b""
        with self._rx_condition:
            if self._opening_pending:
                self._opening_pending = False
                if self._prebuffer_enabled:
                    raw = bytes(self._rx_buffer)
                    self._rx_buffer.clear()
                    historical = bool(raw)
                    logger.info(
                        "[timing] 上行预缓冲: bytes=%d(≈%.0fms) dropped=%d",
                        len(raw), len(raw) / (MODEM_RATE * 2) * 1000, self._prebuffer_dropped,
                    )
                else:
                    # 奇出的半个样本保留，不能因丢开场积压引入新的错位。
                    drop = len(self._rx_buffer) // 2 * 2
                    del self._rx_buffer[:drop]
                    logger.info("上行开场积压已丢弃: bytes=%d", drop)
            if not historical:
                deadline = time.monotonic() + 0.02
                while self._reader_alive and self._running and len(self._rx_buffer) < NMEA_READ_SIZE:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._rx_condition.wait(remaining)
                count = (min(len(self._rx_buffer), _UPLINK_CATCHUP_BYTES)
                         if len(self._rx_buffer) > NMEA_READ_SIZE * 2 else NMEA_READ_SIZE)
                raw = bytes(self._rx_buffer[:count])
                del self._rx_buffer[:len(raw)]
            reader_alive = self._reader_alive
        if not raw and not reader_alive:
            # 此处仍持有 _rx_lock，stop 不会在故障回退读期间关闭串口。
            raw = self._read_serial_chunk()
        # 对齐器继续按原块长观察同一条流，避免批量追平改变纠错的判定边界。
        data = b"".join(self._align_uplink(raw[offset:offset + NMEA_READ_SIZE])
                        for offset in range(0, len(raw), NMEA_READ_SIZE))
        return PrebufferedPcm(data) if historical else data

    def _align_uplink(self, raw: bytes) -> bytes:
        # carry + 本次读，保证返回偶数字节（16-bit 对齐）；奇出的 1 字节留到下次。
        data = self._rx_carry + raw
        if self._align_drop and data:
            # 判定为错位：丢 1 字节把整条流拨回采样边界（此后 carry 维持新对齐）。
            data = data[1:]
            self._align_drop = False
        if len(data) % 2:
            self._rx_carry = data[-1:]
            data = data[:-1]
        else:
            self._rx_carry = b""
        if not self._align_locked:
            self._probe_alignment(data)
        else:
            self._monitor_alignment(data)
        return data

    def _read_serial_chunk(self) -> bytes:
        assert self._ser is not None
        # 每次 read 前采样 tty，直接观察内核小队列是否仍被顶满。
        try:
            waiting = self._ser.in_waiting
        except (OSError, AttributeError):
            waiting = -1
        if waiting >= 0:
            self._inwaiting_max = max(self._inwaiting_max, waiting)
            self._inwaiting_sum += waiting
            self._inwaiting_n += 1
        if not self._reader_alive:
            self._log_uplink_backlog()
        data = self._ser.read(NMEA_READ_SIZE)
        if data:
            # 在原始读取处计时：奇数字节、数字静音、回退直读都算通路有数据。
            self._last_uplink_at = time.monotonic()
        if self._uplink_hold_ms > 0 and not self._first_downlink.is_set():
            self._first_downlink_bytes += len(data)
            if self._first_downlink_bytes >= 2:
                now = time.monotonic()
                assert self._pcm_enabled_at is not None
                logger.info("PCM_UPLINK_HOLD 首帧下行到达耗时 elapsed_ms=%.3f mono=%.6f",
                            (now - self._pcm_enabled_at) * 1000, now)
                self._first_downlink.set()
        return data

    def _log_uplink_backlog(self, buffered: int | None = None) -> None:
        # 常驻线程退出前才释放 _reader_alive；之后回退直读持有 _rx_lock，
        # 因而两条路径不会并发更新统计。进程缓冲仅由读线程采样。
        if buffered is not None:
            self._rx_buffer_max = max(self._rx_buffer_max, buffered)
            self._rx_buffer_sum += buffered
            self._rx_buffer_n += 1
        now = time.monotonic()
        if now - self._inwaiting_logged_at < 5:
            return
        # 失败样本不稀释均值；整个窗口都不可读时保留 -1，不能误报为零积压。
        tty_max = self._inwaiting_max if self._inwaiting_n else -1
        tty_avg = self._inwaiting_sum // self._inwaiting_n if self._inwaiting_n else -1
        tty_ms = tty_max / (MODEM_RATE * 2) * 1000 if self._inwaiting_n else -1
        stats = "tty max=%dB(≈%.0fms) 均值=%dB (n=%d)" % (
            tty_max, tty_ms, tty_avg, self._inwaiting_n,
        )
        if buffered is not None:
            stats += " | 读线程缓冲 max=%dB(≈%.0fms) 均值=%dB (n=%d)" % (
                self._rx_buffer_max, self._rx_buffer_max / (MODEM_RATE * 2) * 1000,
                self._rx_buffer_sum // self._rx_buffer_n, self._rx_buffer_n,
            )
        logger.info("[timing] 上行积压%s: %s", "（直读）" if buffered is None else "", stats)
        self._inwaiting_max = self._inwaiting_sum = self._inwaiting_n = 0
        self._rx_buffer_max = self._rx_buffer_sum = self._rx_buffer_n = 0
        self._inwaiting_logged_at = now

    def _probe_alignment(self, data: bytes) -> None:
        """用相邻样本相关性判断上行是否整体错位 1 字节。

        真实语音相邻样本高度相关（r>0.8）；错位后高低字节颠倒，相关性趋近 0。
        一边安静而另一边大幅噪声时直接锁定安静偏移；两边都静音则不猜。
        其余情况用有声样本相关性判断。证据必须**一边倒**才
        锁定（真机教训 2026-08-12：两个偏移都 0.8+ 的含糊样本被当「对齐正常」
        锁死，整通乱码没人管）；含糊就扔掉这批继续攒。锁定后仍由
        _monitor_alignment 持续复核——劣化的模组会中途丢字节，错位可能随时发生。
        """
        self._align_probe.extend(data)
        if len(self._align_probe) < _ALIGN_PROBE_BYTES:
            return
        probe = bytes(self._align_probe)
        self._align_probe.clear()
        views = []
        for offset in (0, 1):
            chunk = probe[offset: offset + (len(probe) - offset) // 2 * 2]
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            if samples.size < 1000:
                return
            views.append(samples)
        peaks = [float(np.max(np.abs(samples))) for samples in views]
        if min(peaks) < _ALIGN_MIN_PEAK and max(peaks) >= _ALIGN_RAIL_PEAK:
            self._align_locked = True
            self._align_drop = peaks[1] < peaks[0]
            logger.info("上行静音对齐锁定：偏移0峰值 %.0f，偏移1峰值 %.0f，补丢字节=%d",
                        peaks[0], peaks[1], int(self._align_drop))
            return
        if min(peaks) < _ALIGN_MIN_PEAK:
            return
        scores = []
        for samples in views:
            scores.append(float(np.corrcoef(samples[:-1], samples[1:])[0, 1]))
        best = max(scores)
        # NaN（恒定/退化信号，如纯音尾巴）视为含糊：NaN 的比较全为 False，
        # 不设防会直接落到锁定分支。
        if (
            not all(np.isfinite(s) for s in scores)
            or best < _ALIGN_CLEAR_WIN
            or abs(scores[0] - scores[1]) < _ALIGN_MARGIN
        ):
            logger.info(
                "上行对齐证据含糊（对齐0 %.3f，偏移1 %.3f），不锁定继续探",
                scores[0], scores[1],
            )
            return
        self._align_locked = True
        if scores[1] > scores[0]:
            self._align_drop = True
            logger.warning(
                "上行字节错位已纠正：偏移1 相关性 %.3f > 对齐0 %.3f（丢 1 字节回到采样边界）",
                scores[1], scores[0],
            )
        else:
            logger.info(
                "上行字节对齐正常（对齐0 相关性 %.3f，偏移1 %.3f）", scores[0], scores[1]
            )

    def _monitor_alignment(self, data: bytes) -> None:
        """锁定后的持续复核：有声段相关性塌到乱码水平就解锁重探。

        中途错位的来源是 USB/模组丢字节（劣化形态之一），carry 只保证「相对流首」
        的对齐，保证不了流本身不丢字节。重探期间照常出流（乱码已经在流上了，
        不会更糟）；重探判定错位后丢 1 字节归位，代价是 1 个采样点的毛刺。
        """
        self._align_monitor.extend(data)
        if len(self._align_monitor) < _ALIGN_MONITOR_BYTES:
            return
        probe = bytes(self._align_monitor)
        self._align_monitor.clear()
        usable = len(probe) - (len(probe) % 2)
        samples = np.frombuffer(probe[:usable], dtype=np.int16).astype(np.float32)
        if samples.size < 1000 or np.max(np.abs(samples)) < _ALIGN_MIN_PEAK:
            return  # 静音段，无法复核
        corr = float(np.corrcoef(samples[:-1], samples[1:])[0, 1])
        if corr < _ALIGN_BAD_CORR:
            self._align_locked = False
            self._align_probe.clear()
            logger.warning(
                "上行有声段相关性塌陷（%.3f < %.1f），疑似中途字节错位，重新探测对齐",
                corr, _ALIGN_BAD_CORR,
            )

    def pending_output_bytes(self) -> int:
        with self._tx_lock:
            return len(self._tx_buffer)

    def discard_pending_output(self) -> int:
        """立即丢弃未播的下行积压（barge-in 打断用），返回丢弃字节数。

        对端开口打断 AI 时，OpenAI 已突发投递的整段音频可能还有十几秒积压在
        这里慢慢播；不清掉的话「打断」只是不再生成新音频，旧音频仍会播完。
        """
        with self._tx_lock:
            dropped = len(self._tx_buffer)
            self._take_output_ranges(dropped)
            self._tx_buffer.clear()
        return dropped

    def silence_for_hangup(self) -> int:
        """挂断后彻底停写（含补零帧），直到下次 start；返回丢弃的排队字节数。

        与打断用的 discard_pending_output 不同，这里停止静音时钟并清串口输出，
        防止旧尾音进入下一通。先等在途写完成，再清空，不能让已取出的帧漏写。
        """
        # 先置位再等写锁：写线程落后节拍时会连续抢回锁追帧，等锁期间不置位
        # 就会把排队的尾音补写出去（CI macOS 3.12 偶发复现）。
        with self._tx_lock:
            self._hangup_silenced = True
        with self._write_lock:
            with self._tx_lock:
                dropped = len(self._tx_buffer)
                self._take_output_ranges(dropped)
                self._tx_buffer.clear()
                self._output_ranges = ()
                self._output_real_bytes = 0
            try:
                if self._ser is not None:
                    self._ser.reset_output_buffer()
            except Exception:  # noqa: BLE001
                pass
        return dropped

    def set_ready_check(self, ready_check: Callable[[], bool]) -> None:
        """注入上行流控判断：返回 False 时暂停向模组写 PCM。"""
        self._ready_check = ready_check

    def write_modem_chunks(self, chunks: Iterable[bytes]) -> None:
        if not self._ser:
            return
        appended = 0
        with self._tx_lock:
            if self._hangup_silenced:
                return
            for chunk in chunks:
                if chunk:
                    self._tx_buffer.extend(chunk)
                    self._queue_output_span(chunk)
                    appended += len(chunk)
            self._queued_bytes += appended
        if appended:
            logger.debug("已缓存 Agent 下行 PCM: %s bytes", appended)

    def _write_loop(self) -> None:
        if self._uplink_hold_ms > 0:
            assert self._pcm_enabled_at is not None
            deadline = self._pcm_enabled_at + self._uplink_hold_ms / 1000
            while self._running and not self._first_downlink.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._first_downlink.wait(min(0.02, remaining))
        next_write_at = time.monotonic()
        silence = b"\x00" * self._write_size
        while self._running:
            now = time.monotonic()
            if now < next_write_at:
                time.sleep(min(0.01, next_write_at - now))
                continue

            if self._ready_check is not None and not self._ready_check():
                # 模组上报忙 (+QPCMV:0,0)，本帧不发送，等待就绪。
                next_write_at += self._write_interval
                continue

            with self._write_lock:
                if not self._hangup_silenced:
                    self._write_frame(silence, now)

            next_write_at += self._write_interval

    def _write_frame(self, silence: bytes, now: float) -> None:
        # 调用方持有 _write_lock，覆盖取帧、串口写和观测簿记。
        payload = self._next_write_payload(silence)
        try:
            if self._ser and self._ser.is_open:
                if self._uplink_hold_ms > 0 and not self._uplink_started:
                    self._uplink_started = True
                    assert self._pcm_enabled_at is not None
                    logger.info("PCM_UPLINK_HOLD 开始上行时刻 mono=%.6f elapsed_ms=%.3f reason=%s",
                                now, (now - self._pcm_enabled_at) * 1000,
                                "first_downlink" if self._first_downlink.is_set() else "deadline")
                written = self._ser.write(payload)
                if self._output_observer is not None and written != len(payload):
                    self.output_write_errors += 1
                if written:
                    self._note_output_progress(min(written, self._output_real_bytes))
                    self._notify_output(payload[:written], self._output_ranges)
                self._written_bytes += len(payload)
                self._write_timeouts = 0
                self._log_write_stats()
        except serial.SerialTimeoutException:
            if self._output_observer is not None:
                self.output_write_errors += 1
            # 单帧超时仅丢本帧，继续静音时钟；不能因瞬时忙永久屏蔽上行。
            self._write_timeouts += 1
            if self._write_timeouts == 1 or self._write_timeouts % 50 == 0:
                logger.warning("写入 NMEA PCM 超时，丢弃本帧继续 (累计 %d 次)", self._write_timeouts)
            try:
                if self._ser and self._ser.is_open:
                    self._ser.reset_output_buffer()
            except Exception:
                pass
        except serial.SerialException as exc:
            if self._output_observer is not None:
                self.output_write_errors += 1
            logger.error("写入 NMEA PCM 失败: %s", exc)
            self._running = False

    def _next_write_payload(self, silence: bytes) -> bytes:
        with self._tx_lock:
            if len(self._tx_buffer) >= self._write_size:
                payload = bytes(self._tx_buffer[: self._write_size])
                del self._tx_buffer[: self._write_size]
                self._output_real_bytes = len(payload)
                self._output_ranges = self._take_output_ranges(len(payload))
            elif self._tx_buffer:
                payload = bytes(self._tx_buffer)
                self._tx_buffer.clear()
                self._output_real_bytes = len(payload)
                self._output_ranges = self._take_output_ranges(len(payload))
                payload = payload + silence[: self._write_size - len(payload)]
            else:
                self._output_real_bytes = 0
                self._output_ranges = ()
                return silence
        # 下行盲区埋点（WIL-112）：≥1s 静默后的首个真实帧=新一轮开播进串口。
        # 与「判停→首音频」「端到端」两条日志的时间戳相减，即可归属
        # websocket→闸门→队列→串口 各段耗时。
        now = time.monotonic()
        if now - self._last_real_payload_at > 1.0:
            logger.info("[timing] 下行新一轮首帧进串口")
        self._last_real_payload_at = now
        return payload

    def _log_write_stats(self) -> None:
        now = time.monotonic()
        if now - self._last_stats_at < 5:
            return
        with self._tx_lock:
            buffered = len(self._tx_buffer)
            queued = self._queued_bytes
            self._queued_bytes = 0
        logger.info(
            "NMEA PCM 写入统计: written=%s bytes, agent_queued=%s bytes, buffered=%s bytes",
            self._written_bytes,
            queued,
            buffered,
        )
        self._written_bytes = 0
        self._last_stats_at = now

    @staticmethod
    def modem_to_agent(pcm_8k: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_8k, MODEM_RATE, agent_rate)

    @staticmethod
    def agent_to_modem(pcm_agent: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_agent, agent_rate, MODEM_RATE)

    def amplify_for_modem(self, pcm_8k: bytes) -> bytes:
        return apply_pcm_gain(pcm_8k, self.tx_gain)


class FfmpegAudioBridge(_OutputObserver):
    """经 ffmpeg 子进程与 EG25 UAC 声卡收发 PCM（仅 macOS）。

    macOS 上 PortAudio 打不开 EC20 的 UAC 声卡（AUHAL -66740），但
    AVFoundation（采集）与 AudioToolbox（播放）路径正常，故用两个
    ffmpeg 子进程做搬运：采集→stdout 管道；stdin 管道→播放。
    下行由写线程按 100ms 实时节奏喂给 ffmpeg，pending_output_bytes
    因此能反映真实积压。

    macOS 专属：avfoundation/audiotoolbox 是 ffmpeg 的 macOS-only 设备，
    且设备枚举依赖本项目的 CoreAudio 绑定；其他平台 PortAudio 本身可用，
    直接走 ModemAudioBridge（uac 模式）即可，无需此 workaround。
    """

    # realtime TTS 是 burst 推送（远快于实时），tx_buffer 本就是"快到达、按
    # 100ms 实时放出"的蓄水池，正常长句 pending 峰值可达 10-30s——上限必须远大于
    # 正常 burst，否则会丢正常语音的开头（真机实测 3s 上限把开场白切掉 12.6s）。
    # 它只是写线程僵死时的内存兜底（僵死本身由写超时 ~250ms 检出并重启）。
    _MAX_TX_BUFFER_BYTES = MODEM_RATE * MODEM_CHANNELS * 2 * 60
    _WRITE_DEADLINE_SECONDS = 0.25
    _PLAY_RESTART_DELAY_SECONDS = 0.5
    _PROCESS_STOP_TIMEOUT_SECONDS = 0.5
    _MAX_PLAY_RESTARTS = 20

    def __init__(self, device_keyword: str, tx_gain: float = 1.0) -> None:
        if not platforms.IS_MACOS:
            raise RuntimeError(
                "uac_ffmpeg 音频模式仅支持 macOS（依赖 ffmpeg 的 "
                "avfoundation/audiotoolbox 设备），本平台请改用 MODEM_AUDIO_MODE=uac"
            )
        self.device_keyword = device_keyword
        self.tx_gain = tx_gain
        self.input_index = self._find_avfoundation_input(device_keyword)
        from .coreaudio import find_output_index

        self.output_index = find_output_index(device_keyword)
        if self.input_index is None or self.output_index is None:
            raise RuntimeError(
                f"未找到含 '{device_keyword}' 的 UAC 采集/播放设备，"
                "请检查 EG25 UAC 是否启用 (AT+QPCMV=1,2)"
            )
        self._cap: subprocess.Popen | None = None
        self._play: subprocess.Popen | None = None
        self._tx_buffer = bytearray()
        self._tx_lock = threading.Lock()
        self._writer_thread: threading.Thread | None = None
        self._running = False
        self._dropped_bytes = 0
        self._drop_events = 0
        self._consecutive_play_restarts = 0
        # 上行第三段观测：真实写入 AS（audiotoolbox 播放）的帧统计，
        # 只统计非静音 payload；补零静音单独计次。仅写线程内使用。
        self._write_stats = PcmFlowStats("uplink3_as_write")
        self._silence_writes = 0

    @staticmethod
    def _find_avfoundation_input(keyword: str) -> int | None:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
        in_audio_section = False
        for line in result.stderr.splitlines():
            if "audio devices" in line:
                in_audio_section = True
                continue
            if not in_audio_section:
                continue
            match = re.search(r"\[(\d+)\]\s+(.*)$", line)
            if match and keyword.lower() in match.group(2).lower():
                logger.info("找到 UAC 采集设备 [%s]: %s", match.group(1), match.group(2))
                return int(match.group(1))
        return None

    def _spawn_play(self) -> None:
        """（重）启动下行播放 ffmpeg 进程。

        EC20 的 UAC 输出设备在 AT+QPCMV=1,2 刚启用时往往还没就绪，过早打开会
        AudioQueueStart 失败（-66637）而立即退出。故播放进程独立于此，供 write
        loop 在其退出后带退避重启，直到设备就绪。
        """
        self._play = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "s16le", "-ar", str(MODEM_RATE), "-ac", "1",
             "-i", "pipe:0", "-f", "audiotoolbox",
             "-audio_device_index", str(self.output_index), "none"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        stdin = cast(BinaryIO, self._play.stdin)
        os.set_blocking(stdin.fileno(), False)

    def start(self) -> None:
        common = ["-hide_banner", "-loglevel", "error"]
        self._cap = subprocess.Popen(
            ["ffmpeg", *common, "-f", "avfoundation", "-i", f":{self.input_index}",
             "-f", "s16le", "-ar", str(MODEM_RATE), "-ac", "1", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        # Safe: stdout is non-None because the process is created with stdout=PIPE.
        stdout = cast(BinaryIO, self._cap.stdout)
        os.set_blocking(stdout.fileno(), False)
        self._spawn_play()
        self._running = True
        self._dropped_bytes = 0
        self._drop_events = 0
        self._consecutive_play_restarts = 0
        self._writer_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._writer_thread.start()
        logger.info(
            "ffmpeg UAC 音频桥已启动 (采集 avfoundation:%s → 播放 audiotoolbox:%s)",
            self.input_index, self.output_index,
        )

    def stop(self) -> None:
        self._running = False
        # 先关闭播放管道，立即唤醒可能卡在 select/os.write 的写线程。
        self._terminate_process(self._play)
        if self._writer_thread:
            self._writer_thread.join(timeout=2)
        self._terminate_process(self._cap)
        self._cap = None
        self._play = None
        with self._tx_lock:
            self._take_output_ranges(len(self._tx_buffer))
            self._tx_buffer.clear()

    def read_modem_chunk(self) -> bytes:
        if not self._cap or not self._cap.stdout:
            return b""
        try:
            return self._cap.stdout.read(NMEA_READ_SIZE) or b""
        except (BlockingIOError, ValueError):
            return b""

    def pending_output_bytes(self) -> int:
        with self._tx_lock:
            return len(self._tx_buffer)

    def write_modem_chunks(self, chunks: Iterable[bytes]) -> None:
        dropped = 0
        with self._tx_lock:
            for chunk in chunks:
                if chunk:
                    self._tx_buffer.extend(chunk)
                    self._queue_output_span(chunk)
            overflow = len(self._tx_buffer) - self._MAX_TX_BUFFER_BYTES
            if overflow > 0:
                # PCM 是 int16；从队首丢弃偶数字节，不能把后续样本切到半字边界。
                dropped = overflow + overflow % 2
                del self._tx_buffer[:dropped]
                self._take_output_ranges(dropped)
                self._dropped_bytes += dropped
                self._drop_events += 1
                should_log_drop = self._drop_events == 1 or self._drop_events % 50 == 0
        if dropped and should_log_drop:
            logger.warning(
                "ffmpeg 下行 PCM 积压超限，丢弃最旧音频: dropped=%d total=%d pending=%d",
                dropped,
                self._dropped_bytes,
                self.pending_output_bytes(),
            )

    @classmethod
    def _terminate_process(cls, proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=cls._PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=cls._PROCESS_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                logger.error("ffmpeg 进程强杀后仍未退出")
        except OSError:
            return

    def _drop_stale_tx_buffer(self) -> None:
        with self._tx_lock:
            dropped = len(self._tx_buffer)
            self._take_output_ranges(dropped)
            self._tx_buffer.clear()
            self._dropped_bytes += dropped
        if dropped:
            logger.warning("ffmpeg 播放重启，丢弃陈旧下行 PCM: dropped=%d", dropped)

    def _restart_play(self, reason: str) -> bool:
        if not self._running:
            return False
        if self._consecutive_play_restarts >= self._MAX_PLAY_RESTARTS:
            logger.error(
                "ffmpeg 播放连续失败（已重启 %d 次），下行放弃——"
                "检查 EC20 UAC 输出设备是否被其它 App 占用",
                self._consecutive_play_restarts,
            )
            self._running = False
            return False

        self._consecutive_play_restarts += 1
        logger.warning(
            "ffmpeg 播放%s，%.1fs 后重启（连续第 %d 次）",
            reason,
            self._PLAY_RESTART_DELAY_SECONDS,
            self._consecutive_play_restarts,
        )
        old_play = self._play
        self._play = None
        self._terminate_process(old_play)
        self._drop_stale_tx_buffer()
        if self._PLAY_RESTART_DELAY_SECONDS:
            time.sleep(self._PLAY_RESTART_DELAY_SECONDS)
        if not self._running:
            return False
        self._spawn_play()
        return True

    def _write_play_payload(self, payload: bytes) -> bool:
        """在单帧 deadline 内把 payload 完整写入非阻塞 ffmpeg stdin。"""
        play = self._play
        if play is None or play.stdin is None:
            return False
        try:
            fd = play.stdin.fileno()
        except (OSError, ValueError):
            return False

        deadline = time.monotonic() + self._WRITE_DEADLINE_SECONDS
        view = memoryview(payload)
        written = 0
        while written < len(view):
            if not self._running:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                _, writable, _ = select.select([], [fd], [], remaining)
            except (OSError, ValueError):
                return False
            if not writable:
                continue
            try:
                count = os.write(fd, view[written:])
            except BlockingIOError:
                continue
            except (BrokenPipeError, OSError, ValueError):
                return False
            if count <= 0:
                return False
            written += count
        return True

    def _write_loop(self) -> None:
        """按 100ms 实时节奏喂给播放进程，空闲时也补静音保持 UAC 下行时钟。

        播放进程若退出（多为 QPCMV 刚启用、UAC 输出设备尚未就绪），带退避
        重启重试直到就绪，而非整通哑掉。一旦写成功即清零重启计数。
        """
        next_write_at = time.monotonic()
        silence = b"\x00" * NMEA_WRITE_SIZE
        while self._running:
            now = time.monotonic()
            if now < next_write_at:
                time.sleep(min(0.01, next_write_at - now))
                continue
            # 用 poll() 判定播放进程是否真的退出——不能靠 write 是否抛异常：
            # 进程刚退出时 write 仍可能把数据塞进管道缓冲而“看似成功”，
            # 会误清重启计数、导致无限重试刷屏（曾整通每 0.5s 重启上百次）。
            if self._play is None or self._play.poll() is not None:
                if self._output_observer is not None:
                    self.output_write_errors += 1
                if not self._restart_play("进程退出"):
                    return
                next_write_at = time.monotonic()
                continue
            payload, real_bytes = self._next_write_payload(silence)
            play = self._play
            wrote_full_payload = self._write_play_payload(payload)
            if not wrote_full_payload or play is None or play.poll() is not None:
                if self._output_observer is not None:
                    self.output_write_errors += 1
                if not self._running or not self._restart_play("写入僵死/管道断开"):
                    return
                next_write_at = time.monotonic()
                continue

            self._consecutive_play_restarts = 0
            # 只统计完整写成功的：真实 payload 记帧/峰值，纯静音只计次。
            if real_bytes:
                self._note_output_progress(real_bytes)
                self._notify_output(payload, self._output_ranges)
                self._write_stats.add(payload[:real_bytes])
            else:
                self._silence_writes += 1
            if self._write_stats.maybe_log(
                silence_writes=self._silence_writes,
                play_alive=play.poll() is None,
                pending=self.pending_output_bytes(),
                dropped=self._dropped_bytes,
            ):
                self._silence_writes = 0
            next_write_at += NMEA_WRITE_INTERVAL_SECONDS

    def _next_write_payload(self, silence: bytes) -> tuple[bytes, int]:
        """取下一块待写数据，返回 (payload, 其中真实数据的字节数)。

        真实字节数供写线程区分「转发的上行 PCM」与「保持时钟的补零静音」——
        观测统计只对前者记帧/峰值。
        """
        with self._tx_lock:
            if len(self._tx_buffer) >= NMEA_WRITE_SIZE:
                payload = bytes(self._tx_buffer[:NMEA_WRITE_SIZE])
                del self._tx_buffer[:NMEA_WRITE_SIZE]
                self._output_ranges = self._take_output_ranges(len(payload))
                return payload, len(payload)
            if self._tx_buffer:
                payload = bytes(self._tx_buffer)
                self._tx_buffer.clear()
                self._output_ranges = self._take_output_ranges(len(payload))
                padded = payload + silence[: NMEA_WRITE_SIZE - len(payload)]
                return padded, len(payload)
        self._output_ranges = ()
        return silence, 0

    @staticmethod
    def modem_to_agent(pcm_8k: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_8k, MODEM_RATE, agent_rate)

    @staticmethod
    def agent_to_modem(pcm_agent: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_agent, agent_rate, MODEM_RATE)

    def amplify_for_modem(self, pcm_8k: bytes) -> bytes:
        return apply_pcm_gain(pcm_8k, self.tx_gain)


def create_audio_bridge(
    mode: str,
    device_keyword: str,
    pcm_port: str | None,
    pcm_baudrate: int,
    tx_gain: float = 1.0,
    modem: SerialModem | None = None,
) -> ModemAudioBridge | SerialPcmAudioBridge | FfmpegAudioBridge | BluetoothAudioBridge:
    if getattr(modem, "line_kind", "") == "bluetooth":
        from .bluetooth_modem import BluetoothModem
        assert isinstance(modem, BluetoothModem)
        modem.audio_bridge.tx_gain = tx_gain
        return modem.audio_bridge
    selected = mode.lower()
    if selected == "uac":
        return ModemAudioBridge(device_keyword)
    if selected == "uac_ffmpeg":
        return FfmpegAudioBridge(device_keyword, tx_gain=tx_gain)
    if selected == "nmea":
        if not pcm_port:
            raise RuntimeError("NMEA PCM 模式需要配置 MODEM_PCM_PORT")
        hold_ms = config.get_int("MODEM_PCM_UPLINK_HOLD_MS") if getattr(modem, "vendor", "") == "simcom" else 0
        enable = getattr(modem, "last_voice_enable", None) or {}
        if hold_ms > 0 and enable.get("ok") and enable.get("enabled_mono") is not None:
            return SerialPcmAudioBridge(pcm_port, pcm_baudrate, tx_gain=tx_gain,
                                        uplink_hold_ms=hold_ms, pcm_enabled_at=enable["enabled_mono"])
        return SerialPcmAudioBridge(pcm_port, pcm_baudrate, tx_gain=tx_gain)
    raise ValueError("MODEM_AUDIO_MODE 只能是 uac、uac_ffmpeg（仅 macOS）或 nmea")
