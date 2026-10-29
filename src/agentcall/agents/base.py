"""Agent 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .tools import ToolRegistry


class VoiceAgent(ABC):
    input_rate: int = 16000
    output_rate: int = 24000

    # 会话不可恢复标志：实现（如断线重连全败）置 True 后，
    # CallSession 主循环会结束整通电话，避免"电话活着但 AI 已死"。
    fatal: bool = False

    _on_transcript: "Callable[[str, str], None] | None" = None
    _on_repeat_stuck: "Callable[[str], None] | None" = None
    _on_status: "Callable[[str], None] | None" = None
    _on_user_interrupt: "Callable[[], bool] | None" = None
    _on_late_cut: "Callable[[], bool] | None" = None
    _on_latency: "Callable[[str, float], None] | None" = None
    _tools: "ToolRegistry | None" = None
    _session_instructions: str | None = None
    _on_output_complete: Callable[[], None] | None = None
    # 单句自测不播放额外的慢网占位语；普通通话保持原行为。
    single_utterance: bool = False
    output_failed: bool = False

    def set_output_complete_handler(self, handler: Callable[[], None]) -> None:
        """可选观测：本轮生成与音频回调全部结束，不代表硬件已播完。"""
        self._on_output_complete = handler

    def _emit_output_complete(self, *, success: bool = True) -> None:
        if self._on_output_complete is not None:
            self.output_failed = self.output_failed or not success
            self._on_output_complete()

    def set_transcript_handler(
        self, handler: "Callable[[str, str], None] | None"
    ) -> None:
        """注册转写回调，参数为 (role, text)，role 为 'user' 或 'agent'。"""
        self._on_transcript = handler

    def set_tools(self, registry: "ToolRegistry | None") -> None:
        """注册可调用工具（function calling）；不支持的实现可忽略。"""
        self._tools = registry

    def set_session_instructions(self, instructions: str | None) -> None:
        """设置本通电话的系统提示词。

        只在 ``start()`` 建立会话前有效——各实现在 start 时把它读进自己的
        ``_instructions`` 并随首次 session.update 发出。会话建立后再调用它
        **不会**改变 provider 侧的提示词，要中途改用
        ``update_session_instructions()``。
        """
        self._session_instructions = instructions

    async def update_session_instructions(self, instructions: str) -> bool:
        """通话中改写系统提示词；返回是否真的下发到了 provider。

        默认实现只更新本地字段，对**已建立**的会话没有任何作用——provider
        那边仍在用连接时的那份。支持中途 session.update 的实现必须覆写。

        返回值不能省：WIL-80 的形态正是「标志翻转了、提示词没换」，调用方
        必须能区分「已下发」与「本 provider 不支持」，否则又是一次静默失效。
        """
        self._session_instructions = instructions
        return False

    async def cancel_response(self) -> bool:
        """打断本轮正在生成的回复；返回是否真的下发到了 provider。

        用于 WIL-90 的单轮长度闸门：模型不可靠地遵守「说短点」的提示词
        （WIL-83 已实测证明），所以需要一个模型之外的确定性手段把跑飞的
        长轮次掐掉。

        默认实现什么都不做。返回值不能省——与
        ``update_session_instructions()`` 同理，调用方必须能区分「已下发」
        与「本 provider 不支持」，否则又是一次静默失效（WIL-75 的形态）。
        """
        return False

    def set_repeat_stuck_handler(
        self, handler: "Callable[[str], None] | None"
    ) -> None:
        """注册复读抑制连续触发后的卡死回调。"""
        self._on_repeat_stuck = handler

    def set_status_handler(self, handler: "Callable[[str], None] | None") -> None:
        """注册面向用户的状态提示回调（如首启下载模型的进度）；多数实现无需用。"""
        self._on_status = handler

    def set_user_interrupt_handler(
        self, handler: "Callable[[], bool] | None"
    ) -> None:
        """注册对端开口打断（barge-in）回调：provider 侧 VAD 检测到对端在 AI
        说话期间开口时触发；调用方在回调里清掉本地未播完的下行积压，让 AI 立即
        闭嘴。回调返回**是否接受这次打断**——False 表示让位（DTMF 护窗期内双音
        优先，或自激兜底已退回半双工，见 WIL-94 坑 1/坑 4），provider 侧此时
        不得 response.cancel。仅在 barge-in 模式（BARGE_IN_ENABLED）下由
        call_agent 注册；不支持打断事件的 provider 不触发即可。"""
        self._on_user_interrupt = handler

    def _emit_user_interrupt(self) -> bool:
        """触发打断回调；返回是否接受打断（无回调/回调异常一律视为不接受）。"""
        if self._on_user_interrupt:
            try:
                return bool(self._on_user_interrupt())
            except Exception:  # noqa: BLE001
                pass
        return False

    def set_latency_handler(
        self, handler: "Callable[[str, float], None] | None"
    ) -> None:
        """注册 provider 侧时延打点回调（WIL-95 第一期）。

        provider 内部测到的逐轮时延（如 OpenAI 的判停→首音频）经此回调交给
        call_agent 落进 events.jsonl——agent 层不认识 CallRecord，持久化归
        调用方。参数为 (stage, ms)；不支持的 provider 不触发即可。"""
        self._on_latency = handler

    def _emit_latency(self, stage: str, ms: float) -> None:
        if self._on_latency:
            try:
                self._on_latency(stage, ms)
            except Exception:  # noqa: BLE001
                pass

    def set_late_cut_handler(self, handler: "Callable[[], bool] | None") -> None:
        """注册复读晚截清积压回调（ResponseAudioGate ``on_late_cut`` 用）。

        与打断回调是**同一个清积压动作、不同的语义**：晚截是 AI 自己复读被掐，
        不是对端插话——绝不能走 ``_emit_user_interrupt``，否则会被 call_agent
        计进 barge-in 的自激笔数（连续几次复读就误判自激退回半双工）。回调
        返回是否真的清了（DTMF 护窗期内会拒绝，双音优先）。"""
        self._on_late_cut = handler

    def _emit_late_cut(self) -> bool:
        """触发复读晚截清积压；未注册专用回调时回落到打断回调（旧接线兼容）。"""
        if self._on_late_cut:
            try:
                return bool(self._on_late_cut())
            except Exception:  # noqa: BLE001
                return False
        return self._emit_user_interrupt()

    def _emit_status(self, text: str) -> None:
        if self._on_status and text:
            try:
                self._on_status(text)
            except Exception:  # noqa: BLE001
                pass

    def _emit_transcript(self, role: str, text: str) -> None:
        if self._on_transcript and text:
            try:
                self._on_transcript(role, text)
            except Exception:  # noqa: BLE001
                pass

    def _emit_repeat_stuck(self, reason: str) -> None:
        if self._on_repeat_stuck:
            try:
                self._on_repeat_stuck(reason)
            except Exception:  # noqa: BLE001
                pass

    @abstractmethod
    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        """启动会话，收到 AI 音频时回调 on_audio_out。"""

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """发送用户侧 PCM（input_rate, mono, int16）。"""

    async def say(self, instructions: str) -> None:
        """让 Agent 主动说一段话；不支持的实现可以忽略。"""

    async def external_tool_result(
        self,
        name: str,
        result: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        """Record an externally executed tool fact without requesting a reply.

        Providers that cannot add a reliable standalone context item return
        ``False``. Callers must never fabricate a function-call id as fallback.
        """

        return False

    @abstractmethod
    async def stop(self) -> None:
        """结束会话。"""
