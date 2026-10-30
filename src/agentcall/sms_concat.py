"""进程内长短信重组；调用方负责周期检查，不启动线程。"""

import threading
from dataclasses import dataclass, field

from .sms_pdu import SmsDeliver

CONCAT_TIMEOUT_SECONDS = 600.0
MISSING_PART = "[…部分内容未收到…]"


@dataclass
class SmsDelivery:
    """交付凭据：成功回调前保留所有 SIM 位置，重复分段可追加待删除位置。"""

    message: SmsDeliver
    storage: set[tuple[str, str]] = field(default_factory=set)
    backfill: bool = False
    delivered: bool = False


@dataclass
class _Assembly:
    started: float
    parts: dict[int, SmsDeliver] = field(default_factory=dict)
    storage: set[tuple[str, str]] = field(default_factory=set)
    backfill: bool = True
    delivery: SmsDelivery | None = None


class SmsConcat:
    def __init__(self, timeout: float = CONCAT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self._pending: dict[tuple[str | None, int, int], _Assembly] = {}
        self._completed: dict[tuple[str | None, int, int], _Assembly] = {}
        self._lock = threading.Lock()

    def add(
        self, message: SmsDeliver, now: float, *, storage: tuple[str, str] | None = None,
        backfill: bool = False,
    ) -> SmsDelivery | None:
        info = message.concat
        if info is None:
            return SmsDelivery(message, {storage} if storage else set(), backfill)
        if not 1 <= info.seq <= info.total <= 255:
            raise ValueError("无效连接序号")
        key = (message.sender, info.ref, info.total)
        with self._lock:
            completed = self._completed.get(key)
            if completed and now - completed.started < self.timeout:
                if completed.parts.get(info.seq) == message:
                    if storage:
                        completed.storage.add(storage)
                    delivery = completed.delivery
                    return delivery if delivery and delivery.delivered else None
            assembly = self._pending.setdefault(key, _Assembly(now))
            assembly.parts.setdefault(info.seq, message)
            if storage:
                assembly.storage.add(storage)
            assembly.backfill = assembly.backfill and backfill
            if len(assembly.parts) != info.total:
                return None
            del self._pending[key]
            assembly.started = now
            self._completed[key] = assembly
            assembly.delivery = self._join(assembly, info.total)
            return assembly.delivery

    @staticmethod
    def _join(assembly: _Assembly, total: int) -> SmsDelivery:
        # 首段缺失时只能降级使用最早收到的分段时间戳。
        first = assembly.parts.get(1) or next(iter(assembly.parts.values()))
        body = "".join(assembly.parts[seq].body if seq in assembly.parts else MISSING_PART
                       for seq in range(1, total + 1))
        return SmsDelivery(SmsDeliver(first.sender, first.timestamp, body), assembly.storage, assembly.backfill)

    def flush_expired(self, now: float) -> list[SmsDelivery]:
        result = []
        with self._lock:
            for key, assembly in list(self._pending.items()):
                if now - assembly.started >= self.timeout:
                    assembly.delivery = self._join(assembly, key[2])
                    result.append(assembly.delivery)
                    del self._pending[key]
                    assembly.started = now
                    self._completed[key] = assembly
            for key, assembly in list(self._completed.items()):
                if now - assembly.started >= self.timeout:
                    del self._completed[key]
        return result
