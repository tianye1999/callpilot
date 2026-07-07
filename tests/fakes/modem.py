"""FakeModem：与 Eg25Modem 同形（duck-typing）的内存实现。

记录全部指令调用供断言；提供 trigger_* 方法模拟模组主动上报
（RING/挂断/短信），用于驱动 CallAgentService 的回调路径。
"""

from __future__ import annotations

import threading
from typing import Callable


class FakeModem:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.sms_should_succeed = True
        self.connected_flag = threading.Event()
        self._on_ring: Callable[[str | None], None] | None = None
        self._on_hangup: Callable[[], None] | None = None
        self._on_sms: Callable[[str | None, str], None] | None = None
        self._on_call_connected: Callable[[str | None], None] | None = None
        self._pcm_ready = True

    # ---- 记录型指令接口（与 Eg25Modem 对齐）----

    def connect(self) -> None:
        self.calls.append(("connect", ()))

    def initialize_for_voice(self, audio_mode: str = "uac") -> None:
        self.calls.append(("initialize_for_voice", (audio_mode,)))

    def start_listener(self) -> None:
        self.calls.append(("start_listener", ()))

    def stop_listener(self) -> None:
        self.calls.append(("stop_listener", ()))

    def answer(self) -> None:
        self.calls.append(("answer", ()))

    def dial(self, number: str) -> str:
        self.connected_flag.clear()
        self.calls.append(("dial", (number,)))
        return "OK"

    def hangup(self) -> None:
        self.calls.append(("hangup", ()))
        self.connected_flag.clear()

    def close(self) -> None:
        self.calls.append(("close", ()))

    def send_sms(self, number: str, text: str) -> bool:
        self.calls.append(("send_sms", (number, text)))
        return self.sms_should_succeed

    def is_call_connected(self) -> bool:
        return self.connected_flag.is_set()

    def pcm_ready(self) -> bool:
        return self._pcm_ready

    # ---- 回调注册（与 Eg25Modem 对齐）----

    def on_ring(self, callback: Callable[[str | None], None]) -> None:
        self._on_ring = callback

    def on_hangup(self, callback: Callable[[], None]) -> None:
        self._on_hangup = callback

    def on_sms(self, callback: Callable[[str | None, str], None]) -> None:
        self._on_sms = callback

    def on_call_connected(self, callback: Callable[[str | None], None]) -> None:
        self._on_call_connected = callback

    # ---- 测试驱动：模拟模组主动上报 ----

    def trigger_ring(self, caller: str | None = "13800000000") -> None:
        assert self._on_ring is not None, "on_ring 回调未注册"
        self._on_ring(caller)

    def trigger_hangup(self) -> None:
        assert self._on_hangup is not None, "on_hangup 回调未注册"
        self._on_hangup()

    def trigger_sms(self, sender: str | None, text: str) -> None:
        assert self._on_sms is not None, "on_sms 回调未注册"
        self._on_sms(sender, text)

    def trigger_call_connected(self, number: str | None = None) -> None:
        self.connected_flag.set()
        if self._on_call_connected:
            self._on_call_connected(number)

    # ---- 断言辅助 ----

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]
