"""8kHz 模组音频 ↔ AI 音频格式桥接。"""

from __future__ import annotations

import logging
import os
import re
import select
import subprocess
import threading
import time
from typing import Any, BinaryIO, Callable, Iterable, cast

import numpy as np
import serial

# 导入模块而非 from-import 常量：让测试能 monkeypatch platforms.IS_MACOS。
from . import platforms
from .pcm_stats import PcmFlowStats

logger = logging.getLogger(__name__)

MODEM_RATE = 8000
MODEM_CHANNELS = 1
MODEM_DTYPE = "int16"
MODEM_BLOCK_MS = 20
NMEA_READ_SIZE = 640
NMEA_WRITE_SIZE = 1600
NMEA_WRITE_INTERVAL_SECONDS = 0.1


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


class ModemAudioBridge:
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


class FfmpegAudioBridge:
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
            overflow = len(self._tx_buffer) - self._MAX_TX_BUFFER_BYTES
            if overflow > 0:
                # PCM 是 int16；从队首丢弃偶数字节，不能把后续样本切到半字边界。
                dropped = overflow + overflow % 2
                del self._tx_buffer[:dropped]
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
                if not self._restart_play("进程退出"):
                    return
                next_write_at = time.monotonic()
                continue
            payload, real_bytes = self._next_write_payload(silence)
            play = self._play
            wrote_full_payload = self._write_play_payload(payload)
            if not wrote_full_payload or play is None or play.poll() is not None:
                if not self._running or not self._restart_play("写入僵死/管道断开"):
                    return
                next_write_at = time.monotonic()
                continue

            self._consecutive_play_restarts = 0
            # 只统计完整写成功的：真实 payload 记帧/峰值，纯静音只计次。
            if real_bytes:
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
                return payload, len(payload)
            if self._tx_buffer:
                payload = bytes(self._tx_buffer)
                self._tx_buffer.clear()
                padded = payload + silence[: NMEA_WRITE_SIZE - len(payload)]
                return padded, len(payload)
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
) -> "ModemAudioBridge | SerialPcmAudioBridge | FfmpegAudioBridge":
    selected = mode.lower()
    if selected == "uac":
        return ModemAudioBridge(device_keyword)
    if selected == "uac_ffmpeg":
        return FfmpegAudioBridge(device_keyword, tx_gain=tx_gain)
    if selected == "nmea":
        if not pcm_port:
            raise RuntimeError("NMEA PCM 模式需要配置 MODEM_PCM_PORT")
        return SerialPcmAudioBridge(pcm_port, pcm_baudrate, tx_gain=tx_gain)
    raise ValueError("MODEM_AUDIO_MODE 只能是 uac、uac_ffmpeg（仅 macOS）或 nmea")
