"""来电会话编排：模组 ↔ 音频桥 ↔ AI Agent。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from queue import Empty, Queue
from typing import Callable

from . import config
from .agents.factory import create_agent
from .agents.tools import ToolRegistry
from .audio_bridge import (
    MODEM_RATE,
    FfmpegAudioBridge,
    ModemAudioBridge,
    SerialPcmAudioBridge,
    apply_pcm_gain,
    create_audio_bridge,
)
from .call_log import CallLogger, CallRecord
from .call_tools import CallTools
from .contacts import is_reply_target_allowed
from .dial_queue import DialQueue, whitelist_from_env
from .dtmf import dtmf_tone
from .events import EventHub
from .modem import Eg25Modem
from .monitor_playback import MonitorPlayback
from .number_profiles import lookup_profile
from .prompt_gen import generate_prompt_scenario
from .prompts import (
    agent_language,
    agent_persona,
    build_instructions,
    default_outbound_task,
    opening_instructions,
    owner_name,
    winddown_instructions,
)
from .summarizer import judge_wrap_up, summarize_call

logger = logging.getLogger(__name__)


AudioBridge = ModemAudioBridge | SerialPcmAudioBridge | FfmpegAudioBridge

# Agent 说话结束后，再屏蔽上行这么久，吸收模组回采的尾音回声。
# （仅作缺省值；每通会话开始时从 config.HALF_DUPLEX_HANGOVER_SECONDS 重新读取。）
HALF_DUPLEX_HANGOVER_SECONDS = 0.5

# 挂断工具触发后延迟这么久再真正挂断，先让 Agent 播完告别语。
# （仅作缺省值；每通会话开始时从 config.HANGUP_TOOL_DELAY_SECONDS 重新读取。）
HANGUP_TOOL_DELAY_SECONDS = 4.5


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
        call_logger: CallLogger | None = None,
        monitor: MonitorPlayback | None = None,
        uplink_monitor: MonitorPlayback | None = None,
        on_ended: Callable[[], None] | None = None,
    ) -> None:
        self.modem = modem
        self.audio_keyword = audio_keyword
        self.provider = provider
        self.audio_mode = audio_mode
        self.pcm_port = pcm_port
        self.pcm_baudrate = pcm_baudrate
        self.tx_gain = tx_gain
        self.hub = hub
        self.call_logger = call_logger
        self.monitor = monitor
        self.uplink_monitor = uplink_monitor
        self._on_ended = on_ended
        self.current_caller: str | None = None
        self._outbound_number: str | None = None
        # 本通外呼主题：start() 显式传入；未传时回退 AGENT_OUTBOUND_TASK 配置。
        self._outbound_task_value: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._active = False
        self._outgoing_audio: Queue[bytes] = Queue()
        self._record: CallRecord | None = None
        self._summary_thread: threading.Thread | None = None
        # 延迟挂断（hangup 工具）状态：CallSession 跨通复用，上一通排下的
        # Timer 必须可取消；世代号兜住已越过 cancel、正在执行的回调，
        # 避免它 stop() 误伤下一通会话。
        self._hangup_timer: threading.Timer | None = None
        self._session_generation = 0
        self._hangup_lock = threading.Lock()
        # 外呼收尾裁判（LLM 判断对话该继续还是收尾，替代关键词枚举）：
        # 请求收尾标志 + 理由 + 在途裁判 task（每通重置）。
        self._wrap_up_requested = False
        self._wrap_up_reason = ""
        self._judge_task: asyncio.Task | None = None
        # 会话级可调参数：每通会话开始时从 config 重新读取，支持不重启改参。
        self._hangover_seconds = HALF_DUPLEX_HANGOVER_SECONDS
        self._hangup_delay_seconds = HANGUP_TOOL_DELAY_SECONDS
        # 外呼动态场景提示词：拨号等待接通时后台生成，接通后限时取用。
        self._prompt_gen_thread: threading.Thread | None = None
        self._prompt_gen_done = threading.Event()
        self._prompt_gen_result: dict | None = None
        self._prompt_gen_timed_out = False
        self._prompt_gen_generation = 0
        self._prompt_gen_opening = ""

    def _publish(self, event: dict) -> None:
        if self.hub:
            self.hub.publish(event)

    @property
    def is_active(self) -> bool:
        return self._active

    def start(self, outbound_number: str | None = None, task: str | None = None) -> None:
        if self._active:
            logger.warning("已有通话进行中，忽略新的呼叫请求")
            return
        self._outbound_number = outbound_number
        self._outbound_task_value = task
        self._wrap_up_requested = False  # 每通重置收尾裁判状态
        self._wrap_up_reason = ""
        self._judge_task = None
        self._prompt_gen_thread = None
        self._prompt_gen_done = threading.Event()
        self._prompt_gen_result = None
        self._prompt_gen_timed_out = False
        self._prompt_gen_generation += 1
        self._prompt_gen_opening = ""
        # 世代号推进与置活必须同锁原子完成：与 _deferred_hangup 的
        # 「校验世代号 → stop()」互斥，保证旧回调要么在新会话置活前跑完
        # （只影响已结束的旧会话），要么校验失败直接放弃。
        with self._hangup_lock:
            self._cancel_hangup_timer()
            self._session_generation += 1
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
            # 会话收尾统一取消未触发的延迟挂断，不让它跨到下一通。
            self._cancel_hangup_timer()
            self._loop.close()
            # 会话结束通知（含异常/未接通路径）：驱动外呼队列拨下一个等。
            if self._on_ended is not None:
                try:
                    self._on_ended()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("会话结束回调异常: %s", exc)

    async def _handle_call(self) -> None:
        self._clear_outgoing_audio()
        self._load_session_config()

        session_t0 = time.monotonic()
        direction = "outbound" if self._outbound_number else "inbound"
        number = self._outbound_number or self.current_caller
        record = self._begin_record(direction, number)
        self._record = record
        transcripts: list[tuple[str, str]] = []

        def mark(event_type: str, **fields) -> None:
            """记录一个会话节点事件，附带相对会话开始的耗时（毫秒）。"""
            if record is not None:
                record.log_event(
                    event_type,
                    t_ms=round((time.monotonic() - session_t0) * 1000, 1),
                    **fields,
                )

        status = "completed"
        try:
            if self._outbound_number:
                if not await self._connect_outbound(mark):
                    status = "not_connected"
                    return
            else:
                logger.info("开始处理来电...")
                self.modem.answer()

            self._publish(
                {"type": "call", "status": "answered", "caller": self.current_caller}
            )
            mark("answered")

            await asyncio.sleep(1.0)

            # 挂断流程会发 AT+QPCMV=0 关闭语音通道，每通电话都要重新启用，
            # 否则第二通开始模组无 PCM 流（双向无声）。
            self.modem.initialize_for_voice(self.audio_mode)

            bridge = create_audio_bridge(
                mode=self.audio_mode,
                device_keyword=self.audio_keyword,
                pcm_port=self.pcm_port,
                pcm_baudrate=self.pcm_baudrate,
                tx_gain=self.tx_gain,
            )
            agent = create_agent(self.provider)
            agent.set_session_instructions(self._build_agent_instructions(direction))
            agent.set_transcript_handler(
                self._make_transcript_handler(record, transcripts)
            )
            agent.set_repeat_stuck_handler(self._request_repeat_stuck_wrap_up)
            agent.set_tools(self._build_tools())
            if isinstance(bridge, SerialPcmAudioBridge):
                bridge.set_ready_check(self.modem.pcm_ready)
            bridge.start()
            mark("bridge_started")

            await agent.start(self._make_agent_audio_handler(agent, bridge, record))
            mark("agent_started")
            await agent.say(self._opening_instructions(direction))
            mark("greeting_sent")

            try:
                await self._run_agent_loop(agent, bridge, record, transcripts)
            finally:
                await self._shutdown_agent(agent, bridge)
        except BaseException:
            status = "failed"
            raise
        finally:
            mark("ended", status=status)
            self._finalize_record(record, status, transcripts, direction, number)

    async def _connect_outbound(self, mark: Callable[..., None]) -> bool:
        """外呼：拨号并等待接通；未接通时发结束事件、挂断并返回 False。"""
        logger.info("开始外呼: %s", self._outbound_number)
        self.current_caller = self._outbound_number
        self._start_prompt_generation()
        self.modem.dial(self._outbound_number)
        mark("dialing", number=self._outbound_number)
        self._publish(
            {"type": "call", "status": "dialing", "caller": self.current_caller}
        )
        connected = await self._wait_connected(timeout=45.0)
        if not connected:
            logger.info("外呼未接通（无人接听/拒接/超时）")
            mark("not_connected")
            self._publish(
                {
                    "type": "call",
                    "status": "ended",
                    "caller": self.current_caller,
                }
            )
            self.modem.hangup()
            return False
        mark("connected")
        return True

    def _make_transcript_handler(
        self,
        record: CallRecord | None,
        transcripts: list[tuple[str, str]],
    ) -> Callable[[str, str], None]:
        """转写回调：累积到 transcripts（供摘要）、写通话记录、推送 UI。"""

        def on_transcript(role: str, text: str) -> None:
            transcripts.append((role, text))
            if record is not None:
                record.log_event("transcript", role=role, text=text)
            self._publish(
                {
                    "type": "transcript",
                    "role": role,
                    "text": text,
                    "caller": self.current_caller,
                }
            )

        return on_transcript

    def _make_agent_audio_handler(
        self, agent, bridge: AudioBridge, record: CallRecord | None
    ) -> Callable[[bytes], None]:
        """Agent 下行音频回调：浏览器实时旁听、本机监听旁路、重采样到 8k、录音、入发送队列。"""

        # 实时旁听按 Agent 输出采样率播放（qwen/openai 均 24k）。
        if self.hub is not None:
            self.hub.set_audio_rate(agent.output_rate)

        def on_agent_audio(pcm_agent: bytes) -> None:
            # 浏览器实时旁听下行 AI（Web Audio）：无监听端时零成本返回。kind=0=下行。
            if self.hub is not None:
                self.hub.broadcast_audio(pcm_agent, kind=0)
            monitor = self.monitor
            if monitor is not None:
                monitor.feed(pcm_agent)
            pcm_8k = bridge.agent_to_modem(pcm_agent, agent.output_rate)
            if hasattr(bridge, "amplify_for_modem"):
                pcm_8k = bridge.amplify_for_modem(pcm_8k)
            if pcm_8k:
                if record is not None:
                    record.write_downlink(pcm_8k)
                self._outgoing_audio.put(pcm_8k)

        return on_agent_audio

    async def _run_agent_loop(
        self,
        agent,
        bridge: AudioBridge,
        record: CallRecord | None,
        transcripts: list[tuple[str, str]],
    ) -> None:
        """通话主循环：下行音频搬运 + 半双工防回环的上行转发。"""
        last_play_at = 0.0
        loop_started = time.monotonic()
        # 外呼硬时限：LLM 收尾裁判失灵/漏判时的最后防线（到点道别挂断）。
        max_seconds = (
            float(config.get_int("OUTBOUND_MAX_SECONDS")) if self._outbound_number else 0.0
        )
        # 收尾裁判（仅外呼）：接通后先给 grace 让通话进正题，之后每 interval 让文本模型
        # 看对话判「继续/收尾」——理解任意措辞（治打转/太早撤），不靠关键词枚举。
        judge_enabled = self._outbound_number is not None
        judge_grace, judge_interval = 20.0, 15.0
        last_judge_at = loop_started
        goal = self._outbound_task(agent_language()) if judge_enabled else ""
        # 浏览器实时旁听：对方上行电平低，推给浏览器前按此增益放大到可闻。
        uplink_listen_gain = config.get_float("MONITOR_UPLINK_GAIN")
        winddown_deadline: float | None = None
        # agent.fatal：实现层判定会话不可恢复（如重连全败）时置位，
        # 结束整通电话而非让对方听沉默。
        while self._active and not agent.fatal:
            self._drain_agent_audio(bridge)

            now = time.monotonic()
            # ① 硬时限兜底
            if (
                max_seconds > 0
                and winddown_deadline is None
                and (now - loop_started) > max_seconds
            ):
                logger.warning("外呼超过 %.0fs 仍在进行，自动道别收尾", max_seconds)
                try:
                    await agent.say(self._winddown_instructions())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("收尾道别发送失败: %s", exc)
                winddown_deadline = now + self._hangup_delay_seconds
            # ② 收尾裁判：到间隔就后台跑一次（asyncio task，不阻塞主循环）
            if (
                judge_enabled
                and winddown_deadline is None
                and not self._wrap_up_requested
                and (now - loop_started) > judge_grace
                and (now - last_judge_at) >= judge_interval
                and (self._judge_task is None or self._judge_task.done())
            ):
                last_judge_at = now
                self._judge_task = asyncio.create_task(
                    self._run_wrap_up_judge(list(transcripts), goal)
                )
            # ③ 裁判判定该收尾 → 说句告别再挂（同硬时限收尾路径）
            if self._wrap_up_requested and winddown_deadline is None:
                logger.info("收尾裁判判定结束（%s），自动收尾", self._wrap_up_reason)
                try:
                    await agent.say(self._winddown_instructions())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("收尾道别发送失败: %s", exc)
                winddown_deadline = now + self._hangup_delay_seconds
            if winddown_deadline is not None and now >= winddown_deadline:
                break
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
            ) < self._hangover_seconds

            pcm_8k = bridge.read_modem_chunk()
            if pcm_8k:
                # 录音不受半双工屏蔽影响（内存追加，非磁盘 IO）。
                if record is not None:
                    record.write_uplink(pcm_8k)
                # 浏览器实时旁听对方声音（kind=1=上行）：放大到可闻后推；不受半双工屏蔽
                # 影响（旁听是单向到浏览器，无回环），让运维能实时听到对方在说什么。
                if self.hub is not None:
                    self.hub.broadcast_audio(
                        apply_pcm_gain(pcm_8k, uplink_listen_gain), kind=1
                    )
                # 本机监听对方声音（8k 旁路，入队即返回）。
                if self.uplink_monitor is not None:
                    self.uplink_monitor.feed(pcm_8k)
                if not suppress_uplink:
                    pcm_agent = bridge.modem_to_agent(pcm_8k, agent.input_rate)
                    await agent.send_audio(pcm_agent)
            await asyncio.sleep(0.01)

    def _load_session_config(self) -> None:
        """每通会话开始时重读可调参数，支持不重启改参。"""
        self._hangover_seconds = config.get_float("HALF_DUPLEX_HANGOVER_SECONDS")
        self._hangup_delay_seconds = config.get_float("HANGUP_TOOL_DELAY_SECONDS")

    def _begin_record(self, direction: str, number: str | None) -> CallRecord | None:
        """创建通话记录；失败只告警不影响通话。"""
        if self.call_logger is None:
            return None
        try:
            return self.call_logger.begin_call(direction, number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建通话记录失败: %s", exc)
            return None

    def _finalize_record(
        self,
        record: CallRecord | None,
        status: str,
        transcripts: list[tuple[str, str]],
        direction: str,
        number: str | None,
    ) -> None:
        """收尾：落盘通话记录，并按需在后台线程生成通话摘要。"""
        if record is None:
            return
        try:
            record.finish(status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("落盘通话记录 %s 失败: %s", record.id, exc)
        self._maybe_summarize(record, transcripts, direction, number)

    def _maybe_summarize(
        self,
        record: CallRecord,
        transcripts: list[tuple[str, str]],
        direction: str,
        number: str | None,
    ) -> None:
        """通话摘要开关打开且对方说过话时，起后台线程生成摘要。"""
        try:
            if not config.get_bool("SUMMARY_ENABLED"):
                return
            if not any(role == "user" and text.strip() for role, text in transcripts):
                return
            thread = threading.Thread(
                target=self._summarize_worker,
                args=(record, list(transcripts), direction, number),
                daemon=True,
                name="call-summary",
            )
            self._summary_thread = thread
            thread.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动通话摘要线程失败: %s", exc)

    def _summarize_worker(
        self,
        record: CallRecord,
        transcripts: list[tuple[str, str]],
        direction: str,
        number: str | None,
    ) -> None:
        """后台线程：调大模型生成结构化摘要并写盘/推送。"""
        try:
            result = summarize_call(transcripts, direction, number)
            if result.get("ok"):
                record.set_summary(result)
                self._publish({"type": "call_summary", "call_id": record.id, **result})
            else:
                logger.warning("通话摘要生成失败: %s", result.get("error"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("通话摘要线程异常: %s", exc)

    def _build_tools(self) -> ToolRegistry:
        """构造本通会话的工具集（工具语义在 call_tools 模块）。"""
        tools = CallTools(
            self.modem,
            hub=self.hub,
            get_caller=lambda: self.current_caller,
            get_record=lambda: self._record,
            schedule_hangup=self._schedule_deferred_hangup,
            is_sms_target_allowed=self._sms_target_allowed,
            send_dtmf=self._send_dtmf_raw,
        )
        return tools.register()

    def _sms_target_allowed(self, number: str) -> bool:
        """发短信目标限制:只允许回复已联系过的号码或当前通话对端。

        取当下的 current_caller(通话中对端可能还没进落盘记录),
        与落盘的短信/来电记录一起判定。
        """
        return is_reply_target_allowed(
            number, self.hub, self.call_logger, extra_allowed=self.current_caller
        )

    def _build_agent_instructions(self, direction: str) -> str:
        """会话系统提示词：文本构造在 prompts 模块（纯函数，可独测）。"""
        lang = agent_language()
        scenario = self._take_prompt_scenario() if direction == "outbound" else None
        return build_instructions(
            direction,
            owner_name(lang),
            agent_persona(lang),
            self._outbound_task(lang),
            lang,
            scenario=scenario,
        )

    def _opening_instructions(self, direction: str) -> str:
        """开场白指令：文本构造在 prompts 模块（纯函数，可独测）。"""
        lang = agent_language()
        return opening_instructions(
            direction,
            owner_name(lang),
            agent_persona(lang),
            self._outbound_task(lang),
            lang,
            opening=self._prompt_gen_opening if direction == "outbound" else None,
        )

    async def _run_wrap_up_judge(
        self, transcripts: list[tuple[str, str]], goal: str
    ) -> None:
        """后台跑一次收尾裁判；判 wrap_up 就置标志，主循环据此说告别并挂断。

        judge_wrap_up 是同步的（内部带超时），放线程池跑，绝不阻塞音频主循环。
        """
        try:
            result = await asyncio.to_thread(judge_wrap_up, transcripts, goal)
        except Exception as exc:  # noqa: BLE001
            logger.debug("收尾裁判调度异常: %s", exc)
            return
        if result.get("decision") == "wrap_up":
            self._wrap_up_requested = True
            self._wrap_up_reason = result.get("reason", "")

    def _request_repeat_stuck_wrap_up(self, reason: str) -> None:
        logger.warning("复读抑制判定会话卡死，准备收尾: %s", reason)
        self._wrap_up_requested = True
        self._wrap_up_reason = reason

    def _start_prompt_generation(self) -> None:
        if not self._outbound_number:
            return
        number = self._outbound_number
        lang = agent_language()
        task = self._outbound_task(lang)
        if config.get_bool("NUMBER_PROFILES_ENABLED"):
            profile = lookup_profile(number, task, lang=lang)
            if profile is not None:
                self._prompt_gen_result = profile
                self._prompt_gen_done.set()
                logger.info(
                    "命中预调教任务库: number=%s task=%s scenario=%.120s",
                    number,
                    task,
                    profile.get("scenario") or "",
                )
                self._log_prompt_gen_result(profile)
                return
        if not config.get_bool("PROMPT_GEN_ENABLED"):
            return
        provider = (self.provider or config.get_str("AGENT_PROVIDER")).strip()
        credential_errors = config.validate_provider_credentials(provider)
        if credential_errors:
            logger.debug(
                "跳过动态场景提示词生成: %s",
                "；".join(credential_errors),
            )
            return
        generation = self._prompt_gen_generation

        def worker() -> None:
            result = generate_prompt_scenario(number, task, lang, provider=provider)
            result["source"] = "generated"
            result["number"] = number
            result["task"] = task
            if generation != self._prompt_gen_generation:
                return
            self._prompt_gen_result = result
            self._prompt_gen_done.set()
            if self._prompt_gen_timed_out:
                return
            self._log_prompt_gen_result(result)

        self._prompt_gen_thread = threading.Thread(
            target=worker, name="prompt-gen", daemon=True
        )
        self._prompt_gen_thread.start()

    def _take_prompt_scenario(self) -> str | None:
        thread = self._prompt_gen_thread
        if thread is None:
            if self._prompt_gen_done.is_set():
                return self._apply_prompt_gen_result()
            return None
        wait_seconds = max(0.0, config.get_float("PROMPT_GEN_WAIT_SECONDS"))
        if not self._prompt_gen_done.wait(wait_seconds):
            self._prompt_gen_timed_out = True
            self._log_prompt_gen_result(
                {
                    "ok": False,
                    "scenario": "",
                    "opening": "",
                    "error": f"等待动态场景提示词超时（>{wait_seconds:g}s）",
                    "provider": self.provider or config.get_str("AGENT_PROVIDER"),
                    "model": config.get_str("PROMPT_GEN_MODEL"),
                    "cached": False,
                    "source": "generated",
                    "number": self._outbound_number or "",
                    "task": self._outbound_task(agent_language()),
                }
            )
            return None
        return self._apply_prompt_gen_result()

    def _apply_prompt_gen_result(self) -> str | None:
        result = self._prompt_gen_result or {}
        self._prompt_gen_opening = str(result.get("opening") or "")
        if result.get("ok") and str(result.get("scenario", "")).strip():
            return str(result["scenario"])
        return None

    def _log_prompt_gen_result(self, result: dict) -> None:
        ok = bool(result.get("ok"))
        scenario = str(result.get("scenario") or "")
        opening = str(result.get("opening") or "")
        error = str(result.get("error") or "")
        model = str(result.get("model") or "")
        provider = str(result.get("provider") or self.provider or "")
        cached = bool(result.get("cached"))
        source = str(result.get("source") or "generated")
        number = str(result.get("number") or self._outbound_number or "")
        task = str(result.get("task") or self._outbound_task(agent_language()))
        if ok:
            if source == "profile":
                logger.info("使用预调教场景提示词: %.120s", scenario)
            else:
                logger.info("动态场景提示词生成成功: %.120s", scenario)
        else:
            logger.info("动态场景提示词未使用: %s", error)
        record = self._record
        if record is not None:
            record.log_event(
                "prompt_gen",
                ok=ok,
                scenario=scenario,
                opening=opening,
                error=error,
                provider=provider,
                model=model,
                cached=cached,
                source=source,
                number=number,
                task=task,
            )

    def _winddown_instructions(self) -> str:
        """收尾道别指令（硬时限或收尾裁判触发时让 AI 说一句简短告别）。"""
        return winddown_instructions(agent_language())

    def _outbound_task(self, lang: str = "zh") -> str:
        """本通外呼主题：start() 显式传入优先，否则回退 AGENT_OUTBOUND_TASK 配置。"""
        task = self._outbound_task_value or config.get_str("AGENT_OUTBOUND_TASK")
        return (task or default_outbound_task(lang)).strip()

    def _schedule_deferred_hangup(self) -> None:
        """排定延迟挂断（hangup 工具触发，HANGUP_TOOL_DELAY_SECONDS 后生效），
        先让 Agent 把告别语播完，避免话没说完线路就断了。
        """
        with self._hangup_lock:
            self._cancel_hangup_timer()
            timer = threading.Timer(
                self._hangup_delay_seconds,
                self._deferred_hangup,
                args=(self._session_generation,),
            )
            self._hangup_timer = timer
            timer.start()

    def send_dtmf(self, digits: str) -> tuple[bool, str | None]:
        """发送 DTMF；UAC 模式默认把双音作为带内 PCM 注入下行队列。"""
        try:
            ok, mode = self._send_dtmf_raw(digits)
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送 DTMF 失败: %s", exc)
            return False, "按键发送失败"
        if ok and self._record is not None:
            self._record.log_event("dtmf", digits=digits, mode=mode)
        return (True, None) if ok else (False, "按键发送失败")

    def _send_dtmf_raw(self, digits: str) -> tuple[bool, str]:
        mode = self._resolve_dtmf_mode()
        ok = True
        if mode in {"inband", "both"}:
            tone = dtmf_tone(digits, MODEM_RATE)
            if not tone:
                return False, mode
            # 与 Agent 语音共用 _outgoing_audio，后续由 _drain_agent_audio
            # 按既有下行链路送入桥；半双工 pending 判定也会自然把它当成正在说话。
            if self._record is not None:
                self._record.write_downlink(tone)
            self._outgoing_audio.put(tone)
        if mode in {"qvts", "both"}:
            ok = self.modem.send_dtmf(digits)
        return ok, mode

    def _resolve_dtmf_mode(self) -> str:
        configured = config.get_str("DTMF_MODE").strip().lower()
        if configured not in {"inband", "qvts", "both"}:
            configured = "inband"
        if self.audio_mode not in {"uac", "uac_ffmpeg"}:
            return "qvts"
        return configured

    def _deferred_hangup(self, generation: int) -> None:
        with self._hangup_lock:
            # 世代号不匹配 = 排定本 Timer 的那通已结束：不得 stop() 新会话。
            if generation != self._session_generation:
                logger.info("延迟挂断已过期（会话已更替），忽略")
                return
            logger.info("工具触发挂断通话")
            self.stop()

    def _cancel_hangup_timer(self) -> None:
        timer = self._hangup_timer
        if timer is not None:
            timer.cancel()
            self._hangup_timer = None

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
        call_logger: CallLogger | None = None,
    ) -> None:
        # modem/call_logger 参数供测试注入；默认按串口/环境配置自建。
        self.modem = modem or Eg25Modem(modem_port, baudrate)
        self.audio_keyword = audio_keyword
        self.provider = provider
        self.audio_mode = audio_mode
        self.hub = hub
        self._ring_lock = threading.Lock()
        # 模组连接状态与后台 supervisor：首启时模组不在也不阻塞 Web，
        # supervisor 反复重连直到成功（首次连上后由 modem 读循环自愈接管）。
        # 注入 modem（测试/直连）视为已就绪；自建的由 supervisor 连上后置 True。
        self.modem_connected = modem is not None
        self._service_running = False
        self._supervisor_thread: threading.Thread | None = None
        self.call_logger = call_logger or CallLogger(
            base_dir=os.getenv("CALL_LOG_DIR", str(config.call_log_dir())),
            recording_enabled=config.get_bool("RECORDING_ENABLED"),
            retention_days=config.get_int("RECORDING_RETENTION_DAYS"),
        )
        self.monitor = self._create_monitor()
        self.uplink_monitor = self._create_uplink_monitor()
        self.dial_queue = DialQueue(
            self.dial,
            whitelist=whitelist_from_env(),
            interval_seconds=config.get_float("DIAL_INTERVAL_SECONDS"),
        )
        self.session = CallSession(
            modem=self.modem,
            audio_keyword=audio_keyword,
            provider=provider,
            audio_mode=audio_mode,
            pcm_port=pcm_port,
            pcm_baudrate=pcm_baudrate,
            tx_gain=tx_gain,
            hub=hub,
            call_logger=self.call_logger,
            monitor=self.monitor,
            uplink_monitor=self.uplink_monitor,
            on_ended=self._handle_session_ended,
        )
        self._setup_callbacks()

    @staticmethod
    def _create_monitor() -> MonitorPlayback | None:
        """按 MONITOR_AI_PLAYBACK 开关构造并启动本地监听播放器（默认关）。

        MonitorPlayback.start() 找不到设备/起进程失败时只告警自禁用，不抛异常。
        """
        if not config.get_bool("MONITOR_AI_PLAYBACK"):
            return None
        monitor = MonitorPlayback(
            config.get_str("MONITOR_OUTPUT_DEVICE"),
            gain=config.get_float("MONITOR_AI_GAIN"),
        )
        monitor.start()
        return monitor

    @staticmethod
    def _create_uplink_monitor() -> MonitorPlayback | None:
        """对方声音（上行 8kHz）的本机监听，独立 ffmpeg 实例，系统自动混音。

        与 AI 下行监听共用 MONITOR_AI_PLAYBACK 开关——用户要听的是"对话"，
        两个方向一起开才成立。
        """
        if not config.get_bool("MONITOR_AI_PLAYBACK"):
            return None
        monitor = MonitorPlayback(
            config.get_str("MONITOR_OUTPUT_DEVICE"),
            sample_rate=8000,
            gain=config.get_float("MONITOR_UPLINK_GAIN"),
        )
        monitor.start()
        return monitor

    def _handle_session_ended(self) -> None:
        """每次会话结束（含来电/外呼/未接通）驱动外呼队列；队列空则 no-op。"""
        try:
            self.dial_queue.on_session_ended()
        except Exception as exc:  # noqa: BLE001
            logger.warning("外呼队列会话结束回调异常: %s", exc)

    def _publish(self, event: dict) -> None:
        if self.hub:
            self.hub.publish(event)

    def _credential_errors(self) -> list[str]:
        provider = self.provider or config.get_str("AGENT_PROVIDER")
        return config.validate_provider_credentials(provider)

    def _reject_if_credentials_missing(self) -> tuple[bool, str | None]:
        errors = self._credential_errors()
        if not errors:
            return False, None
        provider = self.provider or config.get_str("AGENT_PROVIDER")
        self._publish({"type": "config_error", "provider": provider, "errors": errors})
        return True, "；".join(errors)

    def _setup_callbacks(self) -> None:
        def on_ring(caller: str | None) -> None:
            # 同一通来电会被 RING 主动上报和 CLCC 轮询重复触发，需去重：
            # 已有会话进行中时直接忽略，避免重复接听 / 抢占 PCM 串口导致崩溃。
            with self._ring_lock:
                if self.session.is_active:
                    logger.debug("已有通话进行中，忽略重复的 RING/CLCC: %s", caller)
                    return
                logger.info("来电号码: %s", caller or "未知")
                missing_credentials, message = self._reject_if_credentials_missing()
                if missing_credentials:
                    logger.warning("来电未接入 Agent，配置未完成: %s", message)
                    self._publish(
                        {
                            "type": "call",
                            "status": "error",
                            "caller": caller,
                            "error": message,
                        }
                    )
                    return
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

    def dial(self, number: str, task: str | None = None) -> tuple[bool, str | None]:
        """发起外呼：让 Agent 主动拨打指定号码。

        task 非空时作为本通外呼主题并持久化为默认（下次不填主题即沿用）；
        为空则沿用当前 AGENT_OUTBOUND_TASK。
        """
        number = (number or "").strip()
        if not number:
            return False, "号码不能为空"
        # ATD 合法字符集：数字与 +（国际前缀）、*/#（补充业务码）。提前拦住
        # 乱输入，否则占用会话直到 45s 接通超时才释放。
        if not re.fullmatch(r"\+?[0-9*#]{1,32}", number):
            return False, f"号码格式不合法: {number}"
        missing_credentials, message = self._reject_if_credentials_missing()
        if missing_credentials:
            return False, message
        if not self.modem_connected:
            return False, "模组未连接（检查 USB 桥与 EC20）"
        self._remember_outbound_task(task)
        with self._ring_lock:
            if self.session.is_active:
                return False, "当前正在通话中，请稍后再拨"
            self.session.current_caller = number
            self.session.start(outbound_number=number, task=task)
        return True, None

    def hangup(self) -> tuple[bool, str | None]:
        """挂断进行中的通话（AI 与 IVR 互相不挂断时的人工兜底）。"""
        if not self.session.is_active:
            return False, "当前没有进行中的通话"
        self.session.stop()
        return True, None

    def send_dtmf(self, digits: str) -> tuple[bool, str | None]:
        """通话中人工发送 DTMF 按键（IVR 菜单导航）。"""
        if not self.session.is_active:
            return False, "当前没有进行中的通话"
        return self.session.send_dtmf(digits)

    @staticmethod
    def _remember_outbound_task(task: str | None) -> None:
        """把外呼主题写入运行环境并持久化到 .env（成为下次默认）。"""
        task = (task or "").strip()
        if not task:
            return
        # 幂等跳过：批量外呼时每通拨号都经由 dial() 走到这里（task 相同），
        # 不跳过会对 .env 重复整写 N 次，放大磁盘 IO 与并发写窗口。
        if os.environ.get("AGENT_OUTBOUND_TASK") == task:
            return
        os.environ["AGENT_OUTBOUND_TASK"] = task
        try:
            config.update_env_file({"AGENT_OUTBOUND_TASK": task})
        except Exception as exc:  # noqa: BLE001
            logger.warning("外呼主题持久化失败（本通仍生效）: %s", exc)

    def batch_dial(self, numbers: list[str], task: str | None = None) -> dict:
        """批量外呼：号码入队后按 DIAL_INTERVAL_SECONDS 间隔依次拨打。

        返回 {"accepted": [已入队号码], "rejected": [被拒号码]}；
        白名单（DIAL_WHITELIST）不放行、空号码、重复号码会被拒。
        task 非空时作为本批次的外呼任务指令（队列拨号时随号码显式传给
        dial），并持久化为下次默认主题。
        """
        self._remember_outbound_task(task)
        return self.dial_queue.enqueue(numbers, task)

    def dial_queue_status(self) -> dict:
        """外呼队列状态：{"pending", "current", "done", "active"}（供 web 层查询）。"""
        return self.dial_queue.status()

    def start(self) -> None:
        """非阻塞启动：Web 服务不依赖模组，模组连接交给后台 supervisor 反复重试。

        模组不在/桥没起时不再抛错——界面照常可用并显示"模组未连接"，
        supervisor 连上后自动开始接听。首次连上后，后续掉线由 modem 读循环
        的自愈重连（P0-1）接管。
        """
        try:
            purged = self.call_logger.purge_expired()
            if purged:
                logger.info("已清理 %d 条过期通话记录", purged)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理过期通话记录失败: %s", exc)
        self._service_running = True
        self._supervisor_thread = threading.Thread(
            target=self._modem_supervisor, name="modem-supervisor", daemon=True
        )
        self._supervisor_thread.start()

    def _modem_supervisor(self) -> None:
        """后台反复尝试连接模组直到成功，期间向 UI 广播连接状态。"""
        delay = 2.0
        while self._service_running:
            try:
                self.modem.connect()
                self.modem.initialize_for_voice(self.audio_mode)
                self.modem.start_listener()
            except Exception as exc:  # noqa: BLE001
                if not self._service_running:
                    return
                self._set_modem_connected(False, str(exc))
                logger.warning("模组连接失败，%.0fs 后重试: %s", delay, exc)
                # 可被 stop() 打断的等待
                for _ in range(int(delay * 10)):
                    if not self._service_running:
                        return
                    time.sleep(0.1)
                delay = min(delay * 2, 30.0)
                continue
            # 复查：初始化序列执行期间若已 stop_service()，立即收拾干净退出，
            # 不让本轮 start_listener() 复活出的读线程去重连一个本该关停的模组。
            if not self._service_running:
                self.modem.close()
                return
            self._set_modem_connected(True)
            logger.info("模组已连接，等待来电…")
            return

    def _set_modem_connected(self, connected: bool, error: str | None = None) -> None:
        """更新模组连接状态并广播给 UI（仅状态翻转时发事件，避免重连期刷屏）。"""
        if connected == self.modem_connected:
            return
        self.modem_connected = connected
        event = {"type": "modem_status", "connected": connected}
        if error:
            event["error"] = error
        self._publish(event)
        if connected:
            self._publish({"type": "system", "text": "服务已启动，等待来电"})

    def stop_service(self) -> None:
        """停止 supervisor 与当前会话，关闭模组（供退出时调用）。"""
        self._service_running = False
        self.session.stop()
        self.modem.close()

    def run(self) -> None:
        self.start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            logger.info("收到退出信号")
        finally:
            self.stop_service()
