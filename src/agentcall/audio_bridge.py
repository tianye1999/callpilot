"""模组 PCM（8k/16k）↔ AI 音频格式桥接。"""

from __future__ import annotations

import errno
import logging
import math
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

# macOS PTY 只接受 ≤230400；PCM 走桥出的 PTY 时用它兜底(见 _open_serial)。
PTY_SAFE_BAUDRATE = 115200

# 伪终端设备名：macOS 是 /dev/ttysNNN，Linux 是 /dev/pts/N。
_PTY_NAME_RE = re.compile(r"^/dev/(ttys\d+|pts/\d+)$")


def _is_pty(port: str) -> bool:
    """``port`` 是否是伪终端（软链接会先解析）。

    真串口（``/dev/cu.*``、``COMn``）返回 False——那里的波特率有物理意义，
    绝不能替用户降速。判不出来时一律返回 False，把决定权留给 open 的兜底。
    """
    try:
        resolved = os.path.realpath(port)
    except OSError:
        return False
    return bool(_PTY_NAME_RE.match(resolved))

# 模组 PCM 采样率：默认窄带 8k；宽带 16k 由 configure_modem_rate() /
# MODEM_PCM_RATE 在进程启动时切换（须与 AT+CPCMBANDWIDTH 一致）。
SUPPORTED_MODEM_RATES = (8000, 16000)
MODEM_RATE = 8000
MODEM_CHANNELS = 1
MODEM_DTYPE = "int16"
MODEM_BLOCK_MS = 20
NMEA_WRITE_INTERVAL_SECONDS = 0.1
# SIM7600 官方 Linux 示例从麦克风回调每 20ms 向 audio 串口写一次。
SIMCOM_WRITE_INTERVAL_SECONDS = 0.02
NMEA_READ_SIZE = 640
NMEA_WRITE_SIZE = 1600  # = MODEM_RATE * 0.1 * 2 @8k；configure 时重算
SIMCOM_WRITE_SIZE = 320  # = MODEM_RATE * 0.02 * 2 @8k；configure 时重算
# SIM7600 的 USB Audio 在端点刚恢复、PTY flush 或短写边界处，偶尔会让
# 16-bit PCM 从高字节开始。此时正常几百幅值的人声会瞬间变成接近满幅的
# 宽带噪音；把字节流再错开 1 byte 后会恢复。只在 SIMCom 路径启用这一
# 保守检测，避免改变历史 NMEA/Quectel 行为。
SIMCOM_REALIGN_MIN_RMS = 6000.0
SIMCOM_REALIGN_MAX_ALTERNATE_RMS = 3000.0
SIMCOM_REALIGN_IMPROVEMENT_RATIO = 0.25
SIMCOM_STARTUP_GUARD_SECONDS = 2.5


def configure_modem_rate(rate: int) -> int:
    """设置进程内模组 PCM 采样率，并同步写帧字节数。

    必须在创建音频桥 / 通话前调用，且与模组 ``AT+CPCMBANDWIDTH`` 一致；
    否则会出现「16k 流按 8k 解 → 宽带噪声」或吞吐对不上。
    """
    global MODEM_RATE, NMEA_READ_SIZE, NMEA_WRITE_SIZE, SIMCOM_WRITE_SIZE
    if rate not in SUPPORTED_MODEM_RATES:
        raise ValueError(
            f"MODEM_PCM_RATE 仅支持 {SUPPORTED_MODEM_RATES}，收到: {rate}"
        )
    MODEM_RATE = rate
    bytes_per_sec = rate * MODEM_CHANNELS * 2
    NMEA_READ_SIZE = max(640, int(bytes_per_sec * 0.04))  # ~40ms
    NMEA_WRITE_SIZE = int(bytes_per_sec * NMEA_WRITE_INTERVAL_SECONDS)
    SIMCOM_WRITE_SIZE = int(bytes_per_sec * SIMCOM_WRITE_INTERVAL_SECONDS)
    logger.info(
        "模组 PCM 采样率已配置: %dHz (simcom_frame=%dB/%.0fms, nmea_frame=%dB/%.0fms)",
        MODEM_RATE,
        SIMCOM_WRITE_SIZE,
        SIMCOM_WRITE_INTERVAL_SECONDS * 1000,
        NMEA_WRITE_SIZE,
        NMEA_WRITE_INTERVAL_SECONDS * 1000,
    )
    return MODEM_RATE


def phone_passband_hz(sample_rate: int | None = None) -> float:
    """电话有效通带上沿：窄带 ~3.4kHz，宽带(AMR-WB) ~7kHz。"""
    rate = MODEM_RATE if sample_rate is None else sample_rate
    return 3400.0 if rate <= 8000 else 7000.0


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


def _design_phone_lpf(src_rate: int, dst_rate: int, *, taps: int = 127) -> np.ndarray:
    cutoff_hz = min(phone_passband_hz(dst_rate), dst_rate * 0.45)
    center = (taps - 1) / 2
    positions = np.arange(taps, dtype=np.float64) - center
    normalized_cutoff = cutoff_hz / src_rate
    kernel = (
        2.0
        * normalized_cutoff
        * np.sinc(2.0 * normalized_cutoff * positions)
        * np.hamming(taps)
    )
    kernel /= np.sum(kernel)
    return kernel


class StreamingPcmDownsampler:
    """有状态的 mono PCM16 整数倍降采样器。

    Realtime 服务会把一段连续语音拆成大小不固定的 WebSocket delta。旧实现对
    每个 delta 单独 ``np.interp``：既没有在电话 Nyquist 前做低通，又会在
    每个 delta 重新开始采样相位。24kHz 原音在浏览器旁听正常，但高于电话带宽
    的能量会折叠进基带，块边界还可能产生 click。

    这里在源采样率上先做电话通带低通，再保持跨 delta 的抽取相位。
    非整数倍（如 24k→16k）见 ``StreamingPcmRationalResampler``。
    """

    _FILTER_TAPS = 127

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        if src_rate <= dst_rate or src_rate % dst_rate:
            raise ValueError("StreamingPcmDownsampler 仅支持整数倍降采样")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.factor = src_rate // dst_rate
        self._taps = _design_phone_lpf(src_rate, dst_rate, taps=self._FILTER_TAPS)
        self._history = np.zeros(len(self._taps) - 1, dtype=np.float64)
        self._phase = 0
        self._byte_carry = b""
        self._lock = threading.Lock()

    def process(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        with self._lock:
            raw = self._byte_carry + pcm
            aligned = len(raw) - len(raw) % 2
            self._byte_carry = raw[aligned:]
            if aligned <= 0:
                return b""
            samples = np.frombuffer(raw[:aligned], dtype="<i2").astype(np.float64)
            combined = np.concatenate((self._history, samples))
            filtered = np.convolve(combined, self._taps, mode="valid")
            output = filtered[self._phase :: self.factor]
            self._phase = (self._phase - len(filtered)) % self.factor
            self._history = combined[-(len(self._taps) - 1) :]
        encoded = np.clip(np.rint(output), -32768, 32767).astype("<i2")
        return encoded.tobytes()


class StreamingPcmRationalResampler:
    """非整数倍降采样（典型：Agent 24kHz → 模组 16kHz）。

    先 FIR 低通，再按连续分数相位线性插值，避免块边界相位重置。
    """

    _FILTER_TAPS = 127

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        if src_rate <= dst_rate:
            raise ValueError("StreamingPcmRationalResampler 仅支持降采样")
        if src_rate % dst_rate == 0:
            raise ValueError("整数倍请用 StreamingPcmDownsampler")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.ratio = src_rate / dst_rate
        self._taps = _design_phone_lpf(src_rate, dst_rate, taps=self._FILTER_TAPS)
        self._history = np.zeros(len(self._taps) - 1, dtype=np.float64)
        self._filtered = np.zeros(0, dtype=np.float64)
        self._pos = 0.0
        self._byte_carry = b""
        self._lock = threading.Lock()

    def process(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        with self._lock:
            raw = self._byte_carry + pcm
            aligned = len(raw) - len(raw) % 2
            self._byte_carry = raw[aligned:]
            if aligned <= 0:
                return b""
            samples = np.frombuffer(raw[:aligned], dtype="<i2").astype(np.float64)
            combined = np.concatenate((self._history, samples))
            filtered = np.convolve(combined, self._taps, mode="valid")
            self._history = combined[-(len(self._taps) - 1) :]
            self._filtered = np.concatenate((self._filtered, filtered))
            out: list[float] = []
            last_index = len(self._filtered) - 1
            while self._pos < last_index:
                idx = int(self._pos)
                frac = self._pos - idx
                left = self._filtered[idx]
                right = self._filtered[idx + 1]
                out.append(left + (right - left) * frac)
                self._pos += self.ratio
            keep_from = max(0, int(self._pos) - 1)
            self._pos -= keep_from
            self._filtered = self._filtered[keep_from:]
        if not out:
            return b""
        encoded = np.clip(np.rint(np.asarray(out)), -32768, 32767).astype("<i2")
        return encoded.tobytes()


def _agent_chunk_to_modem(
    bridge: Any,
    pcm_agent: bytes,
    agent_rate: int,
) -> bytes:
    """按 bridge 实例保持 Agent→电话降采样状态。"""
    if not pcm_agent or agent_rate == MODEM_RATE:
        return pcm_agent
    if agent_rate <= MODEM_RATE:
        return resample_pcm(pcm_agent, agent_rate, MODEM_RATE)

    resampler = getattr(bridge, "_downlink_resampler", None)
    integer = agent_rate % MODEM_RATE == 0
    want_type = StreamingPcmDownsampler if integer else StreamingPcmRationalResampler
    if (
        resampler is None
        or not isinstance(resampler, want_type)
        or resampler.src_rate != agent_rate
        or resampler.dst_rate != MODEM_RATE
    ):
        resampler = want_type(agent_rate, MODEM_RATE)
        bridge._downlink_resampler = resampler
        logger.info(
            "Agent 下行流式重采样已启用: %dHz -> %dHz "
            "(passband=%.0fHz, %s)",
            agent_rate,
            MODEM_RATE,
            phone_passband_hz(MODEM_RATE),
            "integer-decimate" if integer else "rational-interp",
        )
    return cast(Any, resampler).process(pcm_agent)


def apply_pcm_gain(pcm: bytes, gain: float) -> bytes:
    if gain == 1.0 or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    amplified = np.clip(samples * gain, -32768, 32767)
    return amplified.astype(np.int16).tobytes()


def apply_phone_clarity(pcm: bytes, *, sample_rate: int | None = None) -> bytes:
    """电话下行清晰度：去泥 + 抬辅音区，减轻「隔着木板」。

    顺序：一阶高通去掉 <~180Hz 闷泥 → 中等预加重抬通带上沿。
    仍弱于经典 0.85 全量预加重（那版刺耳/泵感）；不做全程 tanh。

    等效响应是 ``H(z) = 1 - 0.58*pre_coef * z^-1``（见下面的混合），刻度按
    **3.4kHz 相对 300Hz 抬多少 dB** 来记比按系数记直观：

    | pre_coef | 倾斜 |
    |---|---|
    | 0.65 | 6.47dB — 2026-08-10 为治「闷」提上来的，真机实听**刺耳** |
    | 0.35 | 3.37dB — 当前值 |
    | 0.20 | 1.90dB — 已低于单测要求的 +25%，再降就等于没做 |

    0.65 那次调高是在模组端点退化期做的，当时的「闷」与断续同源（见文档 §5.3），
    模组恢复后就显得过亮了。**改这个系数会同时改变总电平**（低频衰减跟着变，
    0.65→0.35 总电平 +1.63dB），调完必须用 ``MODEM_TX_GAIN`` 补回去，
    否则音色和响度两个变量一起动，听感归因不了。
    """
    if not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if x.size == 0:
        return pcm
    rate = MODEM_RATE if sample_rate is None else sample_rate
    # 一阶 HP：α ≈ exp(-2π·180/fs)；8k≈0.87，16k≈0.93
    alpha = float(np.exp(-2.0 * np.pi * 180.0 / rate))
    dx = np.empty_like(x)
    dx[0] = 0.0
    dx[1:] = x[1:] - x[:-1]
    hp = np.empty_like(x)
    acc = 0.0
    for i, step in enumerate(dx):
        acc = alpha * (acc + step)
        hp[i] = acc
    pre_coef = 0.35 if rate <= 8000 else 0.30
    pre = np.empty_like(hp)
    pre[0] = hp[0]
    pre[1:] = hp[1:] - pre_coef * hp[:-1]
    out = 0.42 * hp + 0.58 * pre
    return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()


class PhoneAgc:
    """下行动态范围压缩 + 自动增益（流式，跨块保持状态）。

    窄带电话里句尾、轻辅音常掉到听阈以下，而静态 ``MODEM_TX_GAIN`` 只能在
    「削顶」和「听不清」之间二选一。这里按 ~5ms 子块跟踪 RMS，做标准的
    下压式压缩：超过 ``threshold_dbfs`` 的部分按 ``ratio`` 压回来，然后叠一个
    静态补偿增益 ``makeup``（= target - threshold）把整体抬回目标电平。
    ``threshold_dbfs`` 默认跟着 target 走（低 10dB），否则 target 旋钮在自己
    的量程里有一大截是死的（threshold 写死时 target ≤ threshold 就 makeup=0）。

    抬轻音靠的是 makeup 而不是「慢释放慢慢爬」——句尾辅音只有几十毫秒，
    等释放爬上来早就过去了（第一版这么写，实测只压不抬）。

    ``gate_dbfs`` 以下**冻结**增益而不是归零：既不在每个词头因为增益从 0 重新
    爬升而把起音削软，也不会主动继续抬底噪（但冻结值本身仍会作用在底噪上）。
    块间对增益线性插值以免台阶/咔哒。

    每通电话新建一个实例：增益状态不该跨通继承。
    """

    BLOCK_MS = 5.0
    # 增益后允许的块内峰值上限：留一点余量，让下游 apply_soft_limit 还有得救。
    PEAK_CEILING = 0.99

    def __init__(
        self,
        sample_rate: int,
        *,
        target_dbfs: float = -18.0,
        threshold_dbfs: float | None = None,
        ratio: float = 3.0,
        attack_ms: float = 6.0,
        # attack 必须快：放慢到 40ms 起，真机录音上峰值就顶到 32767。
        release_ms: float = 120.0,
        max_boost_db: float = 12.0,
        gate_dbfs: float = -52.0,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"PhoneAgc 采样率非法: {sample_rate}")
        if ratio < 1.0:
            raise ValueError(f"PhoneAgc ratio 必须 >=1，收到: {ratio}")
        self.sample_rate = sample_rate
        self.target_dbfs = float(target_dbfs)
        self.threshold_dbfs = (
            self.target_dbfs - 10.0 if threshold_dbfs is None else float(threshold_dbfs)
        )
        self.gate_dbfs = float(gate_dbfs)
        self.makeup_db = min(
            max(self.target_dbfs - self.threshold_dbfs, 0.0), float(max_boost_db)
        )
        self._block = max(1, int(round(sample_rate * self.BLOCK_MS / 1000.0)))
        # 超阈部分压掉 (1 - 1/ratio)，ratio=3 即压掉 2/3。
        self._slope = 1.0 - 1.0 / float(ratio)
        block_ms = self._block * 1000.0 / float(sample_rate)
        # 增益下行(信号变响)走 attack，上行(信号变轻)走 release：单极点平滑。
        self._attack = float(np.exp(-block_ms / max(attack_ms, 0.1)))
        self._release = float(np.exp(-block_ms / max(release_ms, 0.1)))
        self._ramp = (np.arange(self._block, dtype=np.float64) + 1.0) / self._block
        self._lock = threading.Lock()
        # 从 makeup 起步而不是 0dB：稳态增益就是 makeup，从 0 起会让每通开场白
        # 被 release 拖出约 500ms 的渐强（真机实测 0~100ms 段低 3.6dB）。
        self._gain_db = self.makeup_db
        self._last_gain = float(np.power(10.0, self.makeup_db / 20.0))

    def process(self, pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
        if x.size == 0:
            return pcm
        block = self._block
        blocks = -(-x.size // block)
        pad = blocks * block - x.size
        padded = np.concatenate([x, np.zeros(pad)]) if pad else x
        framed = padded.reshape(blocks, block)
        # 尾块的补零只是为了 reshape：算 RMS 时必须按真实样本数取平均，否则
        # 电平被稀释→增益虚高，块长不整除时每个 chunk 边界都抖一下。
        counts = np.full(blocks, block, dtype=np.float64)
        counts[-1] -= pad
        rms = np.sqrt(np.sum(np.square(framed), axis=1) / counts)
        level_db = 20.0 * np.log10(np.maximum(rms, 1e-9))
        # 每块增益天花板：本块峰值乘增益不得越过 PEAK_CEILING。
        peaks = np.maximum(np.max(np.abs(framed), axis=1), 1e-9)
        ceiling_db = 20.0 * np.log10(self.PEAK_CEILING / peaks)

        with self._lock:
            gain_db = np.empty(blocks, dtype=np.float64)
            smoothed = self._gain_db
            for i in range(blocks):
                level = level_db[i]
                if level < self.gate_dbfs:
                    desired = smoothed  # 门限下冻结，见类注释
                else:
                    over = level - self.threshold_dbfs
                    compress = -over * self._slope if over > 0.0 else 0.0
                    desired = compress + self.makeup_db
                coef = self._attack if desired < smoothed else self._release
                smoothed = coef * smoothed + (1.0 - coef) * desired
                gain_db[i] = smoothed
            self._gain_db = smoothed

            # 前瞻限幅：块内插值是从「上一块的增益」爬到「本块的增益」，所以
            # 上一块也不能超过本块的天花板，否则响音起头的前几个样本会硬削。
            # 硬削发生在 AGC 内部时，下游 apply_soft_limit 已经救不回来了。
            np.minimum(gain_db, ceiling_db, out=gain_db)
            for i in range(blocks - 1, 0, -1):
                if gain_db[i - 1] > ceiling_db[i]:
                    gain_db[i - 1] = ceiling_db[i]

            linear = np.power(10.0, gain_db / 20.0)
            prev = np.concatenate(
                [[min(self._last_gain, float(np.power(10.0, ceiling_db[0] / 20.0)))],
                 linear[:-1]]
            )
            ramp = (prev[:, None] + (linear - prev)[:, None] * self._ramp).reshape(-1)
            # 记真正作用在最后一个「发出去的」样本上的增益：补零被截掉后，
            # linear[-1] 是块末值而非实际用到的值，直接沿用会在下个 chunk 起头跳一下。
            self._last_gain = float(ramp[x.size - 1])

        y = (padded * ramp)[: x.size] * 32768.0
        return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()


# 下行 AGC 的进程内开关，由 configure_downlink_agc() 在启动时按配置写入；
# 音频桥在构造时据此决定是否挂 PhoneAgc（不在此模块读 config，避免环依赖）。
DOWNLINK_AGC_ENABLED = True
DOWNLINK_AGC_TARGET_DBFS = -18.0


def configure_downlink_agc(enabled: bool, target_dbfs: float) -> None:
    """设置进程内下行 AGC 开关与目标电平；须在创建音频桥前调用。"""
    global DOWNLINK_AGC_ENABLED, DOWNLINK_AGC_TARGET_DBFS
    DOWNLINK_AGC_ENABLED = bool(enabled)
    target = float(target_dbfs)
    # NaN 不能靠下面的 min/max 拦住（与 NaN 的比较恒为 False，会一路穿过去），
    # 而 NaN 增益会让整通下行变成数字静音。config.get_float 只挡 ValueError。
    if not math.isfinite(target):
        logger.warning("MODEM_AGC_TARGET_DBFS 非有限值(%r)，回落默认 -18.0", target_dbfs)
        target = -18.0
    # 目标电平钳在合理区间：太高必然常驻限幅，太低等于没开。
    DOWNLINK_AGC_TARGET_DBFS = min(max(target, -40.0), -6.0)
    logger.info(
        "下行 AGC: %s (target=%.1f dBFS)",
        "开启" if DOWNLINK_AGC_ENABLED else "关闭",
        DOWNLINK_AGC_TARGET_DBFS,
    )


def make_downlink_agc(sample_rate: int | None = None) -> "PhoneAgc | None":
    """按当前进程配置创建下行 AGC；关闭时返回 None。"""
    if not DOWNLINK_AGC_ENABLED:
        return None
    rate = MODEM_RATE if sample_rate is None else sample_rate
    return PhoneAgc(rate, target_dbfs=DOWNLINK_AGC_TARGET_DBFS)


def apply_soft_limit(pcm: bytes, *, knee: float = 0.92) -> bytes:
    """仅作尖峰保护；勿对整段语音常开——tanh 会压高频，听成隔板发闷。"""
    if not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if x.size == 0:
        return pcm
    peak = float(np.max(np.abs(x)))
    if peak <= 28000:
        return pcm
    xn = x / 32768.0
    knee = min(max(knee, 0.5), 0.99)
    y = knee * np.tanh(xn / knee)
    return np.clip(np.rint(y * 32768.0), -32768, 32767).astype("<i2").tobytes()


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
        self._downlink_resampler: StreamingPcmDownsampler | None = None
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
        self._downlink_resampler = None
        logger.info("模组音频流已启动 (%dHz mono)", MODEM_RATE)

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

    def agent_to_modem(self, pcm_agent: bytes, agent_rate: int) -> bytes:
        return _agent_chunk_to_modem(self, pcm_agent, agent_rate)


class SerialPcmAudioBridge:
    """通过 EG25 USB NMEA 口传输 Voice over USB PCM。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 921600,
        tx_gain: float = 1.0,
        *,
        # 默认值不能直接写 NMEA_WRITE_SIZE：那是类定义时求值的，
        # configure_modem_rate() 之后的新值传不进来（16k 下会按 8k 的字节数
        # 喂 16k 流 → 永久欠载 → 电话侧断续）。None 表示「构造时再取」。
        write_size: int | None = None,
        write_interval_seconds: float = NMEA_WRITE_INTERVAL_SECONDS,
        auto_realign: bool = False,
        startup_guard_seconds: float = 0.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.tx_gain = tx_gain
        self.write_size = NMEA_WRITE_SIZE if write_size is None else write_size
        self.write_interval_seconds = write_interval_seconds
        self.auto_realign = auto_realign
        self.startup_guard_seconds = startup_guard_seconds
        self._ready_check: "Callable[[], bool] | None" = None
        self._ser: serial.Serial | None = None
        self._tx_buffer = bytearray()
        # 上次读剩的半个采样（PTY 会在采样中间切断，见 read_modem_chunk）。
        self._rx_carry = b""
        self._tx_lock = threading.Lock()
        self._writer_thread: threading.Thread | None = None
        self._running = False
        self._written_bytes = 0
        self._queued_bytes = 0
        self._last_stats_at = 0.0
        self._write_timeouts = 0
        self._started_at = 0.0
        self._startup_noise_reported = False
        self._downlink_resampler: StreamingPcmDownsampler | None = None
        # 每通一个 AGC 实例：增益状态不跨通继承（桥本身就是每通新建）。
        self._downlink_agc = make_downlink_agc()
        # preroll 默认关闭：Windows SimTech Audio COM 上曾与错误波特率叠出
        # 「全程写超时→静音」；需要时再显式打开。
        self._preroll_bytes = 0
        self._tx_primed = True

    def _write_timeout_seconds(self, *, is_pty: bool) -> float:
        """PTY 用短超时；Windows 官方 Audio COM 首帧常需数百毫秒才收，
        过短会整通写超时→电话静音（2026-08-10 真机：0.15s 全丢，1.0s 可闻）。
        """
        return 0.2 if is_pty else 1.0

    def _open_serial(self) -> serial.Serial:
        """打开 PCM 串口；目标是 PTY 时直接用 PTY 安全波特率，否则失败后降速重开。

        simcom_pcm 模式下 MODEM_PCM_PORT 指向的是 ec20_usb_pty 桥出来的 **PTY**，
        不是真串口：macOS 的 PTY 只接受 ≤230400，用 EC20 NMEA 口的 921600 会抛
        ENOTTY，整通电话在 bridge.start() 就炸掉（真机 2026-08-01 实测）。
        PTY 上波特率本就无物理意义（没有实际串行时序），降速不影响吞吐。

        先判目标是不是 PTY：能判出来就一次开对，省掉每通电话必然失败一次的
        open 和那条看着像故障的 warning；判不出来（软链接失效、非 macOS 命名
        惯例）仍留 ENOTTY 兜底，行为与之前一致。
        """
        baudrate = self.baudrate
        is_pty = _is_pty(self.port)
        if baudrate > PTY_SAFE_BAUDRATE and is_pty:
            logger.info(
                "PCM 口 %s 是 PTY，按 PTY 安全波特率 %s 打开（配置值 %s 在 PTY 上无意义）",
                self.port, PTY_SAFE_BAUDRATE, baudrate,
            )
            baudrate = PTY_SAFE_BAUDRATE
        write_timeout = self._write_timeout_seconds(is_pty=is_pty)
        try:
            return serial.Serial(
                port=self.port,
                baudrate=baudrate,
                timeout=0.02,
                write_timeout=write_timeout,
            )
        except OSError as exc:
            if exc.errno != errno.ENOTTY or baudrate <= PTY_SAFE_BAUDRATE:
                raise
            logger.warning(
                "PCM 口 %s 不接受 %s 波特率（PTY 上限 %s），降速重开；"
                "PTY 无物理串行时序，不影响音频吞吐",
                self.port, baudrate, PTY_SAFE_BAUDRATE,
            )
            return serial.Serial(
                port=self.port,
                baudrate=PTY_SAFE_BAUDRATE,
                timeout=0.02,
                write_timeout=self._write_timeout_seconds(is_pty=True),
            )

    def preclaim(self) -> None:
        """先打开 PCM 口占住驱动接口，再发 AT+CPCMREG（对齐官方示例时序）。

        不启动写线程：无通话时写会超时；只 claim，接通启用后再 ``start()``。
        """
        if self._ser is not None and self._ser.is_open:
            return
        self._ser = self._open_serial()
        self._rx_carry = b""
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
        logger.info("PCM 口已预占用: %s（待 CPCMREG 后再启流）", self.port)

    def start(self) -> None:
        if self._ser is None or not self._ser.is_open:
            self._ser = self._open_serial()
            self._rx_carry = b""
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        self._running = True
        self._started_at = time.monotonic()
        self._startup_noise_reported = False
        self._downlink_resampler = None
        self._written_bytes = 0
        self._queued_bytes = 0
        self._write_timeouts = 0
        self._tx_primed = self._preroll_bytes <= 0
        self._last_stats_at = time.monotonic()
        self._writer_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._writer_thread.start()
        logger.info(
            "NMEA PCM 音频流已启动: %s "
            "(%dHz mono, tx_gain=%.2f, frame=%dB/%.0fms, preroll=%dB)",
            self.port,
            MODEM_RATE,
            self.tx_gain,
            self.write_size,
            self.write_interval_seconds * 1000,
            self._preroll_bytes,
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
        """读一块模组 PCM，保证 16-bit 采样对齐（偶数字节）。

        pyserial 的 read() 返回的是「最多 N 字节」：PTY 上很容易在一个采样中间
        切断，半个采样交给 np.frombuffer(dtype=int16) 会抛
        "buffer size must be a multiple of element size" 并炸掉整通电话
        （真机 2026-08-01 实测，接通 4.3s 后必现）。落单的那个字节留到下一次
        拼回去——丢掉它会让后续所有采样错位半个字节，整条流变噪音。
        """
        if not self._ser:
            return b""
        data = self._rx_carry + self._ser.read(NMEA_READ_SIZE)
        self._rx_carry = b""
        if self.auto_realign and self._should_realign_pcm(data):
            # 丢掉当前错误相位的首字节，让后续 int16 从真正的 low byte 开始。
            # 末尾若因此落单，仍交给既有 carry 逻辑接到下一块，字节不再丢失。
            data = data[1:]
            logger.warning("检测到 SIMCom PCM 字节相位错位，已自动重对齐")
        if len(data) % 2:
            self._rx_carry = data[-1:]
            data = data[:-1]
        if self._should_mute_startup_noise(data):
            if not self._startup_noise_reported:
                logger.warning("SIMCom PCM 启动期仍有异常高能量边界帧，已静音过滤")
                self._startup_noise_reported = True
            return b"\x00" * len(data)
        return data

    def _should_mute_startup_noise(self, data: bytes) -> bool:
        """只过滤开流最初 2.5s 中无法可靠重对齐的满幅异常帧。"""
        if (
            not self.auto_realign
            or not data
            or self.startup_guard_seconds <= 0
            or self._started_at <= 0
            or time.monotonic() - self._started_at > self.startup_guard_seconds
        ):
            return False
        samples = np.frombuffer(data, dtype="<i2").astype(np.float64)
        rms = float(np.sqrt(np.mean(samples * samples)))
        return rms >= SIMCOM_REALIGN_MIN_RMS

    @staticmethod
    def _should_realign_pcm(data: bytes) -> bool:
        """当前 int16 相位明显是噪音、错开 1 byte 明显正常时才返回 True。"""
        if len(data) < 64:
            return False
        current_size = len(data) // 2 * 2
        alternate_size = (len(data) - 1) // 2 * 2
        if alternate_size < 64:
            return False
        current = np.frombuffer(data[:current_size], dtype="<i2").astype(np.float64)
        alternate = np.frombuffer(
            data[1 : 1 + alternate_size], dtype="<i2"
        ).astype(np.float64)
        current_rms = float(np.sqrt(np.mean(current * current)))
        if current_rms < SIMCOM_REALIGN_MIN_RMS:
            return False
        alternate_rms = float(np.sqrt(np.mean(alternate * alternate)))
        return (
            alternate_rms <= SIMCOM_REALIGN_MAX_ALTERNATE_RMS
            and alternate_rms <= current_rms * SIMCOM_REALIGN_IMPROVEMENT_RATIO
        )

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
        silence = b"\x00" * self.write_size
        while self._running:
            now = time.monotonic()
            if now < next_write_at:
                time.sleep(min(0.01, next_write_at - now))
                continue

            if self._ready_check is not None and not self._ready_check():
                # 模组上报忙 (+QPCMV:0,0)，本帧不发送，等待就绪。
                next_write_at += self.write_interval_seconds
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
                # 注意：不要「回队重试」——Windows COM 在错误波特率/驱动背压下会
                # 连续超时，回队会把缓冲区撑满并整通静音（2026-08-10 184509）。
                self._write_timeouts += 1
                if self._write_timeouts == 1 or self._write_timeouts % 50 == 0:
                    logger.warning(
                        "写入 NMEA PCM 超时，丢弃本帧继续 (累计 %d 次)",
                        self._write_timeouts,
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

            next_write_at += self.write_interval_seconds
            # 单帧写阻塞后不要连发赶进度——突发静音交替在电话侧就是「断断续续」。
            if next_write_at < time.monotonic() - self.write_interval_seconds:
                next_write_at = time.monotonic()

    def _next_write_payload(self, silence: bytes) -> bytes:
        with self._tx_lock:
            if not self._tx_primed:
                if len(self._tx_buffer) < self._preroll_bytes:
                    return silence
                self._tx_primed = True
            if len(self._tx_buffer) >= self.write_size:
                payload = bytes(self._tx_buffer[:self.write_size])
                del self._tx_buffer[:self.write_size]
                return payload
            if self._tx_buffer:
                payload = bytes(self._tx_buffer)
                self._tx_buffer.clear()
                return payload + silence[: self.write_size - len(payload)]
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

    def agent_to_modem(self, pcm_agent: bytes, agent_rate: int) -> bytes:
        return _agent_chunk_to_modem(self, pcm_agent, agent_rate)

    def amplify_for_modem(self, pcm_8k: bytes) -> bytes:
        # 轻抬辅音区 → AGC 收动态 → 静态增益微调总响度 → 仅尖峰才软限。
        # AGC 放在 clarity 之后：它要对最终送话的电平负责，包含清晰度处理
        # 带来的电平变化；放在 tx_gain 之前，是为了让 MODEM_TX_GAIN 保持
        # 「最终响度微调」的原有语义，而不是被 AGC 反向抵消掉。
        pcm = apply_phone_clarity(pcm_8k)
        if self._downlink_agc is not None:
            pcm = self._downlink_agc.process(pcm)
        return apply_soft_limit(apply_pcm_gain(pcm, self.tx_gain))


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
        self._downlink_resampler: StreamingPcmDownsampler | None = None

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
        self._downlink_resampler = None
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

    def agent_to_modem(self, pcm_agent: bytes, agent_rate: int) -> bytes:
        return _agent_chunk_to_modem(self, pcm_agent, agent_rate)

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
    if selected == "simcom_pcm":
        # SIMCom 的 PCM 是 mono/16bit 裸流（8k 或 16k，见 MODEM_PCM_RATE），
        # 走模组 USB 音频接口（macOS 上由 ec20_usb_pty 桥成 PTY）。
        if not pcm_port:
            raise RuntimeError(
                "simcom_pcm 模式需要配置 MODEM_PCM_PORT（指向桥出的 PCM PTY，"
                "如 scripts/ec20_usb_pty.py --map 4:/tmp/ec20-pcm）"
            )
        return SerialPcmAudioBridge(
            pcm_port,
            pcm_baudrate,
            tx_gain=tx_gain,
            write_size=SIMCOM_WRITE_SIZE,
            write_interval_seconds=SIMCOM_WRITE_INTERVAL_SECONDS,
            auto_realign=True,
            startup_guard_seconds=SIMCOM_STARTUP_GUARD_SECONDS,
        )
    raise ValueError(
        "MODEM_AUDIO_MODE 只能是 uac、uac_ffmpeg（仅 macOS）、nmea 或 simcom_pcm"
    )
