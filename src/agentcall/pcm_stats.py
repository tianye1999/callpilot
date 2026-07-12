"""音频链路周期统计（隐私安全：只记帧数/字节/峰值等数字，不落任何 PCM 内容）。

用途：二分「链路已建立但无声」类故障——上行各段（LiveKit 入站 → Edge 泵取走 →
ffmpeg AS 写入）各挂一个实例，对照各段的 5s 统计日志即可定位断流 / 全零
发生在哪一段，而不必录下音频内容。
"""

from __future__ import annotations

import logging
import time
from array import array
from collections.abc import Callable

logger = logging.getLogger(__name__)


class PcmFlowStats:
    """按固定窗口聚合一段 PCM 流的 frames/bytes/peak 并输出一行日志。

    - ``add()`` 每帧调用，峰值按 s16le 采样绝对值取最大——只留数字，不留内容；
    - ``maybe_log()`` 供高频循环（如 10ms 泵）反复调用，窗口未到期是 no-op；
      到期即输出并复位。**即使整窗一帧未到（add 从未被调）也会按期打出
      frames=0**——这正是断流定位需要的信号。
    - 无锁，跨线程不可共享。同一 event loop 内多个 task 同步访问是安全的
      （所有方法内部无 await，不会交错）——uplink1 即 add 与 maybe_log
      分属两个 task 的用法。
    """

    def __init__(
        self,
        label: str,
        interval_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.label = label
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._window_started = clock()
        self._frames = 0
        self._bytes = 0
        self._peak = 0

    def add(self, pcm: bytes) -> None:
        self._frames += 1
        self._bytes += len(pcm)
        usable = len(pcm) - (len(pcm) % 2)
        if usable:
            samples = array("h", pcm[:usable])
            peak = max(max(samples), -min(samples))
            if peak > self._peak:
                self._peak = peak

    def due(self) -> bool:
        """窗口是否已到期。取 extra 状态有成本（如拿锁）时先用它判断。"""
        return self._clock() - self._window_started >= self.interval_seconds

    def maybe_log(self, **extra: object) -> bool:
        """窗口到期则输出统计并复位，返回 True；未到期返回 False。

        ``extra`` 里的键值原样附在日志尾部（如 queued/pending/play_alive），
        供调用方带上队列深度等瞬时状态。
        """
        now = self._clock()
        elapsed = now - self._window_started
        if elapsed < self.interval_seconds:
            return False
        detail = "".join(f" {key}={value}" for key, value in extra.items())
        logger.info(
            "[audio-stats] %s %.1fs: frames=%d bytes=%d peak=%d%s",
            self.label,
            elapsed,
            self._frames,
            self._bytes,
            self._peak,
            detail,
        )
        self._window_started = now
        self._frames = 0
        self._bytes = 0
        self._peak = 0
        return True


__all__ = ["PcmFlowStats"]
