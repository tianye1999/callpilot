"""通话记录模块：按通话建目录，保存事件打点、双向录音与元数据。

目录布局（``base_dir/<id>/``）::

    20260707-183000-outbound-10000/
        events.jsonl    # 事件/延迟打点，每行一个 JSON 对象
        meta.json       # 通话元数据（方向/号码/起止时间/状态/事件数/时长）
        uplink.wav      # 上行录音（用户→远端），8kHz 16bit mono
        downlink.wav    # 下行录音（远端→用户），8kHz 16bit mono
        mixed.wav       # 合成对话录音：立体声（左=AI下行 / 右=对方上行），按时间轴对齐
        summary.json    # Agent 生成的通话摘要（可选）

性能约定：``log_event`` / ``write_uplink`` / ``write_downlink`` 会被音频主循环
高频调用，因此只做内存追加（极短锁内 ``list.append`` / ``bytearray.extend``），
绝不做磁盘 IO；所有落盘都集中在 ``finish()`` 一次完成。

环境变量（均有默认值，供 ``CallLogger.from_env()`` 使用）：

- ``CALL_LOG_DIR``：通话记录根目录，默认运行时 data/recordings
- ``RECORDING_ENABLED``：是否保存录音，默认关（判定走 ``config.get_bool``，
  与设置面板同一套语义）
- ``RECORDING_RETENTION_DAYS``：保留天数，默认 30；<=0 表示不自动清理
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import config

logger = logging.getLogger(__name__)

# 录音固定格式：8kHz 16bit 单声道（EC20 语音通道的原生采样率）。
SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2
CHANNELS = 1

_ID_TS_RE = re.compile(r"^(\d{8}-\d{6})")


def _sanitize_number(number: str | None) -> str:
    """把号码变成目录名安全的片段；空/None 用 unknown。"""
    if not number:
        return "unknown"
    cleaned = re.sub(r"[^0-9A-Za-z+]", "", number)
    return cleaned or "unknown"


def _write_wav(path: Path, pcm: bytes) -> None:
    # 截断到采样对齐，避免最后半个采样写出坏帧。
    aligned = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm[:aligned])


def _write_wav_stereo(path: Path, interleaved: bytes) -> None:
    """写立体声 8kHz 16bit WAV（L/R 交错的 PCM）。"""
    frame = SAMPLE_WIDTH * 2
    aligned = len(interleaved) - (len(interleaved) % frame)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(interleaved[:aligned])


# 合成时把对方(上行)自适应放大到可闻：原始上行是窄带低电平（实测比 AI 低约一个
# 数量级），不放大会被 AI 完全盖过。按 99.5 分位峰值归一到目标电平，并设增益上限，
# 避免把极静通话的底噪放得过大；原始 uplink.wav 不受影响。
_MIX_CALLER_TARGET_PEAK = 18000.0  # 目标峰值(int16，约 -5dBFS)
_MIX_CALLER_MAX_GAIN = 40.0


def _boost_caller(up: Any) -> Any:
    """把对方(上行) int16 数组自适应放大到可闻，限幅防削顶；极静则原样返回。"""
    if up.size == 0:
        return up
    ref = float(np.percentile(np.abs(up.astype(np.int32)), 99.5))
    if ref < 1.0:
        return up
    gain = min(_MIX_CALLER_TARGET_PEAK / ref, _MIX_CALLER_MAX_GAIN)
    if gain <= 1.0:
        return up
    boosted = np.clip(up.astype(np.float32) * gain, -32768.0, 32767.0)
    return boosted.astype("<i2")


def _build_stereo_mix(
    uplink: bytes, downlink_chunks: list[tuple[int, bytes]]
) -> bytes:
    """把上行(对方)与按上行位置打点的下行(AI)对齐成立体声交错 PCM。

    左=AI(下行)，右=对方(上行)。以上行字节数为共同时间轴，但 OpenAI 会把整轮 AI
    音频**以突发写入**（远快于实时播放），因此不能简单按打点位置覆盖——同一轮的
    分块打点几乎相同，覆盖会只剩最后一小段。做法：AI 分块首尾相接铺开，静音间隔后
    按上行位置重新锚定，即 ``start = max(游标, 上行位置)``。对方声道自适应放大到可闻。
    两路皆空返回 b""（不生成文件）。
    """
    up = np.frombuffer(
        uplink[: len(uplink) - (len(uplink) % SAMPLE_WIDTH)], dtype="<i2"
    )
    placed: list[tuple[int, Any]] = []
    cursor = 0  # AI 左声道写游标（样本）：保证突发分块首尾相接、不互相覆盖
    for pos_bytes, pcm in downlink_chunks:
        chunk = np.frombuffer(
            pcm[: len(pcm) - (len(pcm) % SAMPLE_WIDTH)], dtype="<i2"
        )
        if chunk.size == 0:
            continue
        start = max(cursor, max(0, pos_bytes // SAMPLE_WIDTH))
        placed.append((start, chunk))
        cursor = start + len(chunk)
    n = max(len(up), cursor)
    if n == 0:
        return b""
    left = np.zeros(n, dtype="<i2")
    for start, chunk in placed:
        end = min(n, start + len(chunk))
        if end > start:
            left[start:end] = chunk[: end - start]
    right = np.zeros(n, dtype="<i2")
    right[: len(up)] = _boost_caller(up)
    stereo = np.empty(n * 2, dtype="<i2")
    stereo[0::2] = left   # 左 = AI 下行
    stereo[1::2] = right  # 右 = 对方上行（已放大）
    return stereo.tobytes()


class CallRecord:
    """单次通话的记录器。

    事件与 PCM 都先积累在内存里（极短锁），``finish()`` 时一次性落盘。
    ``finish()`` 幂等：第二次及以后的调用直接返回。
    """

    def __init__(
        self,
        id: str,  # noqa: A002 - 接口契约要求属性名为 id
        path: Path,
        direction: str,
        number: str | None,
        recording_enabled: bool = False,
        source: str | None = None,
        public_id: str | None = None,
    ) -> None:
        self.id = id
        self.public_id = public_id or f"call_{secrets.token_urlsafe(18)}"
        self.path = path
        self.direction = direction
        self.number = number
        self.source = source
        self.recording_enabled = recording_enabled
        self.started_at = time.time()
        self._content_updated_at = self.started_at
        self._summary_state = "UNAVAILABLE"
        self._lock = threading.Lock()
        self._disk_lock = threading.Lock()
        self._event_lines: list[str] = []
        # 录音缓冲用 chunk list（append 引用 O(1)），不用 bytearray——
        # 长通话时 extend 会在锁内触发大 buffer 扩容拷贝，造成音频抖动。
        self._uplink: list[bytes] = []
        self._uplink_bytes = 0  # 上行累计字节数：作为下行(AI)分块的时间轴锚点
        self._downlink: list[tuple[int, bytes]] = []  # (上行位置, AI PCM)
        self._answered = False
        self._finished = False

    # ---- 热路径：只做内存追加 ----

    def log_event(self, type: str, **fields: Any) -> None:  # noqa: A002
        """追加一条事件（自动 ts=time.time()）到 events.jsonl，线程安全。

        通话中只写内存缓冲；``finish()`` 之后调用会直接追加到磁盘文件
        （非热路径，例如挂断后补记摘要），此时 meta.json 里的事件计数不再更新。
        """
        event: dict[str, Any] = {"type": type, "ts": time.time(), **fields}
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            if not self._finished:
                if type == "answered":
                    self._answered = True
                self._event_lines.append(line)
                return
        # 已经 finish：直接落盘（低频路径）。
        try:
            with open(self.path / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.warning("追加事件到 %s 失败: %s", self.id, exc)

    def log_latency(self, stage: str, ms: float, **fields: Any) -> None:
        """延迟打点便捷方法，等价于 log_event("latency", stage=..., ms=...)。"""
        self.log_event("latency", stage=stage, ms=ms, **fields)

    def write_uplink(self, pcm8k: bytes) -> None:
        """追加上行 PCM（8kHz 16bit mono）；录音关闭时 no-op。"""
        if not self.recording_enabled or not pcm8k:
            return
        with self._lock:
            if not self._finished:
                self._uplink.append(pcm8k)
                self._uplink_bytes += len(pcm8k)

    def write_downlink(self, pcm8k: bytes) -> None:
        """追加下行 PCM（8kHz 16bit mono）；录音关闭时 no-op。"""
        if not self.recording_enabled or not pcm8k:
            return
        with self._lock:
            if not self._finished:
                # 打上"此刻上行已累计字节数"作为时间轴位置，供合成对齐。
                self._downlink.append((self._uplink_bytes, pcm8k))

    # ---- 低频路径：允许磁盘 IO ----

    def set_summary(self, summary: dict) -> None:
        """写 summary.json 并记一条 summary 事件。"""
        with self._disk_lock:
            try:
                (self.path / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("写入 %s/summary.json 失败: %s", self.id, exc)
            self.log_event("summary", summary=summary)
            with self._lock:
                self._content_updated_at = time.time()
                content_updated_at = self._content_updated_at
                self._summary_state = (
                    "READY" if summary.get("ok") is True else "FAILED"
                )
                summary_state = self._summary_state
            self._update_content_meta(content_updated_at, summary_state)

    def mark_summary_pending(self) -> None:
        """Persist that a summary worker was actually scheduled for this call."""
        with self._disk_lock:
            with self._lock:
                self._summary_state = "PENDING"
                self._content_updated_at = time.time()
                content_updated_at = self._content_updated_at
            self._update_content_meta(content_updated_at, "PENDING")

    def finish(self, status: str) -> None:
        """结束通话：flush 录音为 wav、写 events.jsonl 与 meta.json。幂等。"""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            ended_at = time.time()
            self._event_lines.append(
                json.dumps(
                    {"type": "call_finished", "ts": ended_at, "status": status},
                    ensure_ascii=False,
                )
            )
            event_lines = self._event_lines
            self._event_lines = []
            uplink = b"".join(self._uplink)
            downlink_chunks = self._downlink
            downlink = b"".join(pcm for _, pcm in downlink_chunks)
            answered = self._answered
            self._uplink = []
            self._uplink_bytes = 0
            self._downlink = []

        # 磁盘 IO 全部在热路径锁外完成；disk_lock 与迟到摘要更新串行化。
        with self._disk_lock:
            try:
                self.path.mkdir(parents=True, exist_ok=True)
                (self.path / "events.jsonl").write_text(
                    "\n".join(event_lines) + "\n", encoding="utf-8"
                )
                if self.recording_enabled:
                    _write_wav(self.path / "uplink.wav", uplink)
                    _write_wav(self.path / "downlink.wav", downlink)
                    mixed = _build_stereo_mix(uplink, downlink_chunks)
                    if mixed:
                        _write_wav_stereo(self.path / "mixed.wav", mixed)
                with self._lock:
                    self._content_updated_at = max(
                        self._content_updated_at, ended_at
                    )
                    content_updated_at = self._content_updated_at
                meta = {
                    "id": self.id,
                    "public_id": self.public_id,
                    "content_updated_at": content_updated_at,
                    "summary_state": self._summary_state,
                    "direction": self.direction,
                    "number": self.number,
                    "started_at": self.started_at,
                    "ended_at": ended_at,
                    "duration": round(ended_at - self.started_at, 3),
                    "status": status,
                    "answered": answered,
                    "events": len(event_lines),
                    "recording_enabled": self.recording_enabled,
                    "uplink_bytes": len(uplink),
                    "downlink_bytes": len(downlink),
                }
                if self.source:
                    meta["source"] = self.source
                (self.path / "meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as exc:
                logger.error("落盘通话记录 %s 失败: %s", self.id, exc)

    def _update_content_meta(
        self, content_updated_at: float, summary_state: str
    ) -> None:
        meta_path = self.path / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(meta, dict):
            return
        meta["public_id"] = self.public_id
        meta["content_updated_at"] = content_updated_at
        meta["summary_state"] = summary_state
        temp_path = meta_path.with_suffix(".json.tmp")
        try:
            temp_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp_path.replace(meta_path)
        except OSError as exc:
            logger.warning("更新 %s/meta.json 失败: %s", self.id, exc)
            temp_path.unlink(missing_ok=True)


class CallLogger:
    """通话记录管理器：创建通话目录、查询历史、清理过期记录。"""

    def __init__(
        self,
        base_dir: str | Path,
        recording_enabled: bool = False,
        retention_days: int = 30,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.recording_enabled = recording_enabled
        self.retention_days = retention_days

    @classmethod
    def from_env(cls) -> CallLogger:
        """从环境变量构造；CALL_LOG_DIR 未进 config 注册表（不上面板），单独读。"""
        return cls(
            base_dir=config.call_log_dir(),
            recording_enabled=config.get_bool("RECORDING_ENABLED"),
            retention_days=config.get_int("RECORDING_RETENTION_DAYS"),
        )

    def begin_call(
        self,
        direction: str,
        number: str | None,
        *,
        source: str | None = None,
        recording_enabled: bool | None = None,
    ) -> CallRecord:
        """开始记录一次通话；direction 必须是 inbound 或 outbound。"""
        if direction not in ("inbound", "outbound"):
            raise ValueError(f"direction 必须是 inbound/outbound，收到: {direction!r}")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base_id = f"{stamp}-{direction}-{_sanitize_number(number)}"
        call_id = base_id
        seq = 2
        while (self.base_dir / call_id).exists():
            call_id = f"{base_id}-{seq}"
            seq += 1
        path = self.base_dir / call_id
        path.mkdir(parents=True)
        record = CallRecord(
            id=call_id,
            path=path,
            direction=direction,
            number=number,
            recording_enabled=(
                self.recording_enabled
                if recording_enabled is None
                else recording_enabled
            ),
            source=source,
        )
        started_fields: dict[str, Any] = {"direction": direction, "number": number}
        if source:
            started_fields["source"] = source
        record.log_event("call_started", **started_fields)
        logger.info("开始记录通话 %s", call_id)
        return record

    def list_calls(self, limit: int = 50) -> list[dict]:
        """列出历史通话（新→旧），读 meta.json；损坏/未完成的目录跳过。"""
        if not self.base_dir.is_dir():
            return []
        results: list[dict] = []
        dirs = sorted(
            (p for p in self.base_dir.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        for path in dirs:
            if len(results) >= limit:
                break
            try:
                meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(meta, dict):
                continue
            entry = {
                "id": meta.get("id", path.name),
                "direction": meta.get("direction"),
                "number": meta.get("number"),
                "started_at": meta.get("started_at"),
                "ended_at": meta.get("ended_at"),
                "status": meta.get("status"),
            }
            summary_path = path / "summary.json"
            if summary_path.exists():
                try:
                    entry["summary"] = json.loads(
                        summary_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    pass
            results.append(entry)
        return results

    def delete_call(self, call_id: str, *, active_ids: set[str] | None = None) -> str:
        """删除单条通话目录；返回 ``deleted``/``skipped``/``missing``。"""
        if active_ids and call_id in active_ids:
            return "skipped"
        path = self.base_dir / call_id
        if not path.is_dir():
            return "missing"
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.warning("删除通话目录 %s 失败: %s", call_id, exc)
            raise
        return "deleted"

    def clear_calls(self, *, active_ids: set[str] | None = None) -> dict[str, list[str]]:
        """删除全部通话目录，跳过正在进行中的通话。"""
        deleted: list[str] = []
        skipped: list[str] = []
        if not self.base_dir.is_dir():
            return {"deleted": deleted, "skipped": skipped}
        for path in sorted(p for p in self.base_dir.iterdir() if p.is_dir()):
            call_id = path.name
            if active_ids and call_id in active_ids:
                skipped.append(call_id)
                continue
            try:
                shutil.rmtree(path)
                deleted.append(call_id)
            except OSError as exc:
                logger.warning("删除通话目录 %s 失败: %s", call_id, exc)
                raise
        return {"deleted": deleted, "skipped": skipped}

    def inbound_numbers(self) -> set[str]:
        """所有来电方号码集合（direction==inbound），供发短信目标校验用。

        扫描全部通话目录(不设窗口上限、不读 summary),比 ``list_calls`` 更省;
        只取来电——避免大量外呼把老来电方挤出窗口,导致给该号码的合法回复被误拒。
        """
        numbers: set[str] = set()
        if not self.base_dir.is_dir():
            return numbers
        for path in self.base_dir.iterdir():
            if not path.is_dir():
                continue
            try:
                meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(meta, dict) or meta.get("direction") != "inbound":
                continue
            number = meta.get("number")
            if isinstance(number, str) and number.strip():
                numbers.add(number.strip())
        return numbers

    def answered_outbound_numbers(self) -> set[str]:
        """已真正接通过的外呼号码集合，供发短信目标校验用。"""
        numbers: set[str] = set()
        if not self.base_dir.is_dir():
            return numbers
        for path in self.base_dir.iterdir():
            if not path.is_dir():
                continue
            try:
                meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(meta, dict) or meta.get("direction") != "outbound":
                continue
            number = meta.get("number")
            if not isinstance(number, str) or not number.strip():
                continue
            answered = meta.get("answered")
            if answered is True or (
                "answered" not in meta and self._legacy_events_include_answered(path)
            ):
                numbers.add(number.strip())
        return numbers

    @staticmethod
    def _legacy_events_include_answered(path: Path) -> bool:
        try:
            with (path / "events.jsonl").open(encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "answered":
                        return True
        except (OSError, UnicodeDecodeError):
            return False
        return False

    def purge_expired(self) -> int:
        """删除超过保留期的通话目录，返回删除数量；retention_days<=0 不清理。"""
        if self.retention_days <= 0 or not self.base_dir.is_dir():
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for path in self.base_dir.iterdir():
            if not path.is_dir():
                continue
            if self._call_time(path) >= cutoff:
                continue
            try:
                shutil.rmtree(path)
                removed += 1
            except OSError as exc:
                logger.warning("删除过期通话目录 %s 失败: %s", path.name, exc)
        if removed:
            logger.info("清理过期通话记录 %d 条", removed)
        return removed

    @staticmethod
    def _call_time(path: Path) -> float:
        """确定通话时间：meta.started_at → 目录名时间戳 → 目录 mtime。"""
        try:
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            started = meta.get("started_at")
            if isinstance(started, (int, float)):
                return float(started)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        match = _ID_TS_RE.match(path.name)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                pass
        try:
            return path.stat().st_mtime
        except OSError:
            return time.time()
