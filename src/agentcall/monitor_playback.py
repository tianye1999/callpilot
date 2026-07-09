"""AI 下行音频镜像到 Mac 扬声器（监听旁路，仅 macOS）。

把发往模组的 Agent 下行 PCM 复制一份，经 ffmpeg audiotoolbox 播放到
本机输出设备（如内置扬声器），方便调试时旁听 AI 在说什么。

监听是纯旁路，永远不能影响通话链路：
- 非 macOS 平台（audiotoolbox 是 ffmpeg 的 macOS-only 输出，设备枚举也
  依赖 CoreAudio 绑定）→ start() log warning 后保持禁用，实例整体 no-op；
- 找不到输出设备 / ffmpeg 起不来 → log warning 后自动禁用，不抛异常；
- ffmpeg 进程中途死亡 / 管道断裂 → log error 后自动禁用；
- ``feed()`` 绝不阻塞：只往有界 deque 追加，满了丢最旧帧（保持低延迟），
  每 50 次丢弃打一条 warning。

播放路径与 :class:`agentcall.audio_bridge.FfmpegAudioBridge` 一致：
``ffmpeg -f s16le -ar {rate} -ac 1 -i pipe:0 -f audiotoolbox
-audio_device_index {idx} none``。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections import deque
from typing import BinaryIO, cast

# 导入模块而非 from-import 常量：让测试能 monkeypatch platforms.IS_MACOS。
from . import platforms
from .audio_bridge import apply_pcm_gain

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}

# 队列以「帧」为单位（一次 feed 即一帧）。24kHz 下 Agent 常见 20~100ms/帧，
# 64 帧 ≈ 1.3~6.4 秒缓冲，对旁听绰绰有余。
DEFAULT_QUEUE_MAXLEN = 64
DEFAULT_DROP_LOG_EVERY = 50
_FEED_WAIT_SECONDS = 0.1


class MonitorPlayback:
    """把 AI 下行 PCM 镜像播放到本机输出设备（仅 macOS）。

    非 macOS 上 start() 只告警不生效，实例保持 no-op——监听是调试旁路，
    平台不支持时静默降级而非阻断通话（调用方无需感知平台差异）。
    """

    def __init__(
        self,
        device_keyword: str,
        *,
        sample_rate: int = 24000,
        gain: float = 1.0,
    ) -> None:
        self.device_keyword = device_keyword
        self.sample_rate = sample_rate
        self.gain = gain
        self._queue_maxlen = max(
            1,
            int(os.environ.get("MONITOR_PLAYBACK_QUEUE_MAXLEN", str(DEFAULT_QUEUE_MAXLEN))),
        )
        self._drop_log_every = max(
            1,
            int(os.environ.get("MONITOR_PLAYBACK_DROP_LOG_EVERY", str(DEFAULT_DROP_LOG_EVERY))),
        )
        self._queue: deque[bytes] = deque(maxlen=self._queue_maxlen)
        self._cond = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._active = False
        self._dropped = 0

    @property
    def active(self) -> bool:
        """监听当前是否可用（start 成功且 ffmpeg 仍存活）。"""
        return self._active

    @property
    def dropped_frames(self) -> int:
        """累计因队列满而丢弃的帧数（观测用）。"""
        return self._dropped

    def start(self) -> None:
        """找输出设备并启动 ffmpeg 播放子进程与喂养线程。

        任何失败（设备不存在、ffmpeg 缺失/起不来）都只 log warning 并
        保持禁用状态，不抛异常。
        """
        if self._active:
            return
        # 平台检查必须在导入 coreaudio 之前：该模块是 macOS 专用绑定。
        if not platforms.IS_MACOS:
            logger.warning(
                "本机监听播放依赖 macOS 的 ffmpeg audiotoolbox，当前平台暂不支持，监听已禁用"
            )
            return
        keyword = (self.device_keyword or "").strip()
        # 设备定位（关键设计）：默认（无关键字）**不指定** -audio_device_index，
        # 让 ffmpeg audiotoolbox 直接播到系统默认输出——彻底摆脱「CoreAudio 序号
        # ↔ ffmpeg 序号」错位、以及本机多虚拟设备导致的序号漂移。只有用户显式指定
        # 设备名时才按名解析出序号并传入。
        dev_args: list[str] = []
        target = "系统默认输出"
        if keyword:
            from .coreaudio import find_output_index  # 延迟导入（macOS 专用绑定）
            try:
                output_index = find_output_index(keyword)
            except Exception as exc:  # noqa: BLE001
                logger.warning("枚举 CoreAudio 输出设备失败: %s，监听已禁用", exc)
                return
            if output_index is None:
                logger.warning("未找到含 '%s' 的输出设备，监听已禁用", keyword)
                return
            dev_args = ["-audio_device_index", str(output_index)]
            target = f"'{keyword}'(#{output_index})"
        try:
            self._proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "s16le", "-ar", str(self.sample_rate), "-ac", "1",
                 "-i", "pipe:0", "-f", "audiotoolbox", *dev_args, "none"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动监听 ffmpeg 失败: %s，监听已禁用", exc)
            self._proc = None
            return
        self._dropped = 0
        self._running = True
        self._active = True
        self._thread = threading.Thread(
            target=self._feed_loop, daemon=True, name="monitor-playback"
        )
        self._thread.start()
        logger.info(
            "监听播放已启动 (→%s, %dHz mono, gain=%.2f)",
            target, self.sample_rate, self.gain,
        )

    def feed(self, pcm: bytes) -> None:
        """入队一帧下行 PCM，绝不阻塞（高频调用安全）。

        队列满时丢弃最旧帧保持低延迟。热路径只递增计数——app 挂了
        FileHandler，logger 调用等价于磁盘 IO，日志移到播放线程打。
        """
        if not self._active or not pcm:
            return
        with self._cond:
            if len(self._queue) >= self._queue_maxlen:
                self._dropped += 1
            self._queue.append(pcm)  # maxlen deque：满时自动挤掉最旧帧
            self._cond.notify()

    def stop(self) -> None:
        """停止监听并回收子进程/线程，可重复调用（幂等）。"""
        self._running = False
        self._active = False
        with self._cond:
            self._queue.clear()
            self._cond.notify_all()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._terminate_proc()

    # ---- 内部 ----

    def _feed_loop(self) -> None:
        """后台线程：从队列取帧，加增益后写入 ffmpeg stdin。

        丢帧日志在这里低频打（feed 热路径只计数，见 feed 注释）。
        """
        logged_dropped = 0
        while self._running:
            with self._cond:
                while self._running and not self._queue:
                    self._cond.wait(timeout=_FEED_WAIT_SECONDS)
                if not self._running:
                    return
                pcm = self._queue.popleft()
                dropped = self._dropped
            if dropped - logged_dropped >= self._drop_log_every or (
                dropped and not logged_dropped
            ):
                logger.warning("监听队列曾满，累计丢弃 %d 帧", dropped)
                logged_dropped = dropped
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is not None:
                logger.error(
                    "监听 ffmpeg 播放进程已退出 (returncode=%s)，自动禁用监听",
                    proc.returncode,
                )
                self._disable_from_thread()
                return
            try:
                # Safe: stdin is non-None because the process is created with stdin=PIPE.
                stdin = cast(BinaryIO, proc.stdin)
                stdin.write(apply_pcm_gain(pcm, self.gain))
                stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                logger.error("写入监听 ffmpeg 管道失败: %s，自动禁用监听", exc)
                self._disable_from_thread()
                return

    def _disable_from_thread(self) -> None:
        """喂养线程内部的自禁用：不 join 自己，只收进程清队列。"""
        self._active = False
        self._running = False
        with self._cond:
            self._queue.clear()
        self._terminate_proc()

    def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def create_monitor_playback() -> MonitorPlayback | None:
    """按环境变量创建监听实例；未启用时返回 None。

    env 名与 config 注册表一致（MONITOR_AI_PLAYBACK / MONITOR_OUTPUT_DEVICE /
    MONITOR_AI_GAIN），避免两套名字误导调用方。
    """
    enabled = os.environ.get("MONITOR_AI_PLAYBACK", "false").strip().lower()
    if enabled not in _TRUE_VALUES:
        return None
    return MonitorPlayback(
        os.environ.get("MONITOR_OUTPUT_DEVICE", ""),  # 空 = 系统默认输出
        sample_rate=int(os.environ.get("MONITOR_PLAYBACK_RATE", "24000")),
        gain=float(os.environ.get("MONITOR_AI_GAIN", "1.0")),
    )
