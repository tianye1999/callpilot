"""8kHz 模组音频 ↔ AI 音频格式桥接。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

import numpy as np
import serial
import sounddevice as sd

logger = logging.getLogger(__name__)

MODEM_RATE = 8000
MODEM_CHANNELS = 1
MODEM_DTYPE = "int16"
MODEM_BLOCK_MS = 20
NMEA_READ_SIZE = 640
NMEA_WRITE_SIZE = 1600
NMEA_WRITE_INTERVAL_SECONDS = 0.1


def find_device_index(keyword: str, kind: str | None = None) -> int | None:
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


class ModemAudioBridge:
    """在 EG25 USB 声卡与 Agent 之间转发 PCM 音频。"""

    def __init__(self, device_keyword: str) -> None:
        self.input_device_index = find_device_index(device_keyword, "input")
        self.output_device_index = find_device_index(device_keyword, "output")
        if self.input_device_index is None or self.output_device_index is None:
            raise RuntimeError(
                f"未找到包含 '{device_keyword}' 的 UAC 输入/输出设备，请检查 EG25 UAC 是否启用"
            )
        self._input_stream: sd.RawInputStream | None = None
        self._output_stream: sd.RawOutputStream | None = None
        self._block_size = int(MODEM_RATE * MODEM_BLOCK_MS / 1000)

    def start(self) -> None:
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
                self._output_stream.write(chunk)

    @staticmethod
    def modem_to_agent(pcm_8k: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_8k, MODEM_RATE, agent_rate)

    @staticmethod
    def agent_to_modem(pcm_agent: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_agent, agent_rate, MODEM_RATE)


class SerialPcmAudioBridge:
    """通过 EG25 USB NMEA 口传输 Voice over USB PCM。"""

    def __init__(self, port: str, baudrate: int = 921600, tx_gain: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.tx_gain = tx_gain
        self._ready_check: "Callable[[], bool] | None" = None
        self._ser: serial.Serial | None = None
        self._tx_buffer = bytearray()
        self._tx_lock = threading.Lock()
        self._writer_thread: threading.Thread | None = None
        self._running = False
        self._written_bytes = 0
        self._queued_bytes = 0
        self._last_stats_at = 0.0
        self._write_timeouts = 0

    def start(self) -> None:
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=0.02,
            write_timeout=0.2,
        )
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
        self._running = True
        self._written_bytes = 0
        self._queued_bytes = 0
        self._write_timeouts = 0
        self._last_stats_at = time.monotonic()
        self._writer_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._writer_thread.start()
        logger.info(
            "NMEA PCM 音频流已启动: %s (8kHz mono, tx_gain=%.2f)",
            self.port,
            self.tx_gain,
        )

    def stop(self) -> None:
        self._running = False
        if self._writer_thread:
            self._writer_thread.join(timeout=2)
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None
        with self._tx_lock:
            self._tx_buffer.clear()

    def read_modem_chunk(self) -> bytes:
        if not self._ser:
            return b""
        return self._ser.read(NMEA_READ_SIZE)

    def pending_output_bytes(self) -> int:
        with self._tx_lock:
            return len(self._tx_buffer)

    def set_ready_check(self, ready_check: Callable[[], bool]) -> None:
        """注入上行流控判断：返回 False 时暂停向模组写 PCM。"""
        self._ready_check = ready_check

    def write_modem_chunks(self, chunks: Iterable[bytes]) -> None:
        if not self._ser:
            return
        appended = 0
        with self._tx_lock:
            for chunk in chunks:
                if chunk:
                    self._tx_buffer.extend(chunk)
                    appended += len(chunk)
            self._queued_bytes += appended
        if appended:
            logger.debug("已缓存 Agent 下行 PCM: %s bytes", appended)

    def _write_loop(self) -> None:
        next_write_at = time.monotonic()
        silence = b"\x00" * NMEA_WRITE_SIZE
        while self._running:
            now = time.monotonic()
            if now < next_write_at:
                time.sleep(min(0.01, next_write_at - now))
                continue

            if self._ready_check is not None and not self._ready_check():
                # 模组上报忙 (+QPCMV:0,0)，本帧不发送，等待就绪。
                next_write_at += NMEA_WRITE_INTERVAL_SECONDS
                continue

            payload = self._next_write_payload(silence)
            try:
                if self._ser and self._ser.is_open:
                    self._ser.write(payload)
                    self._written_bytes += len(payload)
                    self._write_timeouts = 0
                    self._log_write_stats()
            except serial.SerialTimeoutException:
                # 单帧写超时（模组侧瞬时忙/流控）：丢弃本帧并继续，绝不终止音频线程。
                # 否则写线程一旦退出，下行永远没声音，且 tx_buffer 排不空会永久屏蔽上行。
                self._write_timeouts += 1
                if self._write_timeouts == 1 or self._write_timeouts % 50 == 0:
                    logger.warning(
                        "写入 NMEA PCM 超时，丢弃本帧继续 (累计 %d 次)", self._write_timeouts
                    )
                try:
                    if self._ser and self._ser.is_open:
                        self._ser.reset_output_buffer()
                except Exception:
                    pass
            except serial.SerialException as exc:
                logger.error("写入 NMEA PCM 失败: %s", exc)
                self._running = False
                break

            next_write_at += NMEA_WRITE_INTERVAL_SECONDS

    def _next_write_payload(self, silence: bytes) -> bytes:
        with self._tx_lock:
            if len(self._tx_buffer) >= NMEA_WRITE_SIZE:
                payload = bytes(self._tx_buffer[:NMEA_WRITE_SIZE])
                del self._tx_buffer[:NMEA_WRITE_SIZE]
                return payload
            if self._tx_buffer:
                payload = bytes(self._tx_buffer)
                self._tx_buffer.clear()
                return payload + silence[: NMEA_WRITE_SIZE - len(payload)]
        return silence

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


def create_audio_bridge(
    mode: str,
    device_keyword: str,
    pcm_port: str | None,
    pcm_baudrate: int,
    tx_gain: float = 1.0,
) -> ModemAudioBridge | SerialPcmAudioBridge:
    selected = mode.lower()
    if selected == "uac":
        return ModemAudioBridge(device_keyword)
    if selected == "nmea":
        if not pcm_port:
            raise RuntimeError("NMEA PCM 模式需要配置 MODEM_PCM_PORT")
        return SerialPcmAudioBridge(pcm_port, pcm_baudrate, tx_gain=tx_gain)
    raise ValueError("MODEM_AUDIO_MODE 只能是 uac 或 nmea")
