"""OpenAI Realtime 实时语音 Agent（raw websockets，不依赖 openai SDK）。

采用 GA（正式版）会话结构（session.type="realtime" + 嵌套 audio.input/output），
真机验证连通（2026-07）。事件命名同时容忍新旧两代以增强兼容：
- 下行音频：``response.output_audio.delta``（GA）与 ``response.audio.delta``；
- Agent 转写：``response.output_audio_transcript.done`` 与 ``response.audio_transcript.done``。

OPENAI_REALTIME_URL 可选：留空即直连 api.openai.com；仅在用反代 / Azure OpenAI，
或所在网络无法直连 OpenAI 时才需填。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime
from typing import Any, Callable

import websockets

from .. import config
from ..prompts import agent_language, repeat_nudge_instructions
from ..repeat_suppression import ResponseAudioGate
from .base import VoiceAgent
from .tools import SILENT_AFTER_TOOLS, TERMINAL_TOOLS

logger = logging.getLogger(__name__)

# OpenAI Realtime 官方 wss 端点（可经 OPENAI_REALTIME_URL 覆盖 base）。
DEFAULT_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# 输入音频转写模型（session.input_audio_transcription）。
TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

# 重连成功后让 Agent 主动安抚对方的提示词（与 qwen_agent 语义一致）。
RECONNECT_NOTICE = "请直接用中文说：抱歉刚才信号不太好，请继续。"


def _response_id(event: dict[str, Any]) -> str | None:
    raw_response = event.get("response")
    response: dict[str, Any] = raw_response if isinstance(raw_response, dict) else {}
    raw = (
        event.get("response_id")
        or event.get("item_id")
        or event.get("id")
        or response.get("id")
    )
    return str(raw) if raw else None


def _reconnect_max() -> int:
    """读取运行中断线的最大重连次数（注册表 OPENAI_RECONNECT_MAX，默认 2）。"""
    return config.get_int("OPENAI_RECONNECT_MAX")


def _default_instructions() -> str:
    """无外部指令时的默认系统提示词（与 qwen_agent 的默认语义对齐）。"""
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    now = datetime.now()
    now_str = f"{now:%Y年%m月%d日 %H:%M}（{weekdays[now.weekday()]}）"
    return (
        "你是接入电话的语音 Agent。接通后请先用中文简短自我介绍。"
        "之后用口语化、简洁的方式回答对方问题，每次回答控制在两三句话以内。"
        f"当前真实日期时间是 {now_str}，这是准确信息；对方询问日期、时间、"
        "今天几号或星期几时，必须以此为准回答，不要凭记忆猜测年份。"
        "你可以调用工具帮用户完成实际操作：发送短信(send_sms，发给本人时号码留空)、"
        "挂断电话(hangup_call，挂断前先说一句告别语)、查询最近收到的短信验证码"
        "(query_verification_code)。需要时主动调用对应工具，操作完成后用一句话口头确认结果。"
    )


class OpenAIVoiceAgent(VoiceAgent):
    # OpenAI Realtime pcm16 固定 24kHz mono；桥自动做 8k↔24k 重采样。
    input_rate = 24000
    output_rate = 24000

    def __init__(
        self,
        api_key: str,
        model: str,
        model_display_name: str,
        voice: str = "alloy",
        realtime_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.model_display_name = model_display_name
        self.voice = voice
        self.realtime_url = realtime_url
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._audio_gate = ResponseAudioGate(
            "openai",
            self._emit_audio_out,
            on_suppressed=self._nudge_after_repeat_suppressed,
            on_stuck=self._repeat_suppression_stuck,
        )
        self._running = False
        self._handled_tool_calls: set[str] = set()
        self._instructions: str | None = None

    # ---- 连接管理 ----

    def _build_url(self) -> str:
        """拼接 wss 地址：base（默认官方端点，可覆盖）+ ?model=<model>。"""
        base = (self.realtime_url or "").strip() or DEFAULT_REALTIME_URL
        if "model=" in base:
            # 覆盖 URL 已自带 model 参数（如 Azure 兼容端点）则原样使用。
            return base
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}model={self.model}"

    def _tool_specs(self) -> list[dict]:
        """把 ToolRegistry 的千问嵌套格式摊平成 OpenAI Realtime 的扁平格式。

        千问: {"type": "function", "function": {"name": ..., ...}}
        OpenAI: {"type": "function", "name": ..., ...}
        """
        specs: list[dict] = []
        if self._tools is None:
            return specs
        for spec in self._tools.specs():
            function = spec.get("function")
            if isinstance(function, dict):
                specs.append({"type": spec.get("type", "function"), **function})
            else:
                specs.append(spec)
        return specs

    async def _connect(self) -> None:
        """建立 websocket 连接并发送 session.update；成功后挂到 self._ws。

        采用 GA（正式版）会话结构：``session.type="realtime"`` + 嵌套的
        ``audio.input/output``（含 pcm 采样率、server_vad、转写、音色）。
        不发 ``OpenAI-Beta`` 头——beta 形态已在多数账号停用
        （否则报 beta_api_shape_disabled）。
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        ws = await websockets.connect(self._build_url(), additional_headers=headers)

        session: dict = {
            "type": "realtime",
            "instructions": self._instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": self.input_rate},
                    # 打断事件本轮忽略，半双工由 call_agent 统一管理。
                    "turn_detection": {"type": "server_vad"},
                    "transcription": {"model": TRANSCRIPTION_MODEL},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": self.output_rate},
                    "voice": self.voice,
                },
            },
        }
        tool_specs = self._tool_specs()
        if tool_specs:
            session["tools"] = tool_specs
            logger.info("已为会话注册 %d 个工具", len(tool_specs))

        await ws.send(json.dumps({"type": "session.update", "session": session}))
        self._ws = ws
        logger.info("OpenAI Realtime 连接已建立: %s", self.model)

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self._on_audio_out = on_audio_out
        self._running = True
        self._instructions = self._session_instructions or _default_instructions()
        await self._connect()
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _reconnect(self) -> bool:
        """断线重连（参照 qwen_agent 语义）；全部失败返回 False。"""
        max_attempts = _reconnect_max()
        for attempt in range(1, max_attempts + 1):
            if not self._running:
                return True  # 已主动 stop：不算失败，也不再重连
            logger.warning(
                "OpenAI Realtime 尝试重连(第 %d/%d 次)", attempt, max_attempts
            )
            try:
                await self._connect()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "OpenAI Realtime 重连失败(第 %d/%d 次): %s",
                    attempt, max_attempts, exc,
                )
                continue
            if not self._running:
                # stop() 在重连期间完成：关闭刚建立的连接，避免泄漏。
                stale, self._ws = self._ws, None
                if stale is not None:
                    try:
                        await stale.close()
                    except Exception:  # noqa: BLE001
                        pass
                return True
            logger.info("OpenAI Realtime 重连成功(第 %d/%d 次)", attempt, max_attempts)
            try:
                await self.say(RECONNECT_NOTICE)
            except Exception as exc:  # noqa: BLE001
                logger.warning("重连后安抚语发送失败: %s", exc)
            return True
        return False

    # ---- 收发 ----

    def _emit_audio_out(self, pcm: bytes) -> None:
        if self._on_audio_out:
            self._on_audio_out(pcm)

    async def send_audio(self, pcm: bytes) -> None:
        ws = self._ws
        if not ws or not pcm:
            return
        payload = json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        })
        try:
            await ws.send(payload)
        except Exception as exc:  # noqa: BLE001
            # 连接刚死：丢弃本帧，重连由接收循环统一负责。
            logger.warning("发送音频失败，丢弃本帧: %s", exc)

    async def say(self, instructions: str) -> None:
        ws = self._ws
        if not ws:
            return
        try:
            await ws.send(json.dumps({
                "type": "response.create",
                "response": {"instructions": instructions},
            }))
        except Exception as exc:  # noqa: BLE001
            # 断线窗口内 say 失败不应炸掉整通电话（开场白路径直接 await）；
            # 重连由接收循环统一负责。
            logger.warning("发送说话指令失败: %s", exc)

    async def external_tool_result(
        self,
        name: str,
        result: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        """Append a supported system item; do not fabricate a function call."""

        ws = self._ws
        if ws is None:
            return False
        success = result.get("success") is True
        count = result.get("count")
        safe_count = count if isinstance(count, int) and count >= 0 else 0
        mode = result.get("mode")
        safe_mode = mode if isinstance(mode, str) else "unknown"
        text = (
            f"[external_tool_result] {name} was executed by {source}; "
            f"success={str(success).lower()}, count={safe_count}, mode={safe_mode}. "
            "This is context only; do not speak merely to acknowledge it."
        )
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "system",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入外部工具结果失败: error_type=%s", type(exc).__name__)
            return False
        return True

    def _nudge_after_repeat_suppressed(self, _transcript: str) -> None:
        try:
            asyncio.get_running_loop().create_task(
                self.say(repeat_nudge_instructions(agent_language()))
            )
        except RuntimeError:
            logger.warning("发送复读换说法提示失败: event loop 不可用")

    def _repeat_suppression_stuck(self, count: int, _transcript: str) -> None:
        self._emit_repeat_stuck(f"复读抑制连续触发 {count} 次，判定模型卡死")

    async def stop(self) -> None:
        self._running = False
        ws, self._ws = self._ws, None
        if ws:
            try:
                await ws.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 OpenAI 连接异常: %s", exc)
        if self._recv_task:
            # 有界等待：接收任务可能正阻塞在重连的 websockets.connect
            # （open_timeout × 重试次数可达 20s+），而 CallSession 收尾对
            # stop() 无超时——超时就取消，别拖延整通电话的落盘。
            try:
                await asyncio.wait_for(asyncio.shield(self._recv_task), timeout=3.0)
            except asyncio.TimeoutError:
                self._recv_task.cancel()
            except Exception:  # noqa: BLE001
                pass  # 任务自身异常不影响收尾
            await asyncio.gather(self._recv_task, return_exceptions=True)
            self._recv_task = None

    async def _recv_loop(self) -> None:
        """接收循环：连接存活期间分发事件；断线走重连，全败置 fatal。"""
        while self._running:
            ws = self._ws
            if ws is None:
                break
            try:
                async for message in ws:
                    if isinstance(message, (bytes, bytearray)):
                        continue
                    try:
                        event = json.loads(message)
                    except json.JSONDecodeError:
                        logger.warning("收到非 JSON 消息，已忽略")
                        continue
                    self._handle_event(event)
                    if not self._running:
                        break
            except websockets.ConnectionClosed:
                logger.info("OpenAI Realtime 连接已关闭")
            except Exception as exc:  # noqa: BLE001
                logger.error("OpenAI 接收循环异常: %s", exc)
            if not self._running:
                break
            # 立即摘掉死连接：重连期间 send_audio/say 看到 _ws 为 None 会
            # 静默丢帧（对齐 qwen 语义），否则每 10ms 一帧的上行都对死连接
            # send 并各刷一条 warning（重连数秒内可积数百条）。
            self._ws = None
            # 通话进行中断线：尝试重连；全部失败则会话不可恢复，置 fatal
            # 让 CallSession 主循环结束整通电话（避免"电话活着但 AI 已死"）。
            logger.warning("OpenAI Realtime 运行中断线，尝试重连")
            if not await self._reconnect():
                logger.error("OpenAI Realtime 重连全部失败，标记会话不可恢复")
                self.fatal = True
                return

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("type", "")
        if event_type in ("response.audio.delta", "response.output_audio.delta"):
            # beta 与 GA 两代事件名的下行音频增量
            delta = event.get("delta", "")
            if delta:
                self._audio_gate.push_audio(
                    _response_id(event), base64.b64decode(delta)
                )
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                logger.info("[上行·用户] %s", transcript)
                self._emit_transcript("user", transcript)
        elif event_type in (
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
        ):
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                logger.info("[下行·Agent] %s", transcript)
                suppressed = self._audio_gate.complete_transcript(
                    _response_id(event), transcript
                )
                if not suppressed:
                    self._emit_transcript("agent", transcript)
        elif event_type == "response.function_call_arguments.done":
            name = event.get("name")
            call_id = event.get("call_id")
            arguments = event.get("arguments") or ""
            logger.info("OpenAI 请求调用工具 %s (call_id=%s)", name, call_id)
            if name and call_id:
                # 独立任务执行，避免工具耗时阻塞接收循环；捕获当前连接，
                # 防止工具执行期间断线重连后把旧 call_id 的结果发进新会话。
                asyncio.get_running_loop().create_task(
                    self._dispatch_tool_call(name, call_id, arguments, self._ws)
                )
        elif event_type == "input_audio_buffer.speech_started":
            # server_vad 的打断事件本轮忽略（半双工由 call_agent 管理）。
            pass
        elif event_type == "response.done":
            status = (event.get("response") or {}).get("status")
            if status in ("failed", "incomplete"):
                # 轮次异常结束（内容审核/额度/服务端错误）：连接还活着，
                # 记 error 便于排查"通着但沉默"。
                logger.error("OpenAI 回复轮次异常结束: %s", event.get("response"))
            else:
                self._audio_gate.complete_response(_response_id(event))
                logger.debug("OpenAI 回复轮次完成")
        elif event_type == "error":
            logger.error("OpenAI Realtime 错误: %s", event)

    async def _dispatch_tool_call(
        self, name: str, call_id: str, arguments: str, ws: Any
    ) -> None:
        if call_id in self._handled_tool_calls:
            return
        self._handled_tool_calls.add(call_id)
        # 任何一步失败都要给模型回一个错误形状的结果，否则模型会
        # 永远等 function_call_output（对方只听到沉默）。
        try:
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                logger.warning(
                    "工具 %s 参数解析失败: arguments_length=%d, error_type=%s",
                    name,
                    len(arguments),
                    type(exc).__name__,
                )
                args = {}
            if self._tools is not None:
                # dispatch 可能有阻塞 IO（发短信/AT 指令），放线程池执行。
                result = await asyncio.to_thread(self._tools.dispatch, name, args)
            else:
                result = {"success": False, "message": "无可用工具"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("工具 %s 执行异常: %s", name, exc)
            result = {"success": False, "message": f"工具执行异常: {exc}"}

        if ws is None or ws is not self._ws:
            # 工具执行期间连接已更换/关闭：旧 call_id 对新会话无效，丢弃结果。
            logger.warning(
                "工具 %s 结果因连接已更换而丢弃 (call_id=%s)", name, call_id
            )
            return
        try:
            output = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning("工具 %s 结果无法序列化: %r", name, result)
            output = json.dumps(
                {"success": False, "message": "工具结果无法序列化"},
                ensure_ascii=False,
            )
        try:
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }))
            # hangup 后不再回复；DTMF 后等待 IVR 的下一段音频自然触发回复。
            if name not in TERMINAL_TOOLS and name not in SILENT_AFTER_TOOLS:
                await ws.send(json.dumps({"type": "response.create"}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("回传工具结果失败: %s", exc)
