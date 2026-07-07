"""来电会话编排：模组 ↔ 音频桥 ↔ AI Agent。"""

from __future__ import annotations

import asyncio
import logging
import re
from queue import Empty, Queue
import threading
import time

from .agents.factory import create_agent
from .agents.tools import (
    HANGUP_SPEC,
    QUERY_CODE_SPEC,
    SEND_SMS_SPEC,
    ToolRegistry,
)
from .audio_bridge import ModemAudioBridge, SerialPcmAudioBridge, create_audio_bridge
from .events import EventHub
from .modem import Eg25Modem

logger = logging.getLogger(__name__)


AudioBridge = ModemAudioBridge | SerialPcmAudioBridge

# Agent 说话结束后，再屏蔽上行这么久，吸收模组回采的尾音回声。
HALF_DUPLEX_HANGOVER_SECONDS = 0.5


class CallSession:
    def __init__(
        self,
        modem: Eg25Modem,
        audio_keyword: str,
        provider: str | None,
        audio_mode: str,
        pcm_port: str | None,
        pcm_baudrate: int,
        tx_gain: float,
        hub: EventHub | None = None,
    ) -> None:
        self.modem = modem
        self.audio_keyword = audio_keyword
        self.provider = provider
        self.audio_mode = audio_mode
        self.pcm_port = pcm_port
        self.pcm_baudrate = pcm_baudrate
        self.tx_gain = tx_gain
        self.hub = hub
        self.current_caller: str | None = None
        self._outbound_number: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._active = False
        self._outgoing_audio: Queue[bytes] = Queue()

    def _publish(self, event: dict) -> None:
        if self.hub:
            self.hub.publish(event)

    @property
    def is_active(self) -> bool:
        return self._active

    def start(self, outbound_number: str | None = None) -> None:
        if self._active:
            logger.warning("已有通话进行中，忽略新的呼叫请求")
            return
        self._outbound_number = outbound_number
        self._active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._active = False
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._handle_call())
        except Exception as exc:  # noqa: BLE001
            logger.exception("通话处理异常: %s", exc)
        finally:
            self._active = False
            self._loop.close()

    async def _handle_call(self) -> None:
        self._clear_outgoing_audio()

        if self._outbound_number:
            logger.info("开始外呼: %s", self._outbound_number)
            self.current_caller = self._outbound_number
            self.modem.dial(self._outbound_number)
            self._publish(
                {"type": "call", "status": "dialing", "caller": self.current_caller}
            )
            connected = await self._wait_connected(timeout=45.0)
            if not connected:
                logger.info("外呼未接通（无人接听/拒接/超时）")
                self._publish(
                    {"type": "call", "status": "ended", "caller": self.current_caller}
                )
                self.modem.hangup()
                return
        else:
            logger.info("开始处理来电...")
            self.modem.answer()

        self._publish(
            {"type": "call", "status": "answered", "caller": self.current_caller}
        )

        await asyncio.sleep(1.0)

        bridge = create_audio_bridge(
            mode=self.audio_mode,
            device_keyword=self.audio_keyword,
            pcm_port=self.pcm_port,
            pcm_baudrate=self.pcm_baudrate,
            tx_gain=self.tx_gain,
        )
        agent = create_agent(self.provider)
        agent.set_transcript_handler(
            lambda role, text: self._publish(
                {
                    "type": "transcript",
                    "role": role,
                    "text": text,
                    "caller": self.current_caller,
                }
            )
        )
        agent.set_tools(self._build_tools())
        if isinstance(bridge, SerialPcmAudioBridge):
            bridge.set_ready_check(self.modem.pcm_ready)
        bridge.start()

        def on_agent_audio(pcm_agent: bytes) -> None:
            pcm_8k = bridge.agent_to_modem(pcm_agent, agent.output_rate)
            if hasattr(bridge, "amplify_for_modem"):
                pcm_8k = bridge.amplify_for_modem(pcm_8k)
            if pcm_8k:
                self._outgoing_audio.put(pcm_8k)

        await agent.start(on_agent_audio)
        model_name = getattr(agent, "model_display_name", "当前语音模型")
        await agent.say(
            "请用中文说：您好，我是红茶语音助手，已经接入电话。"
            f"我的底层模型是{model_name}。"
            "请问有什么可以帮您？"
        )

        last_play_at = 0.0
        try:
            while self._active:
                self._drain_agent_audio(bridge)

                now = time.monotonic()
                pending = (
                    bridge.pending_output_bytes()
                    if hasattr(bridge, "pending_output_bytes")
                    else 0
                )
                agent_speaking = pending > 0 or not self._outgoing_audio.empty()
                if agent_speaking:
                    last_play_at = now
                # 半双工防回环：Agent 说话期间（含挂尾窗口）丢弃上行，
                # 避免模组把下行音频回采给千问导致自循环。
                suppress_uplink = agent_speaking or (
                    now - last_play_at
                ) < HALF_DUPLEX_HANGOVER_SECONDS

                pcm_8k = bridge.read_modem_chunk()
                if pcm_8k and not suppress_uplink:
                    pcm_agent = bridge.modem_to_agent(pcm_8k, agent.input_rate)
                    await agent.send_audio(pcm_agent)
                await asyncio.sleep(0.01)
        finally:
            await self._shutdown_agent(agent, bridge)

    def _build_tools(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(SEND_SMS_SPEC, self._tool_send_sms)
        registry.register(HANGUP_SPEC, self._tool_hangup)
        registry.register(QUERY_CODE_SPEC, self._tool_query_code)
        return registry

    def _tool_send_sms(self, args: dict) -> dict:
        """工具处理：Agent 在通话中请求发送短信。"""
        number = (args.get("to") or "").strip() or (self.current_caller or "").strip()
        content = (args.get("content") or "").strip()
        if not number:
            return {"success": False, "message": "没有可用的收件号码"}
        if not content:
            return {"success": False, "message": "短信内容为空"}
        try:
            ok = self.modem.send_sms(number, content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("工具发送短信失败: %s", exc)
            return {"success": False, "message": f"发送失败: {exc}"}
        if ok:
            self._publish(
                {
                    "type": "sms_out",
                    "number": number,
                    "text": content,
                    "status": "sent",
                }
            )
        return {
            "success": ok,
            "to": number,
            "content": content,
            "message": "短信已发送" if ok else "短信发送失败",
        }

    def _tool_hangup(self, args: dict) -> dict:
        """工具处理：Agent 请求挂断当前通话。

        延迟挂断，先让 Agent 把告别语播完，避免话没说完线路就断了。
        """
        threading.Timer(4.5, self._deferred_hangup).start()
        return {"success": True, "message": "好的，马上为您挂断电话"}

    def _deferred_hangup(self) -> None:
        logger.info("工具触发挂断通话")
        self.stop()

    def _tool_query_code(self, args: dict) -> dict:
        """工具处理：从最近收到的短信里查验证码。"""
        code, text, sender = self._find_latest_code()
        if code:
            return {
                "success": True,
                "code": code,
                "sender": sender,
                "sms_text": text,
                "message": f"最近收到的验证码是 {code}",
            }
        return {"success": False, "message": "最近没有收到含验证码的短信"}

    def _find_latest_code(self) -> tuple[str | None, str | None, str | None]:
        """在已收到的短信中查找最近的数字验证码。

        优先匹配含“验证码/校验码/code”等关键词的短信，找不到再退回任意含
        4-8 位数字的短信。返回 (验证码, 短信全文, 发件号码)。
        """
        if not self.hub:
            return None, None, None
        sms_events = [e for e in self.hub.history() if e.get("type") == "sms_in"]
        code_re = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
        keyword_re = re.compile(r"验证码|校验码|动态码|verification|code|otp", re.I)

        def scan(prefer_keyword: bool) -> tuple[str | None, str | None, str | None]:
            for event in reversed(sms_events):
                text = event.get("text") or ""
                if prefer_keyword and not keyword_re.search(text):
                    continue
                match = code_re.search(text)
                if match:
                    return match.group(1), text, event.get("sender")
            return None, None, None

        result = scan(prefer_keyword=True)
        if result[0]:
            return result
        return scan(prefer_keyword=False)

    async def _wait_connected(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._active and time.monotonic() < deadline:
            if self.modem.is_call_connected():
                return True
            await asyncio.sleep(0.2)
        return False

    async def _shutdown(self) -> None:
        self._active = False

    async def _shutdown_agent(self, agent, bridge: AudioBridge | None) -> None:
        if agent is not None:
            try:
                await agent.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 Agent 出错: %s", exc)
        if bridge is not None:
            try:
                bridge.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭音频桥出错: %s", exc)
        # 会话结束（含异常/端口冲突）时确保挂断物理通话，避免线路悬空。
        try:
            self.modem.hangup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("挂断物理通话出错: %s", exc)
        logger.info("通话 Agent 会话已结束")
        self._publish(
            {"type": "call", "status": "ended", "caller": self.current_caller}
        )

    def _drain_agent_audio(self, bridge: AudioBridge) -> None:
        chunks: list[bytes] = []
        while True:
            try:
                chunks.append(self._outgoing_audio.get_nowait())
            except Empty:
                break
        if chunks:
            bridge.write_modem_chunks(chunks)

    def _clear_outgoing_audio(self) -> None:
        while True:
            try:
                self._outgoing_audio.get_nowait()
            except Empty:
                break


class CallAgentService:
    """常驻服务：监听 EG25 来电并自动接入 Agent。"""

    def __init__(
        self,
        modem_port: str,
        audio_keyword: str,
        provider: str | None = None,
        baudrate: int = 115200,
        audio_mode: str = "uac",
        pcm_port: str | None = None,
        pcm_baudrate: int = 921600,
        tx_gain: float = 1.0,
        hub: EventHub | None = None,
        modem: Eg25Modem | None = None,
    ) -> None:
        # modem 参数供测试注入 FakeModem；默认按串口配置自建。
        self.modem = modem or Eg25Modem(modem_port, baudrate)
        self.audio_keyword = audio_keyword
        self.provider = provider
        self.audio_mode = audio_mode
        self.hub = hub
        self._ring_lock = threading.Lock()
        self.session = CallSession(
            modem=self.modem,
            audio_keyword=audio_keyword,
            provider=provider,
            audio_mode=audio_mode,
            pcm_port=pcm_port,
            pcm_baudrate=pcm_baudrate,
            tx_gain=tx_gain,
            hub=hub,
        )
        self._setup_callbacks()

    def _publish(self, event: dict) -> None:
        if self.hub:
            self.hub.publish(event)

    def _setup_callbacks(self) -> None:
        def on_ring(caller: str | None) -> None:
            # 同一通来电会被 RING 主动上报和 CLCC 轮询重复触发，需去重：
            # 已有会话进行中时直接忽略，避免重复接听 / 抢占 PCM 串口导致崩溃。
            with self._ring_lock:
                if self.session.is_active:
                    logger.debug("已有通话进行中，忽略重复的 RING/CLCC: %s", caller)
                    return
                logger.info("来电号码: %s", caller or "未知")
                self.session.current_caller = caller
                self._publish({"type": "call", "status": "ringing", "caller": caller})
                self.session.start()

        def on_hangup() -> None:
            self.session.stop()
            self.modem.hangup()

        def on_sms(sender: str | None, text: str) -> None:
            logger.info("收到短信 来自=%s 内容=%s", sender or "未知", text)
            self._publish({"type": "sms_in", "sender": sender, "text": text})

        self.modem.on_ring(on_ring)
        self.modem.on_hangup(on_hangup)
        self.modem.on_sms(on_sms)

    def dial(self, number: str) -> tuple[bool, str | None]:
        """发起外呼：让 Agent 主动拨打指定号码。"""
        number = (number or "").strip()
        if not number:
            return False, "号码不能为空"
        with self._ring_lock:
            if self.session.is_active:
                return False, "当前正在通话中，请稍后再拨"
            self.session.current_caller = number
            self.session.start(outbound_number=number)
        return True, None

    def start(self) -> None:
        """非阻塞启动：连接模组、启用语音、开始监听（供网页模式调用）。"""
        self.modem.connect()
        self.modem.initialize_for_voice(self.audio_mode)
        self.modem.start_listener()
        logger.info("Agent助手 服务已启动，等待来电...")
        self._publish({"type": "system", "text": "服务已启动，等待来电"})

    def run(self) -> None:
        self.start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            logger.info("收到退出信号")
        finally:
            self.session.stop()
            self.modem.close()
