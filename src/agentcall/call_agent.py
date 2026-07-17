"""来电会话编排：模组 ↔ 音频桥 ↔ AI Agent。"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime
from queue import Empty, Full, Queue
from typing import Callable

from . import config
from .agents.base import VoiceAgent
from .agents.factory import create_agent
from .agents.tools import REQUEST_OWNER_TAKEOVER_SPEC, ToolRegistry
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
from .dial_guard import DialGuardFailure, check_dial_guard
from .dial_queue import DialQueue, whitelist_from_env
from .dtmf import dtmf_tone
from .dtmf_followup import extract_spoken_dtmf
from .dtmf_judge import DtmfActionLedger, DtmfJudge, WindowMode
from .events import EventHub
from .modem import Eg25Modem
from .monitor_playback import MonitorPlayback
from .number_profiles import lookup_profile, lookup_profile_by_id
from .pcm_stats import PcmFlowStats
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
from .rate_limit import acquire_remote_dial_slot
from .remote_dialer import (
    IssuedLiveKitSession,
    RemoteDialerInvite,
    RemoteDialerRuntimeConfig,
    RemoteDialerWorker,
    RemoteMediaEndpoint,
    RemoteWebDialerCoordinator,
    issue_livekit_session,
)
from .result_verification import (
    apply_carrier_sms_verification,
    carrier_sms_evidence,
    is_carrier_service_call,
)
from .sim_identity import SimIdentity
from .sms_email_forwarder import SmsEmailForwarder
from .summarizer import judge_wrap_up, summarize_call
from .takeover_coordinator import (
    ClaimFence,
    InboundTakeoverCoordinator,
    InboundTakeoverOfferRequest,
    InboundTakeoverRevoke,
    InboundTakeoverSession,
    MediaOwner,
    TakeoverAction,
    TakeoverOffer,
    TakeoverRejection,
    TakeoverResult,
    TakeoverState,
)
from .triage_judge import (
    InboundTriageJudge,
    TriageConsumption,
    TriageVerdict,
    TriageVerdictConsumer,
)

logger = logging.getLogger(__name__)


AudioBridge = ModemAudioBridge | SerialPcmAudioBridge | FfmpegAudioBridge

# Agent 说话结束后，再屏蔽上行这么久，吸收模组回采的尾音回声。
# （仅作缺省值；每通会话开始时从 config.HALF_DUPLEX_HANGOVER_SECONDS 重新读取。）
HALF_DUPLEX_HANGOVER_SECONDS = 0.5

# 挂断工具触发后延迟这么久再真正挂断，先让 Agent 播完告别语。
# （仅作缺省值；每通会话开始时从 config.HANGUP_TOOL_DELAY_SECONDS 重新读取。）
HANGUP_TOOL_DELAY_SECONDS = 4.5

# Profile-gated fallback: wait briefly for a genuine tool call after the Agent
# says it is pressing a key. The recent-send window closes transcript/tool races.
DTMF_SPOKEN_FOLLOWUP_DELAY_SECONDS = 3.0
DTMF_RECENT_SEND_WINDOW_SECONDS = 5.0
_EXTERNAL_TOOL_RESULT_TIMEOUT_SECONDS = 2.0
_INBOUND_TAKEOVER_OFFER_TTL_SECONDS = 30.0
_INBOUND_TAKEOVER_HOLD_TEXT = "请稍等，我确认一下，马上帮您转接。"
_INBOUND_TAKEOVER_MEDIA_TIMEOUT_SECONDS = 15.0
_INBOUND_TRIAGE_CLARIFY_TEXT = "请简单确认一下，您是有具体事情找本人，还是一般业务介绍？"
_INBOUND_TRIAGE_REJECT_TEXT = "谢谢您的来电，目前不需要这项服务。再见。"


class _CallSessionMediaRouter:
    """Phase-B owner fence; Phase C attaches concrete bridge routing."""

    def __init__(self) -> None:
        self._owner = MediaOwner.AI
        self._lock = threading.Lock()

    @property
    def owner(self) -> MediaOwner:
        with self._lock:
            return self._owner

    def switch_owner(self, owner: MediaOwner) -> None:
        with self._lock:
            self._owner = owner


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
        takeover_endpoint_factory: Callable[[IssuedLiveKitSession], RemoteMediaEndpoint]
        | None = None,
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
        self._takeover_endpoint_factory = takeover_endpoint_factory
        self.current_caller: str | None = None
        self._outbound_number: str | None = None
        # 本通外呼主题：start() 显式传入；未传时回退 AGENT_OUTBOUND_TASK 配置。
        self._outbound_task_value: str | None = None
        # 命中键：选中预设时锁定"用哪条预设"（预设原任务），与事项框的具体子主题解耦。
        self._preset_hint: str | None = None
        # 稳定预设 ID：新界面优先按 ID 命中；preset_hint 只保留给旧客户端兼容回退。
        self._preset_id: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._active = False
        self._active_lock = threading.Lock()
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
        self._prompt_gen_opening_mode = "say"
        self._prompt_gen_dtmf_spoken_followup = False
        self._result_verification_mode = "none"
        self._dtmf_lock = threading.RLock()
        self._recent_dtmf_sent: dict[str, tuple[float, str]] = {}
        self._pending_dtmf_followups: dict[
            int,
            tuple[
                threading.Timer,
                str,
                int,
                float,
                VoiceAgent,
                CallRecord | None,
            ],
        ] = {}
        self._next_dtmf_followup_id = 0
        self._dtmf_dispatch_context = threading.local()
        self._active_tools: ToolRegistry | None = None
        self._dtmf_ledger: DtmfActionLedger | None = None
        self._dtmf_judge: DtmfJudge | None = None
        self._dtmf_judge_started_at = 0.0
        self._takeover_lock = threading.RLock()
        self._takeover_coordinator: InboundTakeoverCoordinator | None = None
        self._takeover_request: InboundTakeoverOfferRequest | None = None
        self._takeover_offer_queue: Queue[InboundTakeoverOfferRequest] = Queue(
            maxsize=1
        )
        self._takeover_revoke_queue: Queue[InboundTakeoverRevoke] = Queue(maxsize=1)
        self._takeover_session_queue: Queue[InboundTakeoverSession] = Queue(maxsize=1)
        self._takeover_hold_generation: int | None = None
        self._takeover_hold_done = False
        self._triage_judge: InboundTriageJudge | None = None
        self._triage_mode = "off"
        self._triage_results: Queue[TriageVerdict] = Queue(maxsize=4)
        self._triage_consumer = TriageVerdictConsumer()
        self._triage_pending = False
        self._triage_terminal = False
        self._triage_reject_deadline: float | None = None
        self._triage_clarification_spoken = False

    def _publish(self, event: dict) -> None:
        if self.hub:
            self.hub.publish(event)

    @property
    def is_active(self) -> bool:
        with self._active_lock:
            return self._active

    def _set_active(self, value: bool) -> None:
        with self._active_lock:
            self._active = value

    def start(
        self,
        outbound_number: str | None = None,
        task: str | None = None,
        preset_hint: str | None = None,
        preset_id: str | None = None,
    ) -> None:
        if self.is_active:
            logger.warning("已有通话进行中，忽略新的呼叫请求")
            return
        self._outbound_number = outbound_number
        self._outbound_task_value = task
        self._preset_hint = preset_hint
        self._preset_id = preset_id
        self._wrap_up_requested = False  # 每通重置收尾裁判状态
        self._wrap_up_reason = ""
        self._judge_task = None
        self._prompt_gen_thread = None
        self._prompt_gen_done = threading.Event()
        self._prompt_gen_result = None
        self._prompt_gen_timed_out = False
        self._prompt_gen_generation += 1
        self._prompt_gen_opening = ""
        self._prompt_gen_opening_mode = "say"
        self._prompt_gen_dtmf_spoken_followup = False
        self._result_verification_mode = "none"
        self._cancel_spoken_dtmf_followups(clear_recent=True)
        self._stop_dtmf_judge(join_timeout=0.0)
        self._stop_triage_judge(join_timeout=0.0)
        self._dtmf_judge_started_at = 0.0
        # 世代号推进与置活必须同锁原子完成：与 _deferred_hangup 的
        # 「校验世代号 → stop()」互斥，保证旧回调要么在新会话置活前跑完
        # （只影响已结束的旧会话），要么校验失败直接放弃。
        with self._hangup_lock:
            self._cancel_hangup_timer()
            self._session_generation += 1
            self._set_active(True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._dtmf_lock:
            self._set_active(False)
            self._cancel_spoken_dtmf_followups_locked()
        self._stop_dtmf_judge()
        self._stop_triage_judge()
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
            with self._dtmf_lock:
                self._set_active(False)
                self._cancel_spoken_dtmf_followups_locked()
                self._active_tools = None
            self._stop_dtmf_judge()
            self._stop_triage_judge()
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
        self._initialize_takeover_context(direction)
        self._initialize_triage_context(direction, record)
        transcripts: list[tuple[str, str]] = []

        def mark(event_type: str, **fields) -> float:
            """记录一个会话节点事件，附带相对会话开始的耗时（毫秒）。"""
            now = time.monotonic()
            if record is not None:
                record.log_event(
                    event_type,
                    t_ms=round((now - session_t0) * 1000, 1),
                    **fields,
                )
            return now

        first_audio_lock = threading.Lock()
        first_audio_state = {
            "greeting_sent_at": 0.0,
            "greeting_ready": False,
            "logged": False,
            "pending_before_greeting": False,
        }

        def log_first_audio(ms: int) -> None:
            if record is not None:
                record.log_event("first_audio", ms=ms)
            logger.info("首音频延迟: %dms", ms)

        def note_first_agent_audio() -> None:
            with first_audio_lock:
                if first_audio_state["logged"]:
                    return
                if not first_audio_state["greeting_ready"]:
                    first_audio_state["pending_before_greeting"] = True
                    return
                first_audio_state["logged"] = True
                started_at = float(first_audio_state["greeting_sent_at"])
                ms = max(0, round((time.monotonic() - started_at) * 1000))
            log_first_audio(ms)

        def mark_greeting_sent() -> None:
            marked_at = mark("greeting_sent")
            should_log_pending = False
            with first_audio_lock:
                first_audio_state["greeting_sent_at"] = marked_at
                first_audio_state["greeting_ready"] = True
                if (
                    first_audio_state["pending_before_greeting"]
                    and not first_audio_state["logged"]
                ):
                    first_audio_state["logged"] = True
                    should_log_pending = True
            if should_log_pending:
                log_first_audio(0)

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
            self._start_dtmf_judge(record, session_t0=session_t0)
            agent.set_session_instructions(self._build_agent_instructions(direction))
            agent.set_transcript_handler(
                self._make_transcript_handler(record, transcripts, agent)
            )
            agent.set_repeat_stuck_handler(self._request_repeat_stuck_wrap_up)
            # 面向用户的状态提示（如 local provider 首启下载模型进度）经 EventHub 播给 UI。
            agent.set_status_handler(
                lambda text: self._publish({"type": "system", "text": text})
            )
            tools = self._build_tools(direction)
            with self._dtmf_lock:
                self._active_tools = tools
            agent.set_tools(tools)
            if isinstance(bridge, SerialPcmAudioBridge):
                bridge.set_ready_check(self.modem.pcm_ready)
            bridge.start()
            mark("bridge_started")

            await agent.start(
                self._make_agent_audio_handler(
                    agent, bridge, record, note_first_agent_audio
                )
            )
            mark("agent_started")
            # #80-B:IVR 热线 profile 可声明 opening_mode=wait——不发开场白,
            # 静默等对方(菜单播报)先说,避免 AI 开场压掉首段 IVR。仅外呼且
            # profile 显式 wait 时生效;人呼人/来电行为不变。
            if direction == "outbound" and self._prompt_gen_opening_mode == "wait":
                mark("opening_skipped", mode="wait")
                logger.info("按 profile opening_mode=wait 跳过开场白,等待对方先说")
            else:
                await agent.say(self._opening_instructions(direction))
                mark_greeting_sent()

            active_bridge = bridge
            try:
                active_bridge = await self._run_agent_loop(
                    agent, bridge, record, transcripts
                )
            finally:
                await self._shutdown_agent(agent, active_bridge)
        except BaseException:
            status = "failed"
            raise
        finally:
            # 先作废判官世代再 finish record，避免迟到结果在 meta 落盘后追加事件。
            self._stop_dtmf_judge()
            self._stop_triage_judge()
            self._end_takeover_context("CALL_ENDED")
            mark("ended", status=status)
            self._finalize_record(record, status, transcripts, direction, number)

    async def _connect_outbound(self, mark: Callable[..., float]) -> bool:
        """外呼：拨号并等待接通；未接通时发结束事件、挂断并返回 False。"""
        number = self._outbound_number
        if number is None:
            raise RuntimeError("外呼号码未设置")
        logger.info("开始外呼: %s", number)
        self.current_caller = number
        self._start_prompt_generation()
        self.modem.dial(number)
        mark("dialing", number=number)
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
        agent: VoiceAgent,
    ) -> Callable[[str, str], None]:
        """转写回调：累积到 transcripts（供摘要）、写通话记录、推送 UI。"""

        generation = self._session_generation

        def on_transcript(role: str, text: str) -> None:
            if not self._agent_effect_allowed(generation):
                return
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
            if role == "agent":
                self._schedule_spoken_dtmf_followup(agent, text)
            elif role == "user":
                judge = self._dtmf_judge
                if judge is not None:
                    judge.submit_remote_transcript(
                        text,
                        t_ms=max(
                            0.0,
                            (time.monotonic() - self._dtmf_judge_started_at) * 1000,
                        ),
                    )
            triage = self._triage_judge
            if triage is not None:
                triage.submit_turn(role, text)

        return on_transcript

    def _make_agent_audio_handler(
        self,
        agent,
        bridge: AudioBridge,
        record: CallRecord | None,
        on_first_audio: Callable[[], None] | None = None,
    ) -> Callable[[bytes], None]:
        """Agent 下行音频回调：浏览器实时旁听、本机监听旁路、重采样到 8k、录音、入发送队列。"""

        # 实时旁听按 Agent 输出采样率播放（qwen/openai 均 24k）。
        if self.hub is not None:
            self.hub.set_audio_rate(agent.output_rate)

        generation = self._session_generation

        def on_agent_audio(pcm_agent: bytes) -> None:
            if not self._agent_effect_allowed(generation):
                return
            if pcm_agent and on_first_audio is not None:
                on_first_audio()
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

    async def _speak_takeover_hold_if_needed(
        self, agent: VoiceAgent, bridge: AudioBridge, generation: int
    ) -> None:
        """Play one deterministic hold line before the AI transport is fenced."""

        with self._takeover_lock:
            coordinator = self._takeover_coordinator
            should_speak = (
                coordinator is not None
                and coordinator.state is TakeoverState.TAKEOVER_PREPARING
                and self._takeover_hold_generation == generation
                and not self._takeover_hold_done
            )
        if not should_speak:
            return
        try:
            await agent.say(_INBOUND_TAKEOVER_HOLD_TEXT)
            # Flush the one permitted hold line before closing the AI gate; the
            # regular loop deliberately drops queued AI audio after this point.
            self._drain_agent_audio(bridge)
        except Exception as exc:  # noqa: BLE001
            logger.warning("播放接管垫话失败: error_type=%s", type(exc).__name__)
        finally:
            with self._takeover_lock:
                self._takeover_hold_done = True

    async def _run_agent_loop(
        self,
        agent,
        bridge: AudioBridge,
        record: CallRecord | None,
        transcripts: list[tuple[str, str]],
    ) -> AudioBridge:
        """通话主循环：下行音频搬运 + 半双工防回环的上行转发。"""
        last_play_at = 0.0
        loop_started = time.monotonic()
        # 外呼硬时限：LLM 收尾裁判失灵/漏判时的最后防线（到点道别挂断）。
        outbound_max_seconds = (
            float(config.get_int("OUTBOUND_MAX_SECONDS")) if self._outbound_number else 0.0
        )
        inbound_max_seconds = (
            float(config.get_int("INBOUND_MAX_SECONDS"))
            if self._outbound_number is None
            else 0.0
        )
        if self._outbound_number is None and inbound_max_seconds <= 0:
            inbound_max_seconds = float(
                config.get_spec("INBOUND_MAX_SECONDS").default
            )
            logger.warning(
                "INBOUND_MAX_SECONDS 必须大于 0，当前通话回落为 %.0fs",
                inbound_max_seconds,
            )
        # 收尾裁判（仅外呼）：接通后先给 grace 让通话进正题，之后每 interval 让文本模型
        # 看对话判「继续/收尾」——理解任意措辞（治打转/太早撤），不靠关键词枚举。
        judge_enabled = self._outbound_number is not None
        judge_grace = config.get_float("WRAP_UP_JUDGE_GRACE_SECONDS")
        judge_interval = config.get_float("WRAP_UP_JUDGE_INTERVAL_SECONDS")
        last_judge_at = loop_started
        goal = self._outbound_task(agent_language()) if judge_enabled else ""
        # 浏览器实时旁听：对方上行电平低，推给浏览器前按此增益放大到可闻。
        uplink_listen_gain = config.get_float("MONITOR_UPLINK_GAIN")
        agent_uplink_gain = config.get_float("AGENT_UPLINK_GAIN")
        if not math.isfinite(agent_uplink_gain) or agent_uplink_gain <= 0:
            logger.warning(
                "AGENT_UPLINK_GAIN=%r 非法，当前通话回落为 1.0",
                agent_uplink_gain,
            )
            agent_uplink_gain = 1.0
        uplink_pre_stats = PcmFlowStats("agent_uplink_pre_gain")
        uplink_post_stats = PcmFlowStats("agent_uplink_post_gain")
        winddown_deadline: float | None = None
        generation = self._session_generation
        # agent.fatal：实现层判定会话不可恢复（如重连全败）时置位，
        # 结束整通电话而非让对方听沉默。
        while self.is_active and not agent.fatal:
            triage_action = await self._consume_triage_results(
                agent, bridge, generation
            )
            if triage_action == "reject":
                winddown_deadline = self._triage_reject_deadline
            await self._speak_takeover_hold_if_needed(agent, bridge, generation)
            claimed = self.take_takeover_session()
            if claimed is not None:
                handed_off = await self._handoff_to_mobile(
                    agent, bridge, claimed, record, generation
                )
                if handed_off is not None:
                    return handed_off
            agent_effects_allowed = self._agent_effect_allowed(generation)
            if agent_effects_allowed:
                self._drain_agent_audio(bridge)
            else:
                self._clear_outgoing_audio()

            now = time.monotonic()
            # 来电的 NO CARRIER 与 CLCC 轮询可能同时停活；会话自己持有最终时限，
            # 确保 Agent/bridge/CallRecord 最终仍走统一 finally 收尾。
            if (
                inbound_max_seconds > 0
                and (now - loop_started) >= inbound_max_seconds
            ):
                logger.warning(
                    "来电超过 %.0fs 仍未收到挂断信号，触发会话级兜底收尾",
                    inbound_max_seconds,
                )
                if record is not None:
                    record.log_event(
                        "inbound_hard_deadline",
                        max_seconds=inbound_max_seconds,
                    )
                break
            # ① 外呼硬时限兜底
            if (
                outbound_max_seconds > 0
                and winddown_deadline is None
                and (now - loop_started) > outbound_max_seconds
            ):
                logger.warning(
                    "外呼超过 %.0fs 仍在进行，自动道别收尾",
                    outbound_max_seconds,
                )
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
                if not suppress_uplink and agent_effects_allowed:
                    pcm_agent = bridge.modem_to_agent(pcm_8k, agent.input_rate)
                    uplink_pre_stats.add(pcm_agent)
                    pcm_agent = apply_pcm_gain(pcm_agent, agent_uplink_gain)
                    uplink_post_stats.add(pcm_agent)
                    await agent.send_audio(pcm_agent)
            uplink_pre_stats.maybe_log(gain=agent_uplink_gain)
            uplink_post_stats.maybe_log(gain=agent_uplink_gain)
            await asyncio.sleep(0.01)
        return bridge

    async def _handoff_to_mobile(
        self,
        agent: VoiceAgent,
        old_bridge: AudioBridge,
        claimed: InboundTakeoverSession,
        record: CallRecord | None,
        generation: int,
    ) -> AudioBridge | None:
        """Prepare a second media owner, then atomically fence the AI owner."""

        factory = self._takeover_endpoint_factory
        coordinator = self._takeover_coordinator
        if factory is None or coordinator is None:
            if coordinator is not None:
                coordinator.rollback_precommit("endpoint_unavailable")
            return None
        endpoint: RemoteMediaEndpoint | None = None
        new_bridge: AudioBridge | None = None
        committed = False
        old_stopped = False
        try:
            endpoint = factory(claimed.issued)
            await endpoint.connect()
            deadline = time.monotonic() + _INBOUND_TAKEOVER_MEDIA_TIMEOUT_SECONDS
            while not endpoint.media_ready and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if not endpoint.media_ready:
                raise RuntimeError("takeover_media_not_ready")

            # No two writers: stop the AI bridge before creating the mobile bridge.
            old_bridge.stop()
            old_stopped = True
            new_bridge = create_audio_bridge(
                mode=self.audio_mode,
                device_keyword=self.audio_keyword,
                pcm_port=self.pcm_port,
                pcm_baudrate=self.pcm_baudrate,
                tx_gain=self.tx_gain,
            )
            if isinstance(new_bridge, SerialPcmAudioBridge):
                new_bridge.set_ready_check(self.modem.pcm_ready)
            new_bridge.start()

            ready = coordinator.mark_mobile_media_ready(claimed.fence)
            committed_result = (
                coordinator.commit_mobile(claimed.fence) if ready.accepted else ready
            )
            if not committed_result.accepted:
                raise RuntimeError(
                    f"takeover_commit_rejected:{committed_result.code or 'unknown'}"
                )
            committed = True
            await self.detach_agent(agent, None)
            if record is not None:
                record.log_event("takeover_committed", generation=generation)
            await self._send_takeover_connected(endpoint)
            status_task = asyncio.create_task(
                self._takeover_connected_snapshot_loop(endpoint)
            )
            try:
                return await self._pump_mobile_media(
                    endpoint, new_bridge, record, claimed
                )
            finally:
                status_task.cancel()
                await asyncio.gather(status_task, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            if committed:
                logger.warning("接管后媒体泵失败: error_type=%s", type(exc).__name__)
                return new_bridge
            logger.warning("接管切换失败，回滚 AI: error_type=%s", type(exc).__name__)
            if new_bridge is not None:
                try:
                    new_bridge.stop()
                except Exception:  # noqa: BLE001
                    pass
            if old_stopped:
                try:
                    old_bridge.start()
                except Exception as restart_exc:  # noqa: BLE001
                    logger.warning(
                        "回滚重启旧音频桥失败: error_type=%s",
                        type(restart_exc).__name__,
                    )
            coordinator.rollback_precommit(type(exc).__name__)
            if record is not None:
                record.log_event("takeover_rollback", reason=type(exc).__name__)
            return None
        finally:
            if endpoint is not None:
                await endpoint.close()

    @staticmethod
    async def _send_takeover_connected(endpoint: RemoteMediaEndpoint) -> None:
        try:
            await endpoint.send_event({"type": "status", "status": "connected"})
        except Exception as exc:  # noqa: BLE001
            logger.debug("接管 connected 状态发送失败: %s", type(exc).__name__)

    async def _takeover_connected_snapshot_loop(
        self, endpoint: RemoteMediaEndpoint, interval: float = 1.0
    ) -> None:
        try:
            while self._active and self.modem.is_call_connected():
                await asyncio.sleep(interval)
                await self._send_takeover_connected(endpoint)
        except asyncio.CancelledError:
            pass

    async def _pump_mobile_media(
        self,
        endpoint: RemoteMediaEndpoint,
        bridge: AudioBridge,
        record: CallRecord | None,
        claimed: InboundTakeoverSession,
    ) -> AudioBridge:
        fence = claimed.fence
        disconnected_at: float | None = None
        while self._active and self.modem.is_call_connected():
            # Keep control responsive without stretching the 10 ms media cadence.
            command = await endpoint.next_command(timeout=0.001)
            if command is not None and command.get("type") == "hangup":
                self._takeover_coordinator.end_call("owner_hangup")  # type: ignore[union-attr]
                if record is not None:
                    record.log_event("takeover_owner_hangup")
                break
            if endpoint.media_ready:
                if self.takeover_state is TakeoverState.MOBILE_RECONNECTING:
                    self._takeover_coordinator.mark_mobile_reconnected(fence)  # type: ignore[union-attr]
                disconnected_at = None
                browser_chunks = endpoint.take_browser_audio()
                if browser_chunks:
                    if record is not None:
                        for chunk in browser_chunks:
                            record.write_downlink(chunk)
                    bridge.write_modem_chunks(browser_chunks)
                modem_pcm = bridge.read_modem_chunk()
                if modem_pcm:
                    if record is not None:
                        record.write_uplink(modem_pcm)
                    endpoint.push_modem_audio(modem_pcm)
            else:
                if self.takeover_state is TakeoverState.MOBILE_ACTIVE:
                    self._takeover_coordinator.mark_mobile_disconnected(fence)  # type: ignore[union-attr]
                    disconnected_at = time.monotonic()
                elif (
                    self.takeover_state is TakeoverState.MOBILE_RECONNECTING
                    and disconnected_at is not None
                    and time.monotonic() - disconnected_at
                    >= max(0.0, config.get_float("REMOTE_DISCONNECT_GRACE_SECONDS"))
                ):
                    result = self._takeover_coordinator.expire_mobile_reconnect(fence)  # type: ignore[union-attr]
                    if result.action is TakeoverAction.NOTICE_THEN_HANGUP and record is not None:
                        record.log_event("takeover_notice_then_hangup")
                    break
            await asyncio.sleep(0.01)
        return bridge

    def _load_session_config(self) -> None:
        """每通会话开始时重读可调参数，支持不重启改参。"""
        self._hangover_seconds = config.get_float("HALF_DUPLEX_HANGOVER_SECONDS")
        self._hangup_delay_seconds = config.get_float("HANGUP_TOOL_DELAY_SECONDS")

    def _begin_record(self, direction: str, number: str | None) -> CallRecord | None:
        """创建通话记录；录音选择在每通开始时锁定，本通中途不再变化。"""
        if self.call_logger is None:
            return None
        try:
            return self.call_logger.begin_call(
                direction,
                number,
                recording_enabled=config.get_bool("RECORDING_ENABLED"),
            )
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
        sim_identity = getattr(self.modem, "sim_identity", None)
        service_number = str(getattr(sim_identity, "service_number", "") or "")
        self._maybe_summarize(
            record,
            transcripts,
            direction,
            number,
            self._result_verification_mode,
            service_number,
        )

    def _maybe_summarize(
        self,
        record: CallRecord,
        transcripts: list[tuple[str, str]],
        direction: str,
        number: str | None,
        result_verification: str = "none",
        service_number: str = "",
    ) -> None:
        """通话摘要开关打开且对方说过话时，起后台线程生成摘要。"""
        try:
            if not config.get_bool("SUMMARY_ENABLED"):
                return
            if (
                result_verification != "carrier_sms"
                and not any(role == "user" and text.strip() for role, text in transcripts)
            ):
                return
            thread = threading.Thread(
                target=self._summarize_worker,
                args=(
                    record,
                    list(transcripts),
                    direction,
                    number,
                    result_verification,
                    service_number,
                ),
                daemon=True,
                name="call-summary",
            )
            self._summary_thread = thread
            record.mark_summary_pending()
            thread.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动通话摘要线程失败: %s", exc)
            try:
                record.set_summary(
                    {"ok": False, "summary": "", "error": "summary_worker_start_failed"}
                )
            except Exception:  # noqa: BLE001
                logger.exception("记录摘要启动失败状态时发生异常")

    def _summarize_worker(
        self,
        record: CallRecord,
        transcripts: list[tuple[str, str]],
        direction: str,
        number: str | None,
        result_verification: str = "none",
        service_number: str = "",
    ) -> None:
        """后台线程：调大模型生成结构化摘要并写盘/推送。"""
        try:
            result = summarize_call(transcripts, direction, number)
            if result_verification == "carrier_sms":
                wait_seconds = max(
                    0.0, config.get_float("SMS_VERIFICATION_WAIT_SECONDS")
                )
                should_collect_evidence = (
                    direction == "outbound"
                    and is_carrier_service_call(number, service_number)
                )
                if self.hub is not None and should_collect_evidence:
                    self.hub.wait_for_event(
                        lambda event: bool(
                            carrier_sms_evidence(
                                [event],
                                service_number=service_number,
                                started_at=record.started_at,
                            )
                        ),
                        timeout=wait_seconds,
                    )
                    evidence = carrier_sms_evidence(
                        self.hub.history(),
                        service_number=service_number,
                        started_at=record.started_at,
                        ended_at=time.time(),
                    )
                else:
                    evidence = []
                result = apply_carrier_sms_verification(
                    result,
                    evidence,
                    lang=agent_language(),
                )
            record.set_summary(result)
            if result.get("ok"):
                self._publish({"type": "call_summary", "call_id": record.id, **result})
            else:
                logger.warning("通话摘要生成失败: %s", result.get("error"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("通话摘要线程异常: %s", exc)
            try:
                record.set_summary(
                    {"ok": False, "summary": "", "error": "summary_worker_error"}
                )
            except Exception:  # noqa: BLE001
                logger.exception("记录摘要线程失败状态时发生异常")

    def _build_tools(self, direction: str | None = None) -> ToolRegistry:
        """构造本通会话的工具集（工具语义在 call_tools 模块）。"""
        direction = direction or (
            "outbound" if self._outbound_number is not None else "inbound"
        )
        generation = self._session_generation
        tools = CallTools(
            self.modem,
            hub=self.hub,
            get_caller=lambda: self.current_caller,
            get_record=lambda: self._record,
            schedule_hangup=self._schedule_deferred_hangup,
            is_sms_target_allowed=self._sms_target_allowed,
            send_dtmf=self._send_dtmf_from_tool,
            effect_guard=lambda: self._agent_effect_allowed(generation),
        )
        registry = tools.register()
        if (
            direction == "inbound"
            and config.get_bool("INBOUND_TAKEOVER_ENABLED")
            and self._triage_mode == "off"
        ):
            registry.register(
                REQUEST_OWNER_TAKEOVER_SPEC,
                lambda _args: self._request_owner_takeover(generation),
            )
        return registry

    def _start_dtmf_judge(
        self, record: CallRecord | None, *, session_t0: float
    ) -> None:
        """Start the per-call shadow worker only when explicitly enabled."""
        self._stop_dtmf_judge(join_timeout=0.0)
        if record is None or config.get_str("DTMF_JUDGE_MODE").strip() != "shadow":
            return
        model = (
            config.get_str("DTMF_JUDGE_MODEL").strip()
            or config.get_str("PROMPT_GEN_MODEL").strip()
            or "qwen-plus"
        )
        lang = agent_language()
        task_goal = (
            self._outbound_task(lang)
            if self._outbound_number
            else ("处理本次来电" if lang == "zh" else "handle this inbound call")
        )
        window_mode: WindowMode = (
            "merged" if config.get_bool("MANUAL_RESPONSE_CONTROL") else "fragmented"
        )
        ledger = DtmfActionLedger()
        judge: DtmfJudge | None = None
        try:
            judge = DtmfJudge(
                record=record,
                task_goal=task_goal,
                ledger=ledger,
                model=model,
                window_mode=window_mode,
            )
            judge.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "DTMF 判官启动失败: error_type=%s", type(exc).__name__
            )
            if judge is not None:
                try:
                    judge.stop(join_timeout=0.0)
                except Exception:  # noqa: BLE001
                    pass
            record.log_event(
                "judge_error",
                code="startup_error",
                latency_ms=0.0,
                window_mode=window_mode,
            )
            return
        with self._dtmf_lock:
            self._dtmf_judge_started_at = session_t0
            self._dtmf_ledger = ledger
            self._dtmf_judge = judge

    def _stop_dtmf_judge(self, *, join_timeout: float = 0.2) -> None:
        with self._dtmf_lock:
            judge = self._dtmf_judge
            self._dtmf_judge = None
            self._dtmf_ledger = None
            self._dtmf_judge_started_at = 0.0
        if judge is not None:
            try:
                judge.stop(join_timeout=join_timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DTMF 判官停止失败: error_type=%s", type(exc).__name__
                )

    def _initialize_triage_context(
        self, direction: str, record: CallRecord | None
    ) -> None:
        self._stop_triage_judge(join_timeout=0.0)
        mode = config.get_str("INBOUND_TRIAGE_MODE").strip().lower()
        self._triage_mode = mode if mode in {"off", "shadow", "enforce"} else "off"
        self._triage_results = Queue(maxsize=4)
        self._triage_consumer = TriageVerdictConsumer(
            transfer_threshold=0.7,
            reject_threshold=0.85,
        )
        self._triage_pending = direction == "inbound" and self._triage_mode == "enforce"
        self._triage_terminal = False
        self._triage_reject_deadline = None
        self._triage_clarification_spoken = False
        if direction != "inbound" or self._triage_mode == "off":
            return

        def on_verdict(verdict: TriageVerdict, latency_ms: float) -> None:
            if record is not None:
                record.log_event(
                    "inbound_triage_judge",
                    mode=self._triage_mode,
                    latency_ms=latency_ms,
                    **verdict.public_fields(),
                )
            self._publish(
                {
                    "type": "inbound_triage",
                    "status": "judged",
                    "mode": self._triage_mode,
                    **verdict.public_fields(),
                }
            )
            if self._triage_mode != "enforce":
                return
            try:
                self._triage_results.put_nowait(verdict)
            except Full:
                try:
                    self._triage_results.get_nowait()
                except Empty:
                    pass
                try:
                    self._triage_results.put_nowait(verdict)
                except Full:
                    pass

        def on_error(
            code: str,
            turn_id: int,
            call_generation: int,
            latency_ms: float,
        ) -> None:
            if record is not None:
                record.log_event(
                    "inbound_triage_error",
                    code=code,
                    turn_id=turn_id,
                    call_generation=call_generation,
                    latency_ms=latency_ms,
                )

        judge = InboundTriageJudge(
            call_generation=self._session_generation,
            preference=config.get_str("INBOUND_TAKEOVER_PREFERENCE"),
            on_verdict=on_verdict,
            on_error=on_error,
        )
        self._triage_judge = judge
        judge.start()

    def _stop_triage_judge(self, *, join_timeout: float = 0.2) -> None:
        judge = self._triage_judge
        self._triage_judge = None
        if judge is not None:
            try:
                judge.stop(join_timeout=join_timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "来电分诊判官停止失败: error_type=%s", type(exc).__name__
                )

    async def _consume_triage_results(
        self, agent: VoiceAgent, bridge: AudioBridge, generation: int
    ) -> str | None:
        terminal_action: str | None = None
        while True:
            try:
                verdict = self._triage_results.get_nowait()
            except Empty:
                break
            result = self._triage_consumer.consume(
                verdict, current_generation=generation
            )
            self._log_triage_consumption(result)
            if result.outcome in {"ignored", "observe"}:
                continue
            if result.outcome == "continue_ai":
                self._triage_pending = False
                continue
            if result.outcome == "clarify":
                if self._triage_clarification_spoken:
                    continue
                self._triage_clarification_spoken = True
                try:
                    await agent.say(_INBOUND_TRIAGE_CLARIFY_TEXT)
                    self._drain_agent_audio(bridge)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "分诊澄清语播放失败: error_type=%s", type(exc).__name__
                    )
                continue
            if result.outcome == "transfer":
                # Fence normal Realtime output before orchestration. The takeover
                # hold line has its own one-shot generation gate.
                self._triage_terminal = True
                request = self.begin_owner_takeover(trigger="triage_judge")
                if request.get("success") is not True:
                    self._triage_terminal = False
                    self._triage_consumer.rollback_terminal()
                    logger.warning(
                        "分诊转接编排失败: code=%s", request.get("code", "unknown")
                    )
                    continue
                terminal_action = "transfer"
                continue
            if result.outcome == "reject":
                # The fixed line is generated by orchestration, not by Realtime's
                # free-form policy. Fence immediately after its audio is flushed.
                self._clear_outgoing_audio()
                try:
                    await agent.say(_INBOUND_TRIAGE_REJECT_TEXT)
                    self._drain_agent_audio(bridge)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "分诊拒绝语播放失败: error_type=%s", type(exc).__name__
                    )
                self._triage_terminal = True
                self._triage_pending = False
                self._triage_reject_deadline = time.monotonic() + min(
                    3.0, max(0.5, self._hangup_delay_seconds)
                )
                terminal_action = "reject"
        return terminal_action

    def _log_triage_consumption(self, result: TriageConsumption) -> None:
        record = self._record
        if record is not None:
            record.log_event(
                "inbound_triage_consumed",
                outcome=result.outcome,
                reason=result.reason,
                **result.verdict.public_fields(),
            )

    def _sms_target_allowed(self, number: str) -> bool:
        """发短信目标限制:只允许回复已联系过的号码或当前通话对端。

        取当下的 current_caller(通话中对端可能还没进落盘记录),
        与落盘的短信/来电记录一起判定。
        """
        return is_reply_target_allowed(
            number,
            self.hub,
            self.call_logger,
            extra_allowed=self.current_caller,
            allow_any=config.get_bool("SMS_ALLOW_ANY_TARGET"),
        )

    def _build_agent_instructions(self, direction: str) -> str:
        """会话系统提示词：文本构造在 prompts 模块（纯函数，可独测）。"""
        lang = agent_language()
        scenario = self._take_prompt_scenario() if direction == "outbound" else None
        triage_mode = self._triage_mode if direction == "inbound" else "off"
        takeover_preference = (
            config.get_str("INBOUND_TAKEOVER_PREFERENCE")
            if direction == "inbound"
            and config.get_bool("INBOUND_TAKEOVER_ENABLED")
            and triage_mode == "off"
            else None
        )
        return build_instructions(
            direction,
            owner_name(lang),
            agent_persona(lang),
            self._outbound_task(lang),
            lang,
            scenario=scenario,
            takeover_preference=takeover_preference,
            triage_pending=triage_mode == "enforce",
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
        # 每次裁判决策落 events：真机验证「否定式答复→收尾」与事后排障都靠它（#16）。
        record = self._record
        if record is not None:
            record.log_event(
                "wrap_up_judge",
                decision=str(result.get("decision", "")),
                reason=str(result.get("reason", ""))[:200],
                ok=bool(result.get("ok")),
            )
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
        # 命中键：选了预设时用预设原任务定位（事项框可被改成具体子主题、不影响命中）；
        # 未选预设（手输）时用事项框内容。子主题始终经 _outbound_task 进 instructions 的 topic。
        match_key = self._preset_hint if self._preset_hint is not None else task
        if config.get_bool("NUMBER_PROFILES_ENABLED"):
            profile = None
            if self._preset_id:
                profile = lookup_profile_by_id(
                    self._preset_id,
                    number,
                    task,
                    lang=lang,
                )
            if profile is None:
                profile = lookup_profile(number, match_key, lang=lang)
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
        mode = str(result.get("opening_mode") or "").strip().lower()
        self._prompt_gen_opening_mode = "wait" if mode == "wait" else "say"
        self._prompt_gen_dtmf_spoken_followup = (
            result.get("dtmf_spoken_followup") is True
        )
        self._result_verification_mode = (
            "carrier_sms"
            if result.get("result_verification") == "carrier_sms"
            else "none"
        )
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
        profile_id = str(result.get("profile_id") or "")
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
                opening_mode=str(result.get("opening_mode") or "").strip().lower() or "say",
                dtmf_spoken_followup=result.get("dtmf_spoken_followup") is True,
                error=error,
                provider=provider,
                model=model,
                cached=cached,
                source=source,
                profile_id=profile_id,
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

    def _schedule_spoken_dtmf_followup(
        self, agent: VoiceAgent, transcript: str
    ) -> None:
        if not self._prompt_gen_dtmf_spoken_followup:
            return
        digits = extract_spoken_dtmf(transcript)
        if digits is None:
            return
        with self._dtmf_lock:
            if not self._active or not self._prompt_gen_dtmf_spoken_followup:
                return
            self._next_dtmf_followup_id += 1
            followup_id = self._next_dtmf_followup_id
            generation = self._session_generation
            created_at = time.monotonic()
            timer = threading.Timer(
                DTMF_SPOKEN_FOLLOWUP_DELAY_SECONDS,
                self._fire_spoken_dtmf_followup,
                args=(followup_id,),
            )
            timer.daemon = True
            self._pending_dtmf_followups[followup_id] = (
                timer,
                digits,
                generation,
                created_at,
                agent,
                self._record,
            )
            timer.start()

    def _fire_spoken_dtmf_followup(self, followup_id: int) -> None:
        with self._dtmf_lock:
            pending = self._pending_dtmf_followups.pop(followup_id, None)
            if pending is None:
                return
            _timer, digits, generation, created_at, agent, record = pending
            if (
                not self._active
                or generation != self._session_generation
                or not self._prompt_gen_dtmf_spoken_followup
            ):
                return
            now = time.monotonic()
            self._prune_recent_dtmf_locked(now)
            previous = self._recent_dtmf_sent.get(digits)
            if (
                previous is not None
                and now - previous[0] < DTMF_RECENT_SEND_WINDOW_SECONDS
            ):
                return
            tools = self._active_tools
            if tools is None:
                return
            self._dtmf_dispatch_context.source = "spoken_followup"
            try:
                # The timer thread is the worker: qvts/AT cannot block an Agent
                # callback or the CallSession audio loop.
                result = tools.dispatch("send_dtmf", {"digits": digits})
            finally:
                try:
                    del self._dtmf_dispatch_context.source
                except AttributeError:
                    pass
            executed_at = time.monotonic()

        context_injected = self._notify_external_tool_result(agent, result)
        if record is not None:
            mode = result.get("mode")
            record.log_event(
                "dtmf_auto_followup",
                count=len(digits),
                mode=mode if isinstance(mode, str) else "unknown",
                result="success" if result.get("success") is True else "failure",
                delay_ms=round((executed_at - created_at) * 1000, 1),
                source="agent_transcript",
                context_injected=context_injected,
            )

    def _notify_external_tool_result(
        self, agent: VoiceAgent, result: dict
    ) -> bool:
        loop = self._loop
        if loop is None or not loop.is_running():
            return False
        safe_result = {
            "success": result.get("success") is True,
            "count": result.get("count") if isinstance(result.get("count"), int) else 0,
            "mode": result.get("mode") if isinstance(result.get("mode"), str) else "unknown",
        }
        try:
            future = asyncio.run_coroutine_threadsafe(
                agent.external_tool_result(
                    "send_dtmf", safe_result, source="spoken_followup"
                ),
                loop,
            )
            return bool(
                future.result(timeout=_EXTERNAL_TOOL_RESULT_TIMEOUT_SECONDS)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "外部 DTMF 结果未写入模型上下文: error_type=%s",
                type(exc).__name__,
            )
            return False

    def _cancel_spoken_dtmf_followups(self, *, clear_recent: bool = False) -> None:
        with self._dtmf_lock:
            self._cancel_spoken_dtmf_followups_locked()
            if clear_recent:
                self._recent_dtmf_sent.clear()

    def _cancel_spoken_dtmf_followups_locked(self) -> None:
        pending = list(self._pending_dtmf_followups.values())
        self._pending_dtmf_followups.clear()
        for timer, _digits, _generation, _created_at, _agent, _record in pending:
            timer.cancel()

    def _cancel_pending_dtmf_locked(self, digits: str) -> None:
        matching = [
            followup_id
            for followup_id, pending in self._pending_dtmf_followups.items()
            if pending[1] == digits
        ]
        for followup_id in matching:
            timer, _digits, _generation, _created_at, _agent, _record = (
                self._pending_dtmf_followups.pop(followup_id)
            )
            timer.cancel()

    def _prune_recent_dtmf_locked(self, now: float) -> None:
        expired = [
            digits
            for digits, (sent_at, _source) in self._recent_dtmf_sent.items()
            if now - sent_at >= DTMF_RECENT_SEND_WINDOW_SECONDS
        ]
        for digits in expired:
            del self._recent_dtmf_sent[digits]

    def send_dtmf(self, digits: str) -> tuple[bool, str | None]:
        """发送 DTMF；UAC 模式默认把双音作为带内 PCM 注入下行队列。"""
        mode = "unknown"
        try:
            ok, mode = self._send_dtmf_raw(
                digits, source="manual", deduplicate=False
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送 DTMF 失败: error_type=%s", type(exc).__name__)
            if self._record is not None:
                self._record.log_event(
                    "dtmf", count=len(digits), mode=mode, result="failure"
                )
            return False, "按键发送失败"
        if self._record is not None:
            self._record.log_event(
                "dtmf",
                count=len(digits),
                mode=mode,
                result="success" if ok else "failure",
            )
        return (True, None) if ok else (False, "按键发送失败")

    def _send_dtmf_from_tool(self, digits: str) -> tuple[bool, str]:
        source = getattr(self._dtmf_dispatch_context, "source", "agent_tool")
        return self._send_dtmf_raw(digits, source=source, deduplicate=True)

    def _send_dtmf_raw(
        self,
        digits: str,
        *,
        source: str = "agent_tool",
        deduplicate: bool = True,
    ) -> tuple[bool, str]:
        mode = self._resolve_dtmf_mode()
        with self._dtmf_lock:
            now = time.monotonic()
            self._prune_recent_dtmf_locked(now)
            previous = self._recent_dtmf_sent.get(digits)
            if (
                deduplicate
                and previous is not None
                and now - previous[0] < DTMF_RECENT_SEND_WINDOW_SECONDS
                and (
                    source == "spoken_followup"
                    or previous[1] == "spoken_followup"
                )
            ):
                logger.info(
                    "抑制重复 DTMF: count=%d source=%s", len(digits), source
                )
                if source != "spoken_followup":
                    self._cancel_pending_dtmf_locked(digits)
                return True, mode

            ok = True
            sent = False
            if mode in {"inband", "both"}:
                tone = dtmf_tone(
                    digits,
                    MODEM_RATE,
                    tone_ms=config.get_int("DTMF_TONE_MS"),
                    amplitude=config.get_float("DTMF_TONE_AMPLITUDE"),
                )
                if not tone:
                    return False, mode
                # 与 Agent 语音共用 _outgoing_audio，后续由 _drain_agent_audio
                # 按既有下行链路送入桥；半双工 pending 判定也会自然把它当成正在说话。
                if self._record is not None:
                    self._record.write_downlink(tone)
                self._outgoing_audio.put(tone)
                sent = True
            if mode in {"qvts", "both"}:
                ok = self.modem.send_dtmf(digits)
                sent = sent or ok
            if sent:
                self._recent_dtmf_sent[digits] = (time.monotonic(), source)
                self._record_dtmf_action(digits, source)
                if source != "spoken_followup":
                    self._cancel_pending_dtmf_locked(digits)
            return ok, mode

    def _record_dtmf_action(self, digits: str, source: str) -> None:
        public_source = {
            "agent_tool": "realtime",
            "spoken_followup": "guard",
            "judge": "judge",
        }.get(source)
        if public_source is None:
            return
        ledger = self._dtmf_ledger
        judge = self._dtmf_judge
        if ledger is None or judge is None:
            return
        relative_time = (
            max(0.0, time.monotonic() - self._dtmf_judge_started_at)
            if self._dtmf_judge_started_at > 0
            else 0.0
        )
        try:
            entry = ledger.record(
                digits,
                public_source,  # type: ignore[arg-type]
                timestamp=relative_time,
            )
        except ValueError as exc:
            logger.warning(
                "DTMF action ledger 拒绝记录: error_type=%s", type(exc).__name__
            )
            return
        record = self._record
        if record is not None:
            record.log_event("dtmf_action", **entry.public_fields())
        judge.record_action(entry)

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
        while self.is_active and time.monotonic() < deadline:
            if self.modem.is_call_connected():
                return True
            await asyncio.sleep(0.2)
        return False

    async def _shutdown(self) -> None:
        self._set_active(False)

    def _agent_generation_current(self, generation: int) -> bool:
        with self._hangup_lock:
            return self.is_active and generation == self._session_generation

    def _agent_effect_allowed(self, generation: int) -> bool:
        if not self._agent_generation_current(generation):
            return False
        with self._takeover_lock:
            coordinator = self._takeover_coordinator
            hold_allowed = (
                self._takeover_hold_generation == generation
                and not self._takeover_hold_done
            )
            if hold_allowed:
                return True
            if self._triage_terminal:
                return False
            return coordinator is None or coordinator.state is TakeoverState.AI_ACTIVE

    @property
    def takeover_state(self) -> TakeoverState | None:
        with self._takeover_lock:
            coordinator = self._takeover_coordinator
            return coordinator.state if coordinator is not None else None

    @property
    def takeover_fence(self):
        with self._takeover_lock:
            coordinator = self._takeover_coordinator
            return coordinator.active_fence if coordinator is not None else None

    def _initialize_takeover_context(self, direction: str) -> None:
        with self._takeover_lock:
            self._takeover_offer_queue = Queue(maxsize=1)
            self._takeover_revoke_queue = Queue(maxsize=1)
            self._takeover_session_queue = Queue(maxsize=1)
            self._takeover_request = None
            self._takeover_hold_generation = None
            self._takeover_hold_done = False
            if direction != "inbound":
                self._takeover_coordinator = None
                return
            router = _CallSessionMediaRouter()
            self._takeover_coordinator = InboundTakeoverCoordinator(
                call_id=f"call_{secrets.token_urlsafe(18)}",
                generation=self._session_generation,
                media_router=router,
            )

    def begin_owner_takeover(self, *, trigger: str = "orchestrator") -> dict:
        """Request takeover for the current inbound call from deterministic policy."""
        return self._request_owner_takeover(
            self._session_generation,
            trigger=trigger,
        )

    def force_takeover_request(self) -> dict:
        """Debug smoke hook: force takeover on the current active call."""
        generation = self._session_generation
        if not self._agent_generation_current(generation):
            return {
                "success": False,
                "code": "NO_ACTIVE_CALL",
                "message": "当前没有进行中的通话",
            }
        if not self.modem.is_call_connected():
            return {
                "success": False,
                "code": "CALL_NOT_CONNECTED",
                "message": "当前物理通话尚未接通",
            }
        with self._takeover_lock:
            if self._takeover_coordinator is None:
                self._takeover_coordinator = InboundTakeoverCoordinator(
                    call_id=f"call_{secrets.token_urlsafe(18)}",
                    generation=generation,
                    media_router=_CallSessionMediaRouter(),
                )
        return self._request_owner_takeover(
            generation,
            trigger="debug_force",
            allow_outbound=True,
            bypass_enabled=True,
        )

    def _request_owner_takeover(
        self,
        generation: int,
        *,
        trigger: str = "agent_tool",
        allow_outbound: bool = False,
        bypass_enabled: bool = False,
    ) -> dict:
        if not bypass_enabled and not config.get_bool("INBOUND_TAKEOVER_ENABLED"):
            return {
                "success": False,
                "code": "TAKEOVER_DISABLED",
                "message": "真人接管未启用",
            }
        if not self._agent_generation_current(generation):
            return {
                "success": False,
                "code": "STALE_AGENT_GENERATION",
                "message": "当前 Agent 会话已失效",
            }
        if self._outbound_number is not None and not allow_outbound:
            return {
                "success": False,
                "code": "TAKEOVER_INBOUND_ONLY",
                "message": "真人接管仅用于来电",
            }

        with self._takeover_lock:
            coordinator = self._takeover_coordinator
            if coordinator is None or coordinator.state is not TakeoverState.AI_ACTIVE:
                return {
                    "success": False,
                    "code": "TAKEOVER_NOT_AI_ACTIVE",
                    "message": "当前通话不允许重复请求真人接管",
                }
            result = coordinator.begin_takeover()
            if not result.accepted:
                return {
                    "success": False,
                    "code": str(result.code or TakeoverRejection.INVALID_STATE),
                    "message": "当前通话状态不允许真人接管",
                }
            created_at = time.time()
            request = InboundTakeoverOfferRequest(
                offer_id=f"offer_{secrets.token_urlsafe(18)}",
                nonce=secrets.token_urlsafe(24),
                call_id=coordinator.call_id,
                generation=coordinator.generation,
                created_at=created_at,
                expires_at=created_at + _INBOUND_TAKEOVER_OFFER_TTL_SECONDS,
            )
            self._takeover_request = request
            self._takeover_hold_generation = generation
            self._takeover_hold_done = False
            try:
                self._takeover_offer_queue.put_nowait(request)
            except Full:
                coordinator.rollback_precommit("offer_queue_full")
                self._takeover_request = None
                return {
                    "success": False,
                    "code": "TAKEOVER_QUEUE_FULL",
                    "message": "真人接管请求队列繁忙",
                }

        self._clear_outgoing_audio()
        record = self._record
        if record is not None:
            record.log_event(
                "takeover_requested",
                call_id=request.call_id,
                generation=request.generation,
                trigger=trigger,
                preference_configured=bool(
                    config.get_str("INBOUND_TAKEOVER_PREFERENCE").strip()
                ),
            )
        self._publish(
            {
                "type": "inbound_takeover",
                "status": "requested",
                "call_id": request.call_id,
                "generation": request.generation,
            }
        )
        return {
            "success": True,
            "code": "TAKEOVER_REQUESTED",
            "message": "真人接管请求已发出",
        }

    def next_takeover_offer(
        self, timeout: float = 0.0
    ) -> InboundTakeoverOfferRequest | None:
        with self._takeover_lock:
            queue = self._takeover_offer_queue
        try:
            return queue.get(timeout=timeout) if timeout > 0 else queue.get_nowait()
        except Empty:
            return None

    def next_takeover_revoke(
        self, timeout: float = 0.0
    ) -> InboundTakeoverRevoke | None:
        with self._takeover_lock:
            queue = self._takeover_revoke_queue
        try:
            return queue.get(timeout=timeout) if timeout > 0 else queue.get_nowait()
        except Empty:
            return None

    def provide_takeover_session(
        self, claimed: InboundTakeoverSession
    ) -> TakeoverResult:
        if not config.get_bool("INBOUND_TAKEOVER_ENABLED"):
            return TakeoverResult.reject(TakeoverRejection.TAKEOVER_DISABLED)
        with self._takeover_lock:
            coordinator = self._takeover_coordinator
            request = self._takeover_request
            if coordinator is None or request is None:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            offer = claimed.offer
            fence = claimed.fence
            if offer.call_id != request.call_id:
                return TakeoverResult.reject(TakeoverRejection.STALE_CALL)
            if offer.generation != request.generation:
                return TakeoverResult.reject(TakeoverRejection.STALE_GENERATION)
            if (
                offer.offer_id != request.offer_id
                or offer.nonce != request.nonce
                or offer.expires_at != request.expires_at
            ):
                return TakeoverResult.reject(
                    TakeoverRejection.OFFER_SCOPE_MISMATCH
                )
            if time.time() >= request.expires_at:
                coordinator.rollback_precommit("offer_expired")
                return TakeoverResult.reject(TakeoverRejection.OFFER_EXPIRED)
            if coordinator.state is TakeoverState.TAKEOVER_PREPARING:
                waiting = coordinator.wait_for_owner([offer])
                if not waiting.accepted:
                    return waiting
            elif coordinator.state is not TakeoverState.WAITING_OWNER:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            result = coordinator.claim_offer(
                offer_id=offer.offer_id,
                nonce=offer.nonce,
                claim_id=fence.claim_id,
                device_id=fence.device_id,
            )
            if not result.accepted or result.idempotent:
                return result
            try:
                self._takeover_session_queue.put_nowait(claimed)
            except Full:
                return TakeoverResult.reject(TakeoverRejection.CLAIM_CONFLICT)
            return result

    def accept_takeover_claim(
        self,
        *,
        offer_id: str,
        call_id: str,
        claim_id: str,
        generation: int,
        nonce: str,
        issued: IssuedLiveKitSession,
    ) -> TakeoverResult:
        """Bind an untrusted cloud claim to the Edge-local offer lifetime."""

        with self._takeover_lock:
            request = self._takeover_request
            if request is None:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            expires_at = request.expires_at
        device_id = issued.browser_identity
        claimed = InboundTakeoverSession(
            offer=TakeoverOffer(
                offer_id=offer_id,
                nonce=nonce,
                call_id=call_id,
                generation=generation,
                target_device_id=device_id,
                expires_at=expires_at,
            ),
            fence=ClaimFence(
                call_id=call_id,
                generation=generation,
                claim_id=claim_id,
                device_id=device_id,
            ),
            issued=issued,
        )
        return self.provide_takeover_session(claimed)

    def take_takeover_session(self) -> InboundTakeoverSession | None:
        with self._takeover_lock:
            queue = self._takeover_session_queue
        try:
            return queue.get_nowait()
        except Empty:
            return None

    def _end_takeover_context(self, reason: str) -> None:
        with self._takeover_lock:
            coordinator = self._takeover_coordinator
            request = self._takeover_request
            if coordinator is None:
                return
            coordinator.end_call(reason.lower())
            if request is not None:
                revoke = InboundTakeoverRevoke(
                    offer_id=request.offer_id,
                    call_id=request.call_id,
                    reason=reason,
                )
                try:
                    self._takeover_revoke_queue.put_nowait(revoke)
                except Full:
                    pass

    async def _stop_agent_resources(self, agent, bridge: AudioBridge | None) -> None:
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

    async def detach_agent(self, agent, bridge: AudioBridge | None) -> None:
        """Detach AI resources without ending the physical modem call."""

        # Invalidate first. Callbacks already in flight then fail their generation
        # check before they can write PCM, send DTMF/SMS, or schedule a hangup.
        with self._hangup_lock:
            self._session_generation += 1
            self._cancel_hangup_timer()
        with self._dtmf_lock:
            self._cancel_spoken_dtmf_followups_locked()
            self._active_tools = None
        self._stop_dtmf_judge()
        self._clear_outgoing_audio()
        await self._stop_agent_resources(agent, bridge)
        logger.info("Agent 已从物理通话分离（modem 与 QPCMV 保持）")

    async def _shutdown_agent(self, agent, bridge: AudioBridge | None) -> None:
        await self._stop_agent_resources(agent, bridge)
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
        sms_email_forwarder: SmsEmailForwarder | None = None,
    ) -> None:
        # modem/call_logger 参数供测试注入；默认按串口/环境配置自建。
        self.modem = modem or Eg25Modem(modem_port, baudrate)
        self.audio_keyword = audio_keyword
        self.provider = provider
        self.audio_mode = audio_mode
        self.pcm_port = pcm_port
        self.pcm_baudrate = pcm_baudrate
        self.tx_gain = tx_gain
        self.hub = hub
        self._ring_lock = threading.Lock()
        self._remote_setup_lock = threading.Lock()
        self._remote_worker: RemoteDialerWorker | None = None
        self._remote_invite: RemoteDialerInvite | None = None
        self._remote_session_device_id: str | None = None
        self._remote_call_owner: RemoteWebDialerCoordinator | None = None
        # 模组连接状态与后台 supervisor：首启时模组不在也不阻塞 Web，
        # supervisor 反复重连直到成功（首次连上后由 modem 读循环自愈接管）。
        # 注入 modem（测试/直连）视为已就绪；自建的由 supervisor 连上后置 True。
        self.modem_connected = modem is not None
        self._modem_state_lock = threading.Lock()
        self._modem_disconnected_monotonic: float | None = None
        self._modem_disconnected_at: str | None = None
        self._service_running = False
        self._supervisor_thread: threading.Thread | None = None
        self.sms_email_forwarder = (
            sms_email_forwarder if sms_email_forwarder is not None else SmsEmailForwarder()
        )
        self.call_logger = call_logger or CallLogger(
            base_dir=os.getenv("CALL_LOG_DIR", str(config.call_log_dir())),
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
            takeover_endpoint_factory=self._build_takeover_endpoint,
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
                if self.session.is_active or self._remote_call_owner is not None:
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
            with self._ring_lock:
                remote_owner = self._remote_call_owner
            if remote_owner is not None:
                remote_owner.request_call_stop("remote_party_hangup")
                return
            self.session.stop()
            self.modem.hangup()

        def on_sms(sender: str | None, text: str, sms_ts: str = "") -> None:
            logger.info("收到短信 来自=%s 字符数=%d", sender or "未知", len(text))
            # publish 返回是否为新短信：补收 SIM 已存短信 / +CMTI 重复上报时，
            # 去重后重复的一条既不入库也不重复转发邮件（is_new=False 直接跳过）。
            is_new = True
            if self.hub is not None:
                is_new = self.hub.publish(
                    {"type": "sms_in", "sender": sender, "text": text, "sms_ts": sms_ts}
                )
            if not is_new:
                return
            try:
                self.sms_email_forwarder.enqueue(sender, text)
            except Exception as exc:  # noqa: BLE001 - 转发失败不能中断模组监听。
                logger.warning("短信邮件转发入队失败: error_type=%s", type(exc).__name__)

        def on_connection_state(connected: bool) -> None:
            self._set_modem_connected(connected)

        def on_sim_identity(identity: SimIdentity) -> None:
            self._publish({"type": "sim_status", **identity.as_dict()})

        self.modem.on_ring(on_ring)
        self.modem.on_hangup(on_hangup)
        self.modem.on_sms(on_sms)
        connection_registrar = getattr(self.modem, "on_connection_state", None)
        if callable(connection_registrar):
            connection_registrar(on_connection_state)
        sim_registrar = getattr(self.modem, "on_sim_identity", None)
        if callable(sim_registrar):
            sim_registrar(on_sim_identity)

    def dial(
        self,
        number: str,
        task: str | None = None,
        preset_hint: str | None = None,
        preset_id: str | None = None,
    ) -> tuple[bool, str | None]:
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
        guard_failure = self._dial_guard(number)
        if guard_failure is not None:
            return False, guard_failure.message
        missing_credentials, message = self._reject_if_credentials_missing()
        if missing_credentials:
            return False, message
        self._remember_outbound_task(task)
        with self._ring_lock:
            if self.session.is_active or self._remote_call_owner is not None:
                return False, "当前正在通话中，请稍后再拨"
            self.session.current_caller = number
            if preset_id is None:
                # 保持旧客户端/测试替身的调用形状；未选预设时不新增关键字参数。
                self.session.start(
                    outbound_number=number,
                    task=task,
                    preset_hint=preset_hint,
                )
            else:
                self.session.start(
                    outbound_number=number,
                    task=task,
                    preset_hint=preset_hint,
                    preset_id=preset_id,
                )
        return True, None

    def hangup(self) -> tuple[bool, str | None]:
        """挂断进行中的通话（AI 与 IVR 互相不挂断时的人工兜底）。"""
        with self._ring_lock:
            remote_owner = self._remote_call_owner
        if remote_owner is not None:
            remote_owner.request_call_stop("local_dashboard_hangup")
            return True, None
        if not self.session.is_active:
            return False, "当前没有进行中的通话"
        self.session.stop()
        return True, None

    def force_takeover_request(self) -> dict:
        """Expose the active-session debug smoke hook to the local web layer."""
        return self.session.force_takeover_request()

    def send_dtmf(self, digits: str) -> tuple[bool, str | None]:
        """通话中人工发送 DTMF 按键（IVR 菜单导航）。"""
        with self._ring_lock:
            remote_owner = self._remote_call_owner
        if remote_owner is not None:
            accepted = remote_owner.submit_local_command(
                {"type": "dtmf", "digits": digits}
            )
            return (True, None) if accepted else (False, "按键发送失败")
        if not self.session.is_active:
            return False, "当前没有进行中的通话"
        return self.session.send_dtmf(digits)

    def create_remote_dialer_invite(self) -> tuple[dict | None, str | None]:
        """Prepare one short-lived browser call session; never exposes admin APIs."""

        if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
            return None, "远程网页拨号未启用"
        if not self.modem_connected:
            return None, "模组未连接（检查 USB 桥与 EC20）"

        with self._remote_setup_lock:
            worker = self._remote_worker
            invite = self._remote_invite
            if (
                worker is not None
                and worker.is_running
                and self._remote_session_device_id is not None
            ):
                return None, "远程拨号线路正在被已配对手机使用"
            if (
                worker is not None
                and worker.is_running
                and invite is not None
                and invite.expires_at > time.time()
            ):
                return self._invite_payload(invite), None
            if worker is not None and worker.is_running:
                worker.stop("invite_expired")

            try:
                invite, worker = self._build_remote_worker()
                self._remote_worker = worker
                self._remote_invite = invite
                self._remote_session_device_id = None
                worker.start(timeout=10.0)
            except (ImportError, RuntimeError, ValueError) as exc:
                self._remote_worker = None
                self._remote_invite = None
                self._remote_session_device_id = None
                logger.warning(
                    "创建远程拨号邀请失败: error_type=%s", type(exc).__name__
                )
                return None, "远程媒体连接失败，请检查 LiveKit 和拨号页配置"
            return self._invite_payload(invite), None

    def create_paired_remote_dialer_invite(
        self, device_id: str
    ) -> tuple[dict | None, str | None]:
        """Create a fresh session for one paired phone without sharing active media."""

        if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
            return None, "远程网页拨号未启用"
        if not self.modem_connected:
            return None, "模组未连接（检查 USB 桥与 EC20）"

        with self._remote_setup_lock:
            worker = self._remote_worker
            if worker is not None and worker.is_running:
                return None, "远程拨号线路正在使用，请结束当前会话后重试"
            try:
                invite, worker = self._build_remote_worker()
                self._remote_worker = worker
                self._remote_invite = invite
                self._remote_session_device_id = device_id
                worker.start(timeout=10.0)
            except (ImportError, RuntimeError, ValueError) as exc:
                self._remote_worker = None
                self._remote_invite = None
                self._remote_session_device_id = None
                logger.warning(
                    "创建已配对远程拨号会话失败: error_type=%s", type(exc).__name__
                )
                return None, "远程媒体连接失败，请检查 LiveKit 和拨号页配置"
            return self._invite_payload(invite), None

    def start_cloud_remote_session(
        self, command: dict
    ) -> tuple[bool, str | None]:
        """Start one server-issued LiveKit session without local signing secrets."""

        if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
            return False, "REMOTE_DISABLED"
        if not config.get_bool("REMOTE_CLOUD_ENABLED"):
            return False, "CLOUD_DISABLED"
        guard_failure = self._dial_guard(None)
        if guard_failure is not None:
            return False, guard_failure.code
        session = command["session"]
        expires_at = float(command["expiresAtUnix"])
        issued = IssuedLiveKitSession(
            invite=RemoteDialerInvite(
                session_id=session["sessionId"],
                url="",
                expires_at=expires_at,
            ),
            room_name=session["roomName"],
            browser_identity=session["browserIdentity"],
            edge_identity=session["edgeIdentity"],
            browser_token="",
            edge_token=session["token"],
            livekit_url=session["livekitUrl"],
        )
        with self._remote_setup_lock:
            worker = self._remote_worker
            if worker is not None and worker.is_running:
                return False, "LINE_BUSY"
            try:
                worker = self._build_remote_worker_for_issued(issued)
                self._remote_worker = worker
                self._remote_invite = issued.invite
                self._remote_session_device_id = f"cloud:{command['callId']}"
                worker.start(timeout=10.0)
            except (ImportError, RuntimeError, ValueError) as exc:
                self._remote_worker = None
                self._remote_invite = None
                self._remote_session_device_id = None
                logger.warning(
                    "启动云端远程拨号会话失败: error_type=%s", type(exc).__name__
                )
                return False, "MEDIA_FAILED"
        return True, None

    def remote_dialer_status(self) -> dict:
        """Return non-secret readiness/session state for the local dashboard."""

        enabled = config.get_bool("REMOTE_WEB_DIALER_ENABLED")
        cloud_enabled = config.get_bool("REMOTE_CLOUD_ENABLED")
        required = (
            ("REMOTE_CLOUD_URL",)
            if cloud_enabled
            else (
                "REMOTE_CONTROL_URL",
                "LIVEKIT_URL",
                "LIVEKIT_API_KEY",
                "LIVEKIT_API_SECRET",
            )
        )
        missing = [key for key in required if not config.get_str(key).strip()]
        worker = self._remote_worker
        payload: dict = {
            "enabled": enabled,
            "cloud_enabled": cloud_enabled,
            "configured": not missing,
            "missing": missing,
            "active": bool(worker and worker.is_running),
            "modem_online": self.modem_connected,
        }
        if worker is not None:
            payload.update(worker.coordinator.status())
        return payload

    def line_busy(self) -> bool:
        """Return a synchronized snapshot of every owner that can occupy the line."""
        with self._remote_setup_lock:
            worker = self._remote_worker
            remote_session_active = bool(worker and worker.is_running)
        with self._ring_lock:
            local_session_active = self.session.is_active
            remote_call_active = self._remote_call_owner is not None
        return remote_session_active or local_session_active or remote_call_active

    def next_inbound_takeover_offer(
        self, timeout: float = 0.0
    ) -> InboundTakeoverOfferRequest | None:
        return self.session.next_takeover_offer(timeout)

    def next_inbound_takeover_revoke(
        self, timeout: float = 0.0
    ) -> InboundTakeoverRevoke | None:
        return self.session.next_takeover_revoke(timeout)

    def provide_inbound_takeover_session(
        self, claimed: InboundTakeoverSession
    ) -> TakeoverResult:
        return self.session.provide_takeover_session(claimed)

    def accept_inbound_takeover_claim(
        self,
        *,
        offer_id: str,
        call_id: str,
        claim_id: str,
        generation: int,
        nonce: str,
        issued: IssuedLiveKitSession,
    ) -> TakeoverResult:
        return self.session.accept_takeover_claim(
            offer_id=offer_id,
            call_id=call_id,
            claim_id=claim_id,
            generation=generation,
            nonce=nonce,
            issued=issued,
        )

    def take_inbound_takeover_session(self) -> InboundTakeoverSession | None:
        return self.session.take_takeover_session()

    def cancel_remote_dialer(self) -> tuple[bool, str | None]:
        with self._remote_setup_lock:
            worker = self._remote_worker
            if worker is None or not worker.is_running:
                self._remote_worker = None
                self._remote_invite = None
                self._remote_session_device_id = None
                return False, "当前没有远程拨号会话"
            worker.stop("local_dashboard_cancel")
            self._remote_worker = None
            self._remote_invite = None
            self._remote_session_device_id = None
        return True, None

    def _build_remote_worker(self) -> tuple[RemoteDialerInvite, RemoteDialerWorker]:
        if config.get_str("REMOTE_MEDIA_PROVIDER").strip().lower() != "livekit":
            raise ValueError("REMOTE_MEDIA_PROVIDER 仅支持 livekit")
        issued = issue_livekit_session(
            livekit_url=config.get_str("LIVEKIT_URL"),
            api_key=config.get_str("LIVEKIT_API_KEY"),
            api_secret=config.get_str("LIVEKIT_API_SECRET"),
            public_url=config.get_str("REMOTE_CONTROL_URL"),
            ttl_seconds=config.get_int("REMOTE_INVITE_TTL_SECONDS"),
        )
        return issued.invite, self._build_remote_worker_for_issued(issued)

    def _build_remote_worker_for_issued(
        self, issued: IssuedLiveKitSession
    ) -> RemoteDialerWorker:
        from .livekit_media import LiveKitRemoteMediaEndpoint

        endpoint = LiveKitRemoteMediaEndpoint(issued)
        runtime = RemoteDialerRuntimeConfig(
            audio_mode=self.audio_mode,
            audio_keyword=self.audio_keyword,
            pcm_port=self.pcm_port,
            pcm_baudrate=self.pcm_baudrate,
            tx_gain=self.tx_gain,
            disconnect_grace_seconds=max(
                0.0, config.get_float("REMOTE_DISCONNECT_GRACE_SECONDS")
            ),
            outbound_max_seconds=max(
                0.0, float(config.get_int("REMOTE_OUTBOUND_MAX_SECONDS"))
            ),
            connect_timeout_seconds=max(
                1.0, config.get_float("REMOTE_CONNECT_TIMEOUT_SECONDS")
            ),
            dtmf_mode=config.get_str("REMOTE_DTMF_MODE"),
            dtmf_tone_ms=config.get_int("DTMF_TONE_MS"),
            dtmf_tone_amplitude=config.get_float("DTMF_TONE_AMPLITUDE"),
            recording_enabled=config.get_bool("REMOTE_HUMAN_RECORDING_ENABLED"),
        )
        coordinator = RemoteWebDialerCoordinator(
            session_id=issued.invite.session_id,
            expires_at=issued.invite.expires_at,
            modem=self.modem,
            endpoint=endpoint,
            runtime=runtime,
            call_logger=self.call_logger,
            reserve_line=self._reserve_remote_line,
            release_line=self._release_remote_line,
            publish_event=self._publish,
            dial_guard=self._dial_guard,
        )
        return RemoteDialerWorker(coordinator)

    @staticmethod
    def _build_takeover_endpoint(issued: IssuedLiveKitSession) -> RemoteMediaEndpoint:
        """Create the existing LiveKit endpoint without starting a dialer worker."""

        from .livekit_media import LiveKitRemoteMediaEndpoint

        return LiveKitRemoteMediaEndpoint(issued)

    def _reserve_remote_line(
        self, owner: RemoteWebDialerCoordinator
    ) -> str | DialGuardFailure | None:
        with self._ring_lock:
            worker = self._remote_worker
            if worker is None or worker.coordinator is not owner:
                return "远程拨号会话已过期"
            if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
                return "远程网页拨号已关闭"
            guard_failure = self._dial_guard(None)
            if guard_failure is not None:
                return guard_failure
            if self.session.is_active or self._remote_call_owner is not None:
                return "当前正在通话中，请稍后再拨"
            limit = acquire_remote_dial_slot(
                config.get_int("REMOTE_DIAL_LIMIT_PER_HOUR")
            )
            if not limit.allowed:
                retry_seconds = max(1, round(limit.retry_after))
                return f"远程外呼过于频繁，请在 {retry_seconds} 秒后重试"
            self._remote_call_owner = owner
        return None

    def _dial_guard(self, number: str | None) -> DialGuardFailure | None:
        return check_dial_guard(
            modem_online=self.modem_connected,
            sim_identity=getattr(self.modem, "sim_identity", None),
            number=number,
        )

    def _release_remote_line(self, owner: RemoteWebDialerCoordinator) -> None:
        with self._ring_lock:
            if self._remote_call_owner is owner:
                self._remote_call_owner = None

    @staticmethod
    def _invite_payload(invite: RemoteDialerInvite) -> dict:
        return {
            "session_id": invite.session_id,
            "url": invite.url,
            "expires_at": invite.expires_at,
        }

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
        with self._modem_state_lock:
            if connected == self.modem_connected:
                return
            self.modem_connected = connected
            event: dict = {"type": "modem_status", "connected": connected}
            if connected:
                if self._modem_disconnected_monotonic is not None:
                    event["recovery_seconds"] = round(
                        max(0.0, time.monotonic() - self._modem_disconnected_monotonic), 3
                    )
                if self._modem_disconnected_at is not None:
                    event["disconnected_at"] = self._modem_disconnected_at
                self._modem_disconnected_monotonic = None
                self._modem_disconnected_at = None
            else:
                self._modem_disconnected_monotonic = time.monotonic()
                self._modem_disconnected_at = datetime.now(UTC).isoformat()
                event["disconnected_at"] = self._modem_disconnected_at
            if error:
                event["error"] = error
        self._publish(event)
        if connected:
            if "recovery_seconds" in event:
                logger.info(
                    "模组连接已恢复: recovery_seconds=%.3f",
                    event["recovery_seconds"],
                )
            self._publish({"type": "system", "text": "服务已启动，等待来电"})
        else:
            logger.warning("模组连接已断开: disconnected_at=%s", event["disconnected_at"])

    def stop_service(self) -> None:
        """停止 supervisor 与当前会话，关闭模组（供退出时调用）。"""
        self._service_running = False
        worker = self._remote_worker
        if worker is not None:
            worker.stop("service_shutdown")
        self.session.stop()
        self.modem.close()
        try:
            self.sms_email_forwarder.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止短信邮件转发 worker 失败: error_type=%s", type(exc).__name__)

    def run(self) -> None:
        self.start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            logger.info("收到退出信号")
        finally:
            self.stop_service()
