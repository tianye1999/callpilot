"""来电会话编排：模组 ↔ 音频桥 ↔ AI Agent。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import secrets
import threading
import time
from array import array
from datetime import UTC, datetime
from queue import Empty, Full, Queue
from typing import Awaitable, Callable

from . import call_playbooks, config, platforms
from .agents.base import VoiceAgent
from .agents.factory import create_agent
from .agents.tools import REQUEST_OWNER_TAKEOVER_SPEC, ToolRegistry
from .audio_bridge import (
    MODEM_RATE,
    AgentPcm,
    FfmpegAudioBridge,
    ModemAudioBridge,
    PrebufferedPcm,
    SerialPcmAudioBridge,
    apply_pcm_gain,
    create_audio_bridge,
)
from .bluetooth_audio import BluetoothAudioBridge
from .bluetooth_modem import BluetoothModem
from .call_log import CallLogger, CallRecord
from .call_tools import CallTools
from .cloud_credentials import CloudCredentialStore
from .contacts import is_reply_target_allowed, known_contact_numbers
from .dial_guard import DialGuardFailure, check_dial_guard
from .dial_queue import DialQueue, whitelist_from_env
from .dtmf import dtmf_tone
from .dtmf_followup import extract_spoken_dtmf
from .dtmf_judge import DtmfActionLedger, DtmfJudge, WindowMode
from .events import EventHub
from .modem import SerialModem, Sim7600Modem, create_modem
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
    select_task_relevant_evidence,
)
from .sim7600_reset import Sim7600CallReset
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

# 上行「有声」判定峰值（int16）。真机实测（2026-08-12）上行静音是纯数字零
# （峰值中位 2、p90 13）——modem 不给底噪，所以阈值可以放得很低；而对端小声
# 说话峰值常在 500~900，设 1000 会把句尾标早、夸大「音尾→判停」的读数。
# 只用于轮次延迟测量的打点，不参与任何对话逻辑。
_VOICED_PEAK_THRESHOLD = 350

# 下行轮首静音垫的判定峰值与掐除上限（WIL-112）。TTS 音频前缘实测垫中位
# 470ms、最长 ~1s 纯静音；上限防止把真正的轻声开头吃掉。16B = 1ms @8kHz/16bit。
_DOWNLINK_SILENCE_PEAK = 500
_TURN_TRIM_CAP_BYTES = 24000  # 1.5s

# barge-in 自激兜底（WIL-94 坑 4）：回复出声后极短时间内就被「对端开口」打断、
# 且连续多次，即视为疑似自激（下行被模组回采成上行触发 provider VAD）。前提
# 「模组无回采」是一次性实测（SIM7600 r=0.009），换硬件/改增益后可能失效，
# 失效的代价是通话自循环——必须有运行时兜底：触发后本通退回半双工。
# 窗口取 2s：无积压时回声必然落在首块音频到达后的这段内；真人抢话通常更晚。
# 连续 3 次才触发——单次快速插话是正常抢话，不该因此没收打断能力。
_BARGE_IN_ECHO_WINDOW_SECONDS = 2.0
_BARGE_IN_ECHO_STRIKES = 3

# dtmf_outcome 事件里对端话语的截断长度：够判读，又不至于把整段通话搬进事件流。
_OUTCOME_TEXT_CHARS = 80
# 按键后等待对端反应的窗口，与 scripts/regression_call.py 的 8s 观察窗一致。
# 超窗即「按键后无反应」——那本身就是证据，必须落，不能悄悄丢。
_OUTCOME_WINDOW_SECONDS = 8.0
# 超窗之后仍愿意与对端下一句配对的上限；超过它只记「没观察到」，不再配对
# （防止几分钟后的无关话语凑成假证据）。实测 10086 有 9207ms 的应答。
_OUTCOME_LATE_GRACE_SECONDS = 30.0


AudioBridge = ModemAudioBridge | SerialPcmAudioBridge | FfmpegAudioBridge | BluetoothAudioBridge

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
_PARALLEL_PLAYOUT_STALL_SECONDS = 1.0
_INBOUND_TAKEOVER_HOLD_INSTRUCTIONS = {
    "zh": "正在尝试联系机主接听，但尚未接通。结合上下文简短、自然地告知来电者正在等待机主，"
          "不要承诺一定能够转接，也不要展开新的话题。",
    "en": "The owner has been invited to take this call but has not answered yet. Briefly and naturally "
          "let the caller know they are waiting for the owner, without promising a connection or starting a new topic.",
}
_INBOUND_TAKEOVER_MEDIA_TIMEOUT_SECONDS = 15.0
_INBOUND_TRIAGE_CLARIFY_TEXT = {
    "zh": "请简单确认一下，您是有具体事情找本人，还是一般业务介绍？",
    "en": "Just to confirm — do you have a specific matter for them personally, or is this a general offer?",
}
_INBOUND_TRIAGE_REJECT_TEXT = {
    "zh": "谢谢您的来电，目前不需要这项服务。再见。",
    "en": "Thanks for calling — this service isn't needed right now. Goodbye.",
}


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
        modem: SerialModem,
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
        self._parallel_agent_connect = False
        self._last_downlink_play_at = 0.0
        self._active_lock = threading.Lock()
        self._outgoing_audio: Queue[bytes] = Queue()
        # 本轮是否已记过「排队时长」（每轮只记一次，见 _make_agent_audio_handler）。
        self._turn_first_audio_logged = False
        # 上行最近一次「有声」块的到达时刻（monotonic）。首块回复音频到达时用
        # now - 这个值 得到本地可观测的端到端轮次延迟（上行传输 + VAD 判停 +
        # 生成），比「判停→首音频」多覆盖前半段链路；蜂窝两腿仍在其外。
        self._last_voiced_at = 0.0
        # 轮首静音掐除：本轮还允许掐多少字节（轮次边界重置为上限）与已掐字节。
        self._turn_trim_budget = 0
        self._turn_trimmed_bytes = 0
        # barge-in（BARGE_IN_ENABLED）：每通开始时从 config 读取；True 时上行
        # 不做半双工丢弃、对端开口即打断 AI（见 _run_agent_loop 与
        # _handle_peer_barge_in）。自激兜底触发时本通内置回 False。
        self._barge_in = False
        # 自激兜底状态（WIL-94 坑 4）：连续「刚出声即被打断」计数 + 本轮首块
        # 回复音频的到达时刻（0.0 = 本通尚无回复音频）。
        self._barge_in_echo_strikes = 0
        self._turn_audio_started_at = 0.0
        self._record: CallRecord | None = None
        self._inbound_record: CallRecord | None = None
        self._phone_owner_took_over = False
        self._phone_audio_lost = False
        self._call_task: asyncio.Task | None = None
        self._resources_released = True
        self._bluetooth_cleanup_started = False
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
        # hold 状态（WIL-120 二期）：收尾裁判判 on_hold 进入、判其他退出。
        # 时间戳用 time.time()（与事件 ts 同钟），秒数聚合在 metrics 层做。
        self._on_hold = False
        self._hold_started_ts = 0.0
        self._judge_task: asyncio.Task | None = None
        self._wrap_up_carrier_sms_enabled = False
        self._wrap_up_sms_service_number = ""
        self._wrap_up_sms_started_at = 0.0
        self._wrap_up_sms_seen: set[tuple[float, str]] = set()
        self._wrap_up_sms_evidence: list[dict] = []
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
        # WIL-120 一期：profile 级长通话覆盖。None=跟随全局 OUTBOUND_MAX_SECONDS。
        self._profile_max_call_seconds: int | None = None
        self._profile_wrap_up_judge = True
        # WIL-120 三期：结构化任务包（值只进模型上下文，不进 events/metrics）。
        self._profile_task_package: dict | None = None
        self._dtmf_lock = threading.RLock()
        # 上行媒体互斥：drain 的「取空队列再写桥」与 DTMF 的「清队列再入双音」
        # 必须原子，否则清队列会清了个空、语音仍抢在双音前面写进桥。
        self._media_lock = threading.Lock()
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
        self._takeover_hold_audio_at: float | None = None
        self._takeover_attempted = False
        self._takeover_ai_fallback = False
        self._takeover_resume_pending = False
        self._takeover_ai_fallback_notified = False
        self._takeover_resuming = False
        self._takeover_started_at: float | None = None
        self._takeover_deadline: float | None = None
        self._triage_judge: InboundTriageJudge | None = None
        self._triage_mode = "off"
        self._triage_results: Queue[TriageVerdict] = Queue(maxsize=4)
        self._triage_consumer = TriageVerdictConsumer()
        self._triage_pending = False
        # 受限期内 AI 说过的话（WIL-83）。只在 enforce 且判官未定论时累积，
        # 挂断后交给判官检测有没有越界——纯观测，不影响通话行为。
        #
        # 必须加锁：追加发生在 provider 的转写回调线程，取用与清空发生在收尾
        # 路径。不加锁时，取用与清空之间的一次追加会被静默丢掉，清空之后的
        # 一次追加会漏进**下一通**（2026-08-05 Codex 评审 P1）。
        self._restricted_utterances: list[str] = []
        self._restricted_lock = threading.Lock()
        self._triage_terminal = False
        self._triage_reject_deadline: float | None = None
        self._triage_clarification_spoken = False
        # 按键护窗截止时刻（monotonic）：DTMF 期间丢弃 Agent 下行。
        self._dtmf_guard_until = 0.0
        # 本轮护窗内被丢弃的 Agent 下行字节数，窗口结束时汇总成一条事件（#81）。
        self._dtmf_guard_dropped_bytes = 0
        # 按键推进校验（#50 第 4 项）：记住最近一次按键，等对端下一句到达时配对成
        # 一条 dtmf_outcome 事件。只存"上一句对端话语"与"待配对的按键"两样。
        # 单独一把锁：按键在通话线程（持 _dtmf_lock）、转写回调在 realtime 线程，
        # 两边都要改这组状态。不复用 _dtmf_lock —— 那把锁跨越阻塞的 AT 往返。
        self._outcome_lock = threading.Lock()
        self._last_remote_utterance = ""
        self._pending_dtmf_outcome: dict | None = None
        # 单独一把小锁：绝不能复用 _dtmf_lock —— 那把锁在 _send_dtmf_raw 里
        # 跨越阻塞的 AT 往返，音频回调等在上面会直接把下行卡住。
        self._guard_drop_lock = threading.Lock()
        # 单轮长度闸门（WIL-90）。on_agent_audio 是热路径且在 provider 线程上跑，
        # 所以只在这里累加字节、判一次，真正的打断走 run_coroutine_threadsafe。
        self._turn_audio_bytes = 0
        self._turn_last_chunk_at = 0.0
        self._turn_cancel_sent = False
        self._turn_lock = threading.Lock()
        self._max_turn_seconds = 0.0
        # 媒体死亡看门狗（WIL-100）：累计收到的纯数字静音时长（秒）。
        self._dead_media_silent_seconds = 0.0
        self._dead_media_seconds = 0.0
        self._dead_media_hangup = False
        self._dead_media_reported = False
        # 最近一次 Agent 音频采样率，供收尾兜底计算丢弃时长。
        self._agent_output_rate = 24000

    def _publish(self, event: dict) -> None:
        if self.hub:
            self.hub.publish(event)

    @property
    def is_active(self) -> bool:
        with self._active_lock:
            return self._active

    @property
    def owns_modem_shutdown(self) -> bool:
        """Keep admission closed until parallel/Bluetooth cleanup has joined workers."""
        owns_cleanup = self._parallel_agent_connect or (
            isinstance(self.modem, BluetoothModem) and not self._resources_released
        )
        return owns_cleanup and self._thread is not None and self._thread.is_alive()

    @property
    def is_busy(self) -> bool:
        return self.is_active or self.owns_modem_shutdown

    def _set_active(self, value: bool) -> None:
        with self._active_lock:
            self._active = value

    def start(
        self,
        outbound_number: str | None = None,
        task: str | None = None,
        preset_hint: str | None = None,
        preset_id: str | None = None,
        *,
        inbound_record: CallRecord | None = None,
    ) -> None:
        if self.is_busy:
            logger.warning("已有通话进行中，忽略新的呼叫请求")
            return
        self._outbound_number = outbound_number
        self._inbound_record = inbound_record
        self._phone_owner_took_over = False
        self._phone_audio_lost = False
        self._resources_released = False
        self._bluetooth_cleanup_started = False
        self._parallel_agent_connect = config.get_bool("PARALLEL_AGENT_CONNECT")
        self._outbound_task_value = task
        self._preset_hint = preset_hint
        self._preset_id = preset_id
        self._wrap_up_requested = False  # 每通重置收尾裁判状态
        self._on_hold = False
        self._hold_started_ts = 0.0
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
        self._profile_max_call_seconds = None
        self._profile_wrap_up_judge = True
        self._profile_task_package = None
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
        if isinstance(self.modem, BluetoothModem):
            self._cancel_bluetooth_call()
            return
        if self._parallel_agent_connect:
            return  # The parallel owner polls is_active and cancels blocked operations.
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)

    def phone_owner_took_over(self) -> None:
        """Classify a previously fenced SCO closure after live-state confirmation."""
        if not isinstance(self.modem, BluetoothModem):
            return
        self._phone_owner_took_over = True
        self.phone_audio_lost()

    def phone_audio_lost(self) -> None:
        """Fence late audio/tools now, without guessing why SCO closed."""
        if not isinstance(self.modem, BluetoothModem):
            return
        self._phone_audio_lost = True
        self._set_active(False)
        self._clear_outgoing_audio()
        self._cancel_bluetooth_call()

    def _cancel_bluetooth_call(self) -> None:
        loop = self._loop
        task = self._call_task
        if loop and loop.is_running():
            def cancel_call() -> None:
                if task and not task.done() and not task.cancelling() and not self._bluetooth_cleanup_started:
                    task.cancel()
            loop.call_soon_threadsafe(cancel_call)

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
            # All resources/workers are joined. The completion callback may now
            # admit the next queued call even though this thread has not returned.
            self._parallel_agent_connect = False
            self._resources_released = True
            # 会话结束通知（含异常/未接通路径）：驱动外呼队列拨下一个等。
            if self._on_ended is not None:
                try:
                    self._on_ended()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("会话结束回调异常: %s", exc)

    async def _handle_call(self) -> None:
        self._call_task = asyncio.current_task()
        self._clear_outgoing_audio()
        self._load_session_config()
        self._parallel_agent_connect = config.get_bool("PARALLEL_AGENT_CONNECT")
        self._last_downlink_play_at = 0.0

        session_t0 = time.monotonic()
        direction = "outbound" if self._outbound_number else "inbound"
        number = self._outbound_number or self.current_caller
        record = self._begin_record(direction, number)
        self._record = record
        if self._wrap_up_carrier_sms_enabled and record is not None:
            self._wrap_up_sms_started_at = record.started_at
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
            if isinstance(self.modem, BluetoothModem) and not self.is_active:
                status = "not_connected"
                return
            if self._parallel_agent_connect:
                if not await self._handle_parallel_startup(
                    direction, number, record, transcripts, mark, session_t0
                ):
                    status = "not_connected"
                return
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

            # 接听后静置时长由厂商实现决定：自带重试的（SIM7600）不必盲等整秒，
            # 首次失败立刻重试即可，健康态省约 0.8s（WIL-104）。Quectel 维持 1.0s。
            await asyncio.sleep(
                getattr(self.modem, "POST_ANSWER_SETTLE_SECONDS", 1.0)
            )

            # 挂断流程会发 AT+QPCMV=0 关闭语音通道，每通电话都要重新启用，
            # 否则第二通开始模组无 PCM 流（双向无声）。
            self.modem.initialize_for_voice(self.audio_mode)
            # 通话内的启用结果进事件流：ok=False 即整通无声、attempts≥2 即已劣化
            # ——WIL-95 D 组「异常掉话指纹」的蜂窝形态（CPCMREG 替代 RTP keepalive）。
            enable_info = getattr(self.modem, "last_voice_enable", None)
            if record is not None and isinstance(enable_info, dict):
                record.log_event("pcm_enable", **enable_info)

            bridge = create_audio_bridge(
                modem=self.modem,
                mode=self.audio_mode,
                device_keyword=self.audio_keyword,
                pcm_port=self.pcm_port,
                pcm_baudrate=self.pcm_baudrate,
                tx_gain=self.tx_gain,
            )
            agent = create_agent(self.provider)
            self._configure_call_agent(agent, lambda: bridge, record, transcripts, direction, number, session_t0)
            bridge.start()
            mark("bridge_started")

            try:
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
            except BaseException:
                if (isinstance(self.modem, BluetoothModem)
                        or isinstance(bridge, SerialPcmAudioBridge) and bridge.uplink_prebuffer_enabled):
                    await self._shutdown_agent(agent, bridge)
                raise

            active_bridge = bridge
            try:
                active_bridge = await self._run_agent_loop(
                    agent, bridge, record, transcripts
                )
            finally:
                await self._shutdown_agent(agent, active_bridge)
        except asyncio.CancelledError:
            if not isinstance(self.modem, BluetoothModem):
                status = "failed"
                raise
        except BaseException:
            status = "failed"
            raise
        finally:
            if isinstance(self.modem, BluetoothModem):
                self._bluetooth_cleanup_started = True
                if self._phone_audio_lost:
                    if self.modem.audio_loss_pending:
                        await asyncio.to_thread(self.modem.wait_audio_resolution)
                    self._phone_owner_took_over = self.modem.owner_takeover_confirmed
            if self._phone_owner_took_over:
                status = "owner_took_over"
                mark("owner_took_over", physical_call_preserved=True)
            if isinstance(self.modem, BluetoothModem):
                # Includes provider/bridge construction failures and cancelled startup.
                try:
                    await asyncio.to_thread(self.modem.hangup)
                except Exception:
                    logger.warning("Bluetooth call cleanup failed", exc_info=True)
            if self._wrap_up_carrier_sms_enabled:
                await self._stop_wrap_up_sms_judge()
            # 先作废判官世代再 finish record，避免迟到结果在 meta 落盘后追加事件。
            self._stop_dtmf_judge()
            self._stop_triage_judge()
            self._end_takeover_context("CALL_ENDED")
            mark("ended", status=status)
            self._finalize_record(record, status, transcripts, direction, number)
            self._call_task = None

    def _configure_call_agent(
        self, agent, get_bridge: Callable[[], AudioBridge | None], record: CallRecord | None,
        transcripts: list[tuple[str, str]], direction: str, number: str | None, session_t0: float,
    ) -> None:
        self._start_dtmf_judge(record, session_t0=session_t0)
        agent.set_session_instructions(self._build_agent_instructions(direction))
        agent.set_transcript_handler(self._make_transcript_handler(record, transcripts, agent))
        agent.set_repeat_stuck_handler(self._request_repeat_stuck_wrap_up)
        agent.set_status_handler(lambda text: self._publish({"type": "system", "text": text}))
        tools = self._build_tools(direction)
        with self._dtmf_lock:
            self._active_tools = tools
        agent.set_tools(tools)
        bridge = get_bridge()
        if isinstance(bridge, SerialPcmAudioBridge):
            bridge.set_ready_check(self.modem.pcm_ready)
        self._barge_in = config.get_bool("BARGE_IN_ENABLED")
        self._barge_in_echo_strikes = 0
        self._turn_audio_started_at = 0.0
        if self._barge_in:
            agent.set_user_interrupt_handler(lambda: self._handle_peer_barge_in(get_bridge()))
        # The same queue is used before/after bridge startup, including guard-aware cuts.
        agent.set_late_cut_handler(lambda: self._discard_agent_playback(get_bridge(), "复读晚截"))
        if record is not None:
            agent.set_latency_handler(record.log_latency)
            try:
                record.log_event(
                    "config_snapshot", provider=self.provider, model=getattr(agent, "model", "") or "",
                    turn_detection=config.get_str("OPENAI_TURN_DETECTION"),
                    vad_eagerness=config.get_str("OPENAI_VAD_EAGERNESS"),
                    vad_silence_ms=config.get_int("OPENAI_VAD_SILENCE_MS"),
                    barge_in_enabled=self._barge_in, hangover_seconds=self._hangover_seconds,
                    dtmf_mode=config.get_str("DTMF_MODE"),
                    recording_enabled=getattr(record, "recording_enabled", False),
                    audio_mode=self.audio_mode, modem_vendor=config.get_str("MODEM_VENDOR"),
                    **({"parallel_agent_connect": True} if self._parallel_agent_connect else {}),
                )
            except Exception:  # noqa: BLE001
                logger.warning("配置快照落盘失败（不影响通话）", exc_info=True)
            try:
                known = bool(number) and number in known_contact_numbers(self.hub, self.call_logger)
                record.log_event("call_context", contact_known=known)
            except Exception:  # noqa: BLE001
                pass

    async def _threaded_modem_call(self, operation: Callable, *args):
        # Cancelling to_thread does not stop its thread. Join before any later hangup/reset.
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _cleanup_step(self, stage: str, operation: Awaitable) -> None:
        started = time.monotonic()
        logger.info("[timing] 通话收尾开始 stage=%s", stage)
        try:
            await operation
        finally:
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            logger.info("[timing] 通话收尾 stage=%s elapsed_ms=%.1f", stage, elapsed_ms)
            if self._record is not None:
                try:
                    self._record.log_event("cleanup_stage", stage=stage, duration_ms=elapsed_ms)
                except Exception:  # noqa: BLE001
                    logger.warning("收尾计时落盘失败（不影响清理）")

    async def _handle_parallel_startup(
        self, direction: str, number: str | None, record: CallRecord | None,
        transcripts: list[tuple[str, str]], mark: Callable[..., float], session_t0: float,
    ) -> bool:
        agent = None
        bridge: AudioBridge | None = None
        active_bridge: AudioBridge | None = None
        prepare_task: asyncio.Task | None = None
        connect_task: asyncio.Task | None = None
        loop_task: asyncio.Task | None = None
        stop_preparing = threading.Event()
        bridge_ready = threading.Event()
        agent_ready = asyncio.Event()
        generation = self._session_generation
        greeting_at: float | None = None
        first_output = False
        first_received = False
        output_lock = threading.Lock()
        hangup_owned = True
        native_greeting = (
            config.get_str("MODEM_ANSWER_GREETING")
            if direction == "inbound" and isinstance(self.modem, Sim7600Modem) else ""
        )
        native_greeting_spoken = ""
        native_greeting_done = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        answered_at = 0.0

        def startup_current() -> bool:
            return not stop_preparing.is_set() and self._agent_generation_current(generation)

        def note_received() -> None:
            nonlocal first_received
            if not first_received:
                first_received = True
                mark("agent_audio_received")

        def observe_output(pcm: bytes) -> None:
            nonlocal first_output
            # This observer belongs to this record/bridge, not the next session.
            # Bytes accepted while stop() is closing the provider still played.
            if record is not None:
                record.write_downlink(pcm)
            if not self._agent_generation_current(generation):
                return
            self._last_downlink_play_at = time.monotonic()
            with output_lock:
                if first_output:
                    return
                first_output = True
                elapsed = None if greeting_at is None else max(0, round((time.monotonic() - greeting_at) * 1000))
                boundary = {
                    "nmea": "pcm_written", "uac": "audio_device_written", "uac_ffmpeg": "playback_pipe_written",
                }[self.audio_mode]
                if isinstance(self.modem, BluetoothModem):
                    boundary = "sco_written"
                mark("first_audio", boundary=boundary, **({"ms": elapsed} if elapsed is not None else {}))
            if elapsed is not None:
                logger.info("首音频延迟: %dms (本地下行写出)", elapsed)

        def prepare_media() -> None:
            nonlocal bridge, native_greeting_spoken
            if native_greeting:
                delay = max(0, config.get_int("MODEM_ANSWER_GREETING_DELAY_MS")) / 1000
                delay = max(0.0, answered_at + delay - time.monotonic())
            else:
                delay = getattr(self.modem, "POST_ANSWER_SETTLE_SECONDS", 1.0)
            if stop_preparing.wait(delay):
                return
            if not startup_current():
                return
            if native_greeting:
                assert isinstance(self.modem, Sim7600Modem)
                started = time.monotonic()
                try:
                    ok = self.modem.play_answer_greeting(native_greeting)
                    if ok:
                        native_greeting_spoken = native_greeting
                    mark(
                        "module_greeting", ok=ok,
                        duration_ms=round((time.monotonic() - started) * 1000, 1),
                        started_after_answer_ms=round((started - answered_at) * 1000, 1),
                    )
                finally:
                    # Connect stays parallel; only the opening needs the CTTS result.
                    event_loop.call_soon_threadsafe(native_greeting_done.set)
                if not startup_current():
                    return
            self.modem.initialize_for_voice(self.audio_mode)
            enable_info = getattr(self.modem, "last_voice_enable", None)
            if record is not None and isinstance(enable_info, dict):
                record.log_event("pcm_enable", **enable_info)
            if isinstance(enable_info, dict) and enable_info.get("ok") is False:
                raise RuntimeError("PCM 启用失败，结束通话以避免死寂线路")
            if not startup_current():
                return
            mark("pcm_ready")
            bridge = create_audio_bridge(
                modem=self.modem,
                mode=self.audio_mode, device_keyword=self.audio_keyword, pcm_port=self.pcm_port,
                pcm_baudrate=self.pcm_baudrate, tx_gain=self.tx_gain,
            )
            if isinstance(bridge, SerialPcmAudioBridge):
                bridge.set_ready_check(self.modem.pcm_ready)
            bridge.set_output_observer(observe_output)
            if not startup_current():
                return
            bridge.start()
            mark("bridge_started")
            bridge_ready.set()

        async def connect_and_greet() -> None:
            nonlocal greeting_at
            assert agent is not None
            mark("agent_connect_started")
            await agent.start(self._make_agent_audio_handler(
                agent, None, record, note_received, bridge_getter=lambda: bridge, record_at_playout=True,
            ))
            if not startup_current():
                return
            mark("agent_connected")
            mark("agent_started")
            agent_ready.set()
            if direction == "outbound" and self._prompt_gen_opening_mode == "wait":
                mark("opening_skipped", mode="wait")
            else:
                if native_greeting:
                    await native_greeting_done.wait()
                    if not startup_current():
                        return
                greeting_at = mark("greeting_requested")
                await agent.say(self._opening_instructions(direction, module_greeting=native_greeting_spoken))
                mark("greeting_sent")

        try:
            if self._outbound_number and not await self._connect_outbound(mark, threaded=True):
                hangup_owned = False
                return False
            agent = create_agent(self.provider)
            self._configure_call_agent(agent, lambda: bridge, record, transcripts, direction, number, session_t0)
            if not self._outbound_number:
                logger.info("开始处理来电（并行准备）...")
                await self._threaded_modem_call(self.modem.answer)
            if not startup_current():
                return True
            self._publish({"type": "call", "status": "answered", "caller": self.current_caller})
            answered_at = mark("answered")
            prepare_task = asyncio.create_task(asyncio.to_thread(prepare_media))
            connect_task = asyncio.create_task(connect_and_greet())
            while startup_current():
                for task in (prepare_task, connect_task):
                    if task.done():
                        task.result()
                if bridge_ready.is_set() and agent_ready.is_set():
                    break
                await asyncio.sleep(0.01)
            if not startup_current() or bridge is None or not bridge_ready.is_set():
                return True
            active_bridge = bridge
            # Run normal duplex/recording/takeover processing even while say() is streaming.
            loop_task = asyncio.create_task(self._run_agent_loop(agent, bridge, record, transcripts))
            connect_checked = False
            # Physical hangup must cancel a loop blocked in say()/send_audio(),
            # not wait for that operation to notice is_active on its own.
            while self.is_active:
                if connect_task.done() and not connect_checked:
                    connect_checked = True
                    if generation == self._session_generation:
                        connect_task.result()
                    else:
                        # Mobile takeover fences the AI owner, not the physical call.
                        await asyncio.gather(connect_task, return_exceptions=True)
                if loop_task.done():
                    active_bridge = loop_task.result()
                    break
                if generation == self._session_generation and agent.fatal:
                    break
                await asyncio.sleep(0.01)
            return True
        finally:
            if isinstance(self.modem, BluetoothModem):
                self._bluetooth_cleanup_started = True
            stop_preparing.set()
            self._set_active(False)
            for pending_task in (connect_task, loop_task):
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()
            await self._cleanup_step("agent_tasks", asyncio.gather(
                *(t for t in (connect_task, loop_task) if t is not None), return_exceptions=True,
            ))
            if loop_task is not None and not loop_task.cancelled() and loop_task.exception() is None:
                # Cancellation can arrive after mobile handoff returned its new
                # bridge but before the normal assignment above resumes.
                active_bridge = loop_task.result()
            await self._stop_agent_resources(agent, None)
            if prepare_task is not None:
                await self._cleanup_step("modem_prepare", asyncio.shield(asyncio.gather(
                    prepare_task, return_exceptions=True,
                )))
            self._clear_outgoing_audio()
            if hangup_owned:
                await self._shutdown_agent(None, active_bridge or bridge, threaded=True)

    async def _connect_outbound(self, mark: Callable[..., float], *, threaded: bool = False) -> bool:
        """外呼：拨号并等待接通；未接通时发结束事件、挂断并返回 False。"""
        number = self._outbound_number
        if number is None:
            raise RuntimeError("外呼号码未设置")
        logger.info("开始外呼: %s", number)
        self.current_caller = number
        self._start_prompt_generation()
        if threaded:
            await self._threaded_modem_call(self.modem.dial, number)
        else:
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
            if threaded:
                await self._threaded_modem_call(self.modem.hangup)
            else:
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
                # WIL-83：受限期内 AI 说过的话单独留一份，挂断后判越界。
                # 这里只是采样，不做任何判断——判断在 _check_triage_compliance。
                if self._triage_pending and text:
                    with self._restricted_lock:
                        self._restricted_utterances.append(text)
                self._schedule_spoken_dtmf_followup(agent, text)
            elif role == "user":
                self._settle_dtmf_outcome(record, text)
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
        bridge: AudioBridge | None,
        record: CallRecord | None,
        on_first_audio: Callable[[], None] | None = None,
        *,
        bridge_getter: Callable[[], AudioBridge | None] | None = None,
        record_at_playout: bool = False,
    ) -> Callable[[bytes], None]:
        """Agent 下行音频回调：浏览器实时旁听、本机监听旁路、重采样到 8k、录音、入发送队列。"""

        self._agent_output_rate = agent.output_rate
        # 实时旁听按 Agent 输出采样率播放（qwen/openai 均 24k）。
        if self.hub is not None:
            self.hub.set_audio_rate(agent.output_rate)

        generation = self._session_generation

        def on_agent_audio(pcm_agent: bytes) -> None:
            if not self._agent_effect_allowed(generation):
                return
            # Local TTS learns its real output rate during start(), after wiring this callback.
            if agent.output_rate != self._agent_output_rate:
                self._agent_output_rate = agent.output_rate
                if self.hub is not None:
                    self.hub.set_audio_rate(self._agent_output_rate)
            if pcm_agent and self._takeover_hold_generation == generation and not self._takeover_hold_done:
                self._takeover_hold_audio_at = time.monotonic()
            # 单轮长度闸门必须在护窗**之前**结算（2026-08-05 Codex 评审 P1）。
            # 闸门量的是「模型生成了多久」，护窗丢的是「要不要送出去」——两件事。
            # 放在护窗之后会让护窗期内生成的音频对闸门完全不可见：轻则晚触发，
            # 重则护窗一旦长过 _TURN_GAP_SECONDS，下一块就被当成新一轮而清零计数，
            # 跑飞的长轮次就此绕过闸门。而这恰恰是 IVR 路径——模型边按键边说话
            # 正是 WIL-49 记录在案的行为。
            if self._check_turn_length(pcm_agent, agent, record):
                # 本轮已超上限：**丢掉后续下行**。这才是真正的闸门。
                #
                # 2026-08-06 真机验证（拨 10086）证明「靠 response.cancel 掐生成」
                # 完全无效：OpenAI 把整轮音频以突发写完的速度，快过我们按字节
                # 累计到阈值——事件记了 turn_length_capped、日志写了「打断本轮」，
                # 而 provider 回的是 response_cancel_not_active（响应早就结束了），
                # 队列照常放完，对端把 14.4s 和 29.6s 的两轮整段听完了。
                #
                # 要限制的是**对端听到多久**，就必须在「送进下行队列」这一步拦，
                # 不能指望上游停止生成。DTMF 双音走 _send_dtmf_raw 单独入队，
                # 不经过这里，所以 WIL-49 的按键不受影响。
                return
            # 按键护窗：DTMF 期间丢弃 Agent 下行，别让语音和双音挤在同一段上行。
            # 真机 2026-08-03 拨 10086：模型一边说「这边先按一下对应的键」一边按，
            # 4 次本机 success、IVR 菜单一次没推进（见 #45 评论）。
            if time.monotonic() < self._dtmf_guard_until:
                # 按窗累计而不是逐块记事件：这里是热路径（约每 20ms 一块），
                # 逐块写会把 events.jsonl 冲垮。窗口结束后统一落一条。
                with self._guard_drop_lock:
                    self._dtmf_guard_dropped_bytes += len(pcm_agent)
                return
            self._flush_dtmf_guard_drop(record, agent.output_rate)
            if pcm_agent and on_first_audio is not None:
                on_first_audio()
            # 浏览器实时旁听下行 AI（Web Audio）：无监听端时零成本返回。kind=0=下行。
            if self.hub is not None:
                self.hub.broadcast_audio(pcm_agent, kind=0)
            monitor = self.monitor
            if monitor is not None:
                monitor.feed(pcm_agent)
            current_bridge = bridge_getter() if bridge_getter is not None else bridge
            if current_bridge is None:
                pcm_8k = ModemAudioBridge.agent_to_modem(pcm_agent, agent.output_rate)
                if self.audio_mode in {"nmea", "uac_ffmpeg"}:
                    pcm_8k = apply_pcm_gain(pcm_8k, self.tx_gain)
            else:
                pcm_8k = current_bridge.agent_to_modem(pcm_agent, agent.output_rate)
                if hasattr(current_bridge, "amplify_for_modem"):
                    pcm_8k = current_bridge.amplify_for_modem(pcm_8k)
            # 掐掉本轮开头的静音垫（WIL-112 最后一块）：TTS 音频前缘实测垫
            # 中位 470ms、最长 ~1s 纯静音——仪器在「首字节到达」停表，对端耳朵
            # 却要等垫子播完。只掐轮首、设上限防误伤；句中停顿原样保留。
            if pcm_8k and self._turn_trim_budget > 0:
                pcm_8k = self._trim_leading_silence(pcm_8k)
            if pcm_8k:
                if record is not None and not record_at_playout:
                    record.write_downlink(pcm_8k)
                # 本轮首块音频入队时，记录「它前面还排着多少秒没播完」——这才是
                # 对端真正要等的时间。判停→首音频只是 OpenAI 生成耗时，不含排队
                # （WIL-112：录音按生成时点记录，会把排队完全抹掉，别再用它判断）。
                if not self._turn_first_audio_logged:
                    self._turn_first_audio_logged = True
                    # 自激兜底的时间锚点：本轮首块回复音频到达时刻。
                    self._turn_audio_started_at = time.monotonic()
                    pending = (
                        current_bridge.pending_output_bytes()
                        if current_bridge is not None and hasattr(current_bridge, "pending_output_bytes") else 0
                    )
                    queued = self._outgoing_audio.qsize() * len(pcm_8k)
                    ahead = (pending + queued) / (MODEM_RATE * 2)
                    # playout_backlog_ms（2026-08-14 定义，WIL-95 第一期）：本轮
                    # 首块回复入队时，前面已排未播的音频时长。每轮都落盘——
                    # 0 也是有效读数（无积压），只有日志沿用 0.5s 的降噪阈值。
                    if record is not None:
                        # 埋点失败绝不影响通话（WIL-95 §6；BrokenRecord 测试锁）。
                        try:
                            record.log_latency(
                                "playout_backlog", round(ahead * 1000, 1)
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    if ahead >= 0.5:
                        logger.info(
                            "[timing] 本轮回复前面还排着 %.1fs 未播完（对端要多等这么久）",
                            ahead,
                        )
                    # 端到端（本地可观测）：上行最后一块有声 → 首块回复到达。
                    # 覆盖上行传输 + VAD 判停 + 生成；对端体感还要再加蜂窝两腿
                    # （各 ~0.2-0.3s）与本地播出排队（上面的 ahead）。
                    voiced_at = self._last_voiced_at
                    if voiced_at > 0:
                        e2e = time.monotonic() - voiced_at
                        if 0 < e2e < 30:
                            # local_response_latency_ms（2026-08-14 定义，WIL-95
                            # 2.1：诚实命名，不叫 e2e——蜂窝两腿在观测范围外）。
                            if record is not None:
                                try:
                                    record.log_latency(
                                        "local_response", round(e2e * 1000, 1)
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                            logger.info(
                                "[timing] 端到端轮次延迟(本地音尾→首音频): %.0fms",
                                e2e * 1000,
                            )
                self._outgoing_audio.put(AgentPcm(pcm_8k) if record_at_playout else pcm_8k)

        return on_agent_audio

    def _trim_leading_silence(self, pcm_8k: bytes) -> bytes:
        """掐掉本轮开头的静音垫，返回剩余部分（可能为空）。

        预算（_turn_trim_budget）由轮次边界重置为上限：静音块整块掐掉直到
        预算耗尽或遇到第一个过阈样本；一旦见声或预算耗尽即停，本轮不再触碰
        （句中停顿因此不受影响）。
        """
        usable = len(pcm_8k) - (len(pcm_8k) % 2)
        if not usable:
            return pcm_8k
        samples = array("h", pcm_8k[:usable])
        if max(max(samples), -min(samples)) < _DOWNLINK_SILENCE_PEAK:
            drop = min(len(pcm_8k), self._turn_trim_budget)
            self._turn_trimmed_bytes += drop
            self._turn_trim_budget -= drop
            if self._turn_trim_budget <= 0:
                self._log_turn_trim()
            return pcm_8k[drop:]
        cut_idx = 0
        for k, sample in enumerate(samples):
            if abs(sample) >= _DOWNLINK_SILENCE_PEAK:
                cut_idx = k
                break
        cut = min(cut_idx * 2, self._turn_trim_budget)
        self._turn_trimmed_bytes += cut
        self._turn_trim_budget = 0
        self._log_turn_trim()
        return pcm_8k[cut:]

    def _log_turn_trim(self) -> None:
        if self._turn_trimmed_bytes:
            logger.info(
                "[timing] 已掐掉本轮开头静音 %dms",
                self._turn_trimmed_bytes // 16,
            )
        self._turn_trimmed_bytes = 0

    async def _speak_takeover_hold_if_needed(
        self, agent: VoiceAgent, bridge: AudioBridge, generation: int
    ) -> None:
        """Allow the asynchronous hold response to reach the caller before fencing."""

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
            remaining = max(0.0, (self._takeover_deadline or time.monotonic()) - time.monotonic())
            await asyncio.wait_for(agent.say(_INBOUND_TAKEOVER_HOLD_INSTRUCTIONS[agent_language()]), timeout=remaining)
            speech_deadline = min(self._takeover_deadline or time.monotonic(), time.monotonic() + 4.0)
            while self._agent_generation_current(generation) and time.monotonic() < speech_deadline:
                self._drain_agent_audio(bridge)
                last_audio = self._takeover_hold_audio_at
                if last_audio is not None and time.monotonic() - last_audio >= 0.35:
                    break
                await asyncio.sleep(0.01)
        except Exception as exc:  # noqa: BLE001
            logger.warning("播放接管垫话失败: error_type=%s", type(exc).__name__)
        finally:
            with self._takeover_lock:
                if generation == self._session_generation:
                    self._takeover_hold_done = True

    async def _run_agent_loop(
        self,
        agent,
        bridge: AudioBridge,
        record: CallRecord | None,
        transcripts: list[tuple[str, str]],
    ) -> AudioBridge:
        """通话主循环：下行音频搬运 + 半双工防回环的上行转发。"""
        last_play_at = self._last_downlink_play_at if self._parallel_agent_connect else 0.0
        loop_started = time.monotonic()
        last_output_progress_at = loop_started
        previous_pending = 0
        previous_output_progress = 0
        last_uplink_gate: str | None = None
        last_gate_log_at = 0.0
        # 外呼硬时限：LLM 收尾裁判失灵/漏判时的最后防线（到点道别挂断）。
        # profile 可覆盖（WIL-120 一期）：长通话（客服排队 30 分钟+）是场景属性，
        # 只给点名的预设放开；0=不限时，其余外呼保持全局安全默认。
        outbound_max_seconds = 0.0
        if self._outbound_number:
            if self._profile_max_call_seconds is not None:
                outbound_max_seconds = float(self._profile_max_call_seconds)
            else:
                outbound_max_seconds = float(
                    config.get_int("OUTBOUND_MAX_SECONDS")
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
        # profile 可显式关闭裁判（WIL-120 一期）：排队循环音会被误判「打转」。
        # 二期换 hold 冻结后收回这个一刀切开关。
        judge_enabled = (
            self._outbound_number is not None and self._profile_wrap_up_judge
        )
        judge_grace = config.get_float("WRAP_UP_JUDGE_GRACE_SECONDS")
        judge_interval = config.get_float("WRAP_UP_JUDGE_INTERVAL_SECONDS")
        last_judge_at = loop_started
        if record is not None and self._outbound_number is not None:
            # 生效时限随通落盘（WIL-95 归因惯例）：没有这条，长通话演练的
            # 指标就无法区分「配置放开了」和「默认 150s 恰好没触发」。
            try:
                record.log_event(
                    "call_limits",
                    outbound_max_seconds=outbound_max_seconds,
                    wrap_up_judge=judge_enabled,
                )
            except Exception:  # noqa: BLE001
                pass
        # goal 只看方向、不看裁判开关：profile 关掉收尾裁判时 task_goal 证据
        # 仍须落盘（WIL-95 §4——离线判官对长排队通话照样要裁「目的是否达成」）。
        goal = (
            self._outbound_task(agent_language())
            if self._outbound_number is not None
            else ""
        )
        if record is not None and goal:
            # 任务目标随通落盘（WIL-95 §4 证据层）：判官离线裁决「目的是否达成」
            # 必须知道目的是什么；不落盘则历史通话永远无法重算。
            try:
                record.log_event("task_goal", goal=goal)
            except Exception:  # noqa: BLE001
                pass
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
            self._expire_takeover(generation=generation)
            triage_action = await self._consume_triage_results(
                agent, bridge, generation
            )
            if triage_action == "reject":
                winddown_deadline = self._triage_reject_deadline
            await self._speak_takeover_hold_if_needed(agent, bridge, generation)
            self._expire_takeover(generation=generation)
            await self._resume_ai_after_takeover(agent, bridge, generation)
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
            # 按键后对端一直不吭声 —— 超窗就把「沉默」本身记下来（WIL-101）。
            #
            # 这个检查此前**在生产上没有任何调用者**，只有单测直接调它，于是
            # 它想记录的那类证据一条都没落过。而「对端毫无反应」恰恰是按键失败
            # 最典型的表现，缺了它，按键有效率会被系统性高估——WIL-72④ 立项
            # 的理由本就是本机 result:success 不可信。
            #
            # 挂在主循环而不是另起 Timer：循环本来就在跑，不用管定时器的生命
            # 周期（跨通清理、世代作废），也就不会引入新的串味风险。
            self._expire_dtmf_outcome(record)

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
                self._log_winddown(record, "outbound_deadline")
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
                self._log_winddown(record, "wrap_up_judge")
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
            if self._parallel_agent_connect:
                progress = bridge.output_progress_bytes() if hasattr(bridge, "output_progress_bytes") else None
                progressed = (
                    progress != previous_output_progress if progress is not None else pending < previous_pending
                )
                if pending == 0 or progressed or self._last_downlink_play_at > last_output_progress_at:
                    last_output_progress_at = now
                if progress is not None:
                    previous_output_progress = progress
                if (pending > 0 and now - last_output_progress_at >= _PARALLEL_PLAYOUT_STALL_SECONDS
                        and now >= self._dtmf_guard_until and hasattr(bridge, "discard_pending_output")):
                    if self._discard_agent_playback(bridge, "下行写出停滞"):
                        logger.warning("下行写出停滞，已清积压并释放半双工屏蔽")
                        if record is not None:
                            try:
                                record.log_event("playout_stalled", pending_bytes=pending)
                            except Exception:  # noqa: BLE001
                                logger.warning("下行停滞事件落盘失败（不影响通话）")
                        pending = 0
                        last_play_at = self._last_downlink_play_at
                        last_output_progress_at = now
                previous_pending = pending
            agent_speaking = pending > 0 or not self._outgoing_audio.empty()
            if self._parallel_agent_connect:
                last_play_at = max(last_play_at, self._last_downlink_play_at)
            if agent_speaking:
                last_play_at = now
            # 半双工防回环：Agent 说话期间（含挂尾窗口）丢弃上行，
            # 避免模组把下行音频回采给千问导致自循环。
            # barge-in 模式下不丢：上行全程送 provider，由其 VAD 触发打断
            # （前提是模组无回采——SIM7600 CPCMREG 实测干净，见 BARGE_IN_ENABLED）。
            suppress_uplink = not self._barge_in and (
                agent_speaking
                or (now - last_play_at) < self._hangover_seconds
            )
            if self._parallel_agent_connect:
                gate = "ownership" if not agent_effects_allowed else "half_duplex" if suppress_uplink else "forward"
                if gate != last_uplink_gate or (gate != "forward" and now - last_gate_log_at >= 5):
                    state = self.takeover_state
                    fields = {
                        "reason": gate, "pending_bytes": pending, "queued_chunks": self._outgoing_audio.qsize(),
                        "last_write_age_ms": max(0, round((now - self._last_downlink_play_at) * 1000, 1))
                        if self._last_downlink_play_at else None,
                        "triage_terminal": self._triage_terminal,
                        "takeover_state": state.value if state is not None else None,
                    }
                    logger.info("[timing] 上行门状态 %s", fields)
                    if record is not None:
                        try:
                            record.log_event("uplink_gate", **fields)
                        except Exception:  # noqa: BLE001
                            logger.warning("上行门事件落盘失败（不影响通话）")
                    last_uplink_gate = gate
                    last_gate_log_at = now

            pcm_8k = bridge.read_modem_chunk()
            if self._dead_media_expired(pcm_8k, now) and not self._dead_media_reported:
                # 检测**总是**落事件；是否据此挂断另有开关（见 _dead_media_expired）。
                self._dead_media_reported = True
                logger.warning(
                    "上行已连续收到 %.0f 秒纯数字静音（仍在出帧）——媒体可能已死；"
                    "本次%s收尾",
                    self._dead_media_silent_seconds,
                    "" if self._dead_media_hangup else "不",
                )
                if record is not None:
                    record.log_event(
                        "dead_media_detected",
                        silent_seconds=round(self._dead_media_silent_seconds, 1),
                        hangup=self._dead_media_hangup,
                    )
                if self._dead_media_hangup:
                    break
            if pcm_8k:
                # 端到端轮次延迟的起点：这块上行是否「有声」。峰值扫描与
                # PcmFlowStats.add 同量级（每块几百样本），开销可忽略。
                usable = len(pcm_8k) - (len(pcm_8k) % 2)
                if usable:
                    samples = array("h", pcm_8k[:usable])
                    if max(max(samples), -min(samples)) >= _VOICED_PEAK_THRESHOLD:
                        # 静音 >1s 后再次有声 = 一次新发声的起点。与 provider 的
                        # speech_started 到达时刻对表，可分离「上行传输延迟」与
                        # 「VAD 判停拖尾」（WIL-112 暗区定位）。
                        if now - self._last_voiced_at > 1.0:
                            logger.info("[timing] 本地检测到语音起点")
                        self._last_voiced_at = now
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
                # Historical startup speech must not cancel a later AI opening through
                # delayed provider VAD. Wait profiles have no opening to protect.
                replay_during_opening = isinstance(pcm_8k, PrebufferedPcm) and not (
                    self._outbound_number and self._prompt_gen_opening_mode == "wait"
                )
                if not suppress_uplink and not replay_during_opening and agent_effects_allowed:
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
                self._finish_takeover_precommit("ENDPOINT_UNAVAILABLE", generation=generation)
                await self._resume_ai_after_takeover(agent, old_bridge, generation)
            return None
        endpoint: RemoteMediaEndpoint | None = None
        new_bridge: AudioBridge | None = None
        committed = False
        old_stopped = False
        connect_timeout_reason: str | None = None
        try:
            endpoint = factory(claimed.issued)
            owner_deadline = self._takeover_deadline
            now = time.monotonic()
            deadline = min(
                owner_deadline if owner_deadline is not None else now,
                now + _INBOUND_TAKEOVER_MEDIA_TIMEOUT_SECONDS,
            )
            deadline_reason = (
                "OWNER_TIMEOUT" if owner_deadline is not None and deadline == owner_deadline
                else "MEDIA_PREPARE_FAILED"
            )
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                connect_timeout_reason = deadline_reason
                raise TimeoutError("takeover_deadline")
            connect_timeout = asyncio.timeout(remaining)
            try:
                async with connect_timeout:
                    await endpoint.connect()
            except TimeoutError:
                if connect_timeout.expired():
                    # Timers can fire a clock tick early; classify the budget, not a later clock read.
                    connect_timeout_reason = deadline_reason
                raise
            while not endpoint.media_ready and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if not endpoint.media_ready:
                raise RuntimeError("takeover_media_not_ready")
            if not self._agent_generation_current(generation) or time.monotonic() >= deadline:
                raise TimeoutError("takeover_deadline")

            # No two writers: stop the AI bridge before creating the mobile bridge.
            old_bridge.stop()
            old_stopped = True
            new_bridge = create_audio_bridge(
                modem=self.modem,
                mode=self.audio_mode,
                device_keyword=self.audio_keyword,
                pcm_port=self.pcm_port,
                pcm_baudrate=self.pcm_baudrate,
                tx_gain=self.tx_gain,
            )
            if isinstance(new_bridge, SerialPcmAudioBridge):
                new_bridge.set_ready_check(self.modem.pcm_ready)
            new_bridge.start()
            if not self._agent_generation_current(generation) or (
                self._takeover_deadline is not None and time.monotonic() >= self._takeover_deadline
            ):
                raise TimeoutError("takeover_deadline")

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
        except asyncio.CancelledError:
            if self._parallel_agent_connect and new_bridge is not None:
                await self._threaded_modem_call(new_bridge.stop)
            raise
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
            reason = connect_timeout_reason or (
                "OWNER_TIMEOUT" if self._takeover_deadline is not None
                and time.monotonic() >= self._takeover_deadline else "MEDIA_PREPARE_FAILED"
            )
            self._finish_takeover_precommit(reason, generation=generation)
            await self._resume_ai_after_takeover(agent, old_bridge, generation)
            return None
        finally:
            if endpoint is not None:
                try:
                    await asyncio.wait_for(endpoint.close(), timeout=2.0)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("关闭接管媒体失败: error_type=%s", type(exc).__name__)

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
        self._max_turn_seconds = config.get_float("AGENT_MAX_TURN_SECONDS")
        self._dead_media_seconds = config.get_float("DEAD_MEDIA_TIMEOUT_SECONDS")
        self._dead_media_hangup = config.get_bool("DEAD_MEDIA_HANGUP")
        self._dead_media_silent_seconds = 0.0
        self._dead_media_reported = False
        self._hangup_delay_seconds = config.get_float("HANGUP_TOOL_DELAY_SECONDS")
        self._wrap_up_carrier_sms_enabled = False
        self._wrap_up_sms_service_number = ""
        self._wrap_up_sms_started_at = 0.0
        self._wrap_up_sms_seen = set()
        self._wrap_up_sms_evidence = []
        if config.get_bool("WRAP_UP_JUDGE_CARRIER_SMS") and getattr(self.modem, "supports_sms", True):
            sim = getattr(self.modem, "sim_identity", None)
            service_number = str(getattr(sim, "service_number", "") or "")
            if is_carrier_service_call(self._outbound_number, service_number):
                self._wrap_up_carrier_sms_enabled = True
                self._wrap_up_sms_service_number = service_number
                self._wrap_up_sms_started_at = time.time()

    def _begin_record(self, direction: str, number: str | None) -> CallRecord | None:
        """创建通话记录；录音选择在每通开始时锁定，本通中途不再变化。"""
        if isinstance(self.modem, BluetoothModem) and direction == "inbound" and self._inbound_record:
            record, self._inbound_record = self._inbound_record, None
            record.recording_enabled = config.get_bool("RECORDING_ENABLED")
            return record
        if self.call_logger is None:
            return None
        try:
            return self.call_logger.begin_call(
                direction,
                number,
                **({"source": "bluetooth"} if isinstance(self.modem, BluetoothModem) else {}),
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
            # 即便这通没有 record（关了通话记录 / 创建失败），受限期采集也必须
            # 在这里清掉——否则它会漏进下一通有 record 的通话，把别人的话算到
            # 那一通头上（2026-08-05 Codex 评审 P1）。
            with self._restricted_lock:
                self._restricted_utterances.clear()
            return
        # 兜底汇总最后一个护窗：常见形态正是「模型说完『我按一下』→ 按键 →
        # 随后安静等 IVR」，那样就永远等不到下一块非护窗音频来触发汇总，
        # 这条事件会整个丢掉——而它恰恰是用来解释这一段的（Codex 评审 P1）。
        self._flush_dtmf_guard_drop(record, self._agent_output_rate)
        try:
            record.finish(status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("落盘通话记录 %s 失败: %s", record.id, exc)
        self._check_triage_compliance(record)
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

    def _check_triage_compliance(self, record: CallRecord | None) -> None:
        """检测分诊受限期内 AI 有没有越界，落一条结构化事件（WIL-83）。

        **纯观测**：不拦截、不改写、不影响通话。它要回答的是「这条约束在生产里
        多久破一次」——今天这个数字完全未知，而它正是决定「继续靠提示词
        还是搬到编排层」的依据。

        判定走文本模型（非枚举硬原则：「我会转告」有无数种说法，禁语表必然漏），
        fail-closed：判不出来当作未越界，宁可漏报也不用一次判定失败去污染统计。

        跑在后台线程：判官是带 8s 超时的网络调用，不能卡住通话收尾。
        `finish()` 之后再 log_event 会直接追加到磁盘，所以事件不会丢。
        """
        with self._restricted_lock:
            utterances = list(self._restricted_utterances)
            self._restricted_utterances.clear()
        if record is None or not utterances:
            return

        def worker() -> None:
            try:
                from .triage_compliance import detect_restricted_violation

                verdict = detect_restricted_violation(utterances)
            except Exception as exc:  # noqa: BLE001
                verdict = {"status": "unavailable", "reason": type(exc).__name__}
            status = verdict.get("status")
            if status == "violation":
                index = int(verdict["index"])
                logger.warning(
                    "分诊受限期内 AI 越界（第 %d 句，%s）——受限话术未被遵守",
                    index + 1,
                    verdict["reason_code"],
                )
                record.log_event(
                    "triage_restriction_check",
                    status="violation",
                    utterances=len(utterances),
                    index=index,
                    reason_code=verdict["reason_code"],
                    # 留原文便于人工复核判官判得对不对——判官本身也会出错（WIL-74）。
                    # 这段文本在同一份 events.jsonl 的 transcript 事件里本就有，
                    # 不构成新的暴露面；但若日后把本事件单独导出到别处，要重新评估。
                    text=utterances[index][:200],
                )
                return
            if status == "compliant":
                record.log_event(
                    "triage_restriction_check",
                    status="compliant",
                    utterances=len(utterances),
                )
                return
            # unavailable：**不能记成 compliant**。观测器的价值就是那个比例，
            # 把判不出来的并进合规会让分母虚高、越界率虚低（Codex 评审 P1）。
            logger.warning(
                "分诊受限期合规检测不可用（%s），本通不计入统计",
                verdict.get("reason", "unknown"),
            )
            record.log_event(
                "triage_restriction_check",
                status="unavailable",
                utterances=len(utterances),
                reason=str(verdict.get("reason", "unknown"))[:40],
            )

        threading.Thread(
            target=worker, name="triage-compliance", daemon=True
        ).start()

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
                    self._outbound_task(agent_language()),
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
        learner_task: str = "",
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
                    and getattr(self.modem, "supports_sms", True)
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
                    # 时间窗+发件人不足以判定相关：运营商在通话期间也推营销短信。
                    # 挑出真正回答本次任务的那条，判定不可用则 fail-closed 到未核实。
                    evidence = select_task_relevant_evidence(
                        evidence,
                        # 相关性判定的任务口径：预设 hint 优先（声明
                        # result_verification 的正是那条预设），其次本通显式主题。
                        # 两者皆空则 select_task_relevant_evidence 会 fail-closed。
                        task=(
                            self._preset_hint
                            or self._outbound_task(agent_language())
                        ),
                        lang=agent_language(),
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
            self._maybe_learn_playbook(record, transcripts, direction, number, task=learner_task)
        except Exception as exc:  # noqa: BLE001
            logger.exception("通话摘要线程异常: %s", exc)
            try:
                record.set_summary(
                    {"ok": False, "summary": "", "error": "summary_worker_error"}
                )
            except Exception:  # noqa: BLE001
                logger.exception("记录摘要线程失败状态时发生异常")

    def _maybe_learn_playbook(
        self,
        record: CallRecord,
        transcripts: list[tuple[str, str]],
        direction: str,
        number: str | None,
        *,
        task: str,
    ) -> None:
        """呼叫情报库自动回流（WIL-129）：摘要线程尾部顺跑一次 learner。

        从转写提炼"对方索要了什么我们没有的核身信息 / 新的 IVR 流程事实"并
        合并入库（只增不删、隐私硬校验在 call_playbooks 内）。挂在摘要流程上
        意味着 SUMMARY_ENABLED=false 时不回流——P1 接受该耦合。绝不抛出。
        """
        try:
            if direction != "outbound" or not number:
                return
            learned = call_playbooks.learn_from_call(
                number,
                transcripts,
                self._profile_task_package,
                call_id=record.id,
                updated=time.strftime("%Y-%m-%d"),
                task=task,
            )
            if not learned:
                return
            # 只发键名与 label 级信息——情报库本身无值，事件同样保持无值。
            keys = [
                entry.get("key")
                for entry in learned.get("required_info", [])
                if isinstance(entry, dict)
            ]
            requirements = call_playbooks.required_info_status(learned)
            record.log_event(
                "playbook_updated", playbook_id=learned.get("id"), keys=keys,
                requirements=requirements,
            )
            self._publish(
                {
                    "type": "playbook_updated",
                    "call_id": record.id,
                    "playbook_id": learned.get("id"),
                    "keys": keys,
                    "requirements": requirements,
                }
            )
        except Exception:  # noqa: BLE001
            logger.exception("呼叫情报库回流异常（不影响摘要）")

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
            direction=direction,
        )
        registry = tools.register()
        if (
            direction == "inbound"
            and not isinstance(self.modem, BluetoothModem)
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
        if isinstance(self.modem, BluetoothModem):
            if mode != "off":
                logger.info("Bluetooth inbound policy overrides INBOUND_TRIAGE_MODE; triage disabled")
            mode = "off"
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
        if self._takeover_ai_fallback:
            return None
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
                # 放行：先清标志，再把重建后的提示词真正下发给 provider。
                # 只清标志是没用的——会话建立时那份 instructions 已经发出去了，
                # 不中途 session.update，AI 会被限制话术锁住整通电话（#76）。
                self._triage_pending = False
                await self._push_triage_release_instructions(agent)
                continue
            if result.outcome == "clarify":
                if self._triage_clarification_spoken:
                    continue
                self._triage_clarification_spoken = True
                try:
                    await agent.say(_INBOUND_TRIAGE_CLARIFY_TEXT[agent_language()])
                    self._drain_agent_audio(bridge)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "分诊澄清语播放失败: error_type=%s", type(exc).__name__
                    )
                continue
            if result.outcome == "transfer":
                try:
                    endpoint_reason = await asyncio.wait_for(
                        asyncio.to_thread(self._takeover_endpoint_rejection), timeout=1.0
                    )
                except TimeoutError:
                    endpoint_reason = "local_registration_timeout"
                if not self._agent_generation_current(generation):
                    return None
                # Fence normal Realtime output before orchestration. The takeover
                # hold line has its own one-shot generation gate.
                self._triage_terminal = True
                request = self._request_owner_takeover(
                    generation, trigger="triage_judge", endpoint_check=lambda: endpoint_reason
                )
                if request.get("success") is not True:
                    self._triage_terminal = False
                    self._triage_consumer.rollback_terminal()
                    await self._resume_ai_after_takeover(agent, bridge, generation)
                    logger.warning(
                        "分诊转接编排失败: code=%s", request.get("code", "unknown")
                    )
                    if self._takeover_ai_fallback:
                        break
                    continue
                terminal_action = "transfer"
                continue
            if result.outcome == "reject":
                # The fixed line is generated by orchestration, not by Realtime's
                # free-form policy. Fence immediately after its audio is flushed.
                self._clear_outgoing_audio()
                try:
                    await agent.say(_INBOUND_TRIAGE_REJECT_TEXT[agent_language()])
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

    async def _push_triage_release_instructions(self, agent: VoiceAgent) -> None:
        """分诊放行后下发解除限制的提示词。

        provider 不支持中途 session.update 时如实告警——那种部署下 AI 会一直
        停在限制话术，是可观测的降级，而不是静默失效。
        """
        try:
            pushed = await agent.update_session_instructions(
                self._build_agent_instructions("inbound")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "分诊放行后更新提示词失败: error_type=%s", type(exc).__name__
            )
            return
        if pushed:
            logger.info("分诊放行: 已解除限制话术")
        else:
            logger.warning(
                "分诊放行: provider 不支持中途更新提示词，AI 仍受限于分诊话术"
            )

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
            owner_number=config.get_str("OWNER_PHONE"),
            allow_any=config.get_bool("SMS_ALLOW_ANY_TARGET"),
        )

    def _build_agent_instructions(self, direction: str) -> str:
        """会话系统提示词：文本构造在 prompts 模块（纯函数，可独测）。"""
        lang = agent_language()
        scenario = self._take_prompt_scenario() if direction == "outbound" else None
        if direction == "outbound" and call_playbooks.playbooks_enabled():
            # 呼叫情报库（WIL-129）：命中热线的 IVR 流程知识拼进场景——导航
            # 纪律从"每个预设手写"升级为"按热线沉淀共享"。lookup 宽松 never-raises。
            playbook = call_playbooks.lookup_playbook(self.current_caller)
            notes = call_playbooks.ivr_notes_text(playbook, lang)
            if notes:
                scenario = f"{scenario}\n{notes}" if scenario else notes
            hints = call_playbooks.soft_info_text(playbook, lang)
            if hints:
                scenario = f"{scenario}\n{hints}" if scenario else hints
                requirements = [
                    status for status in call_playbooks.required_info_status(playbook)
                    if status["level"] == "soft"
                ]
                fields = {"playbook_id": (playbook or {}).get("id"), "requirements": requirements}
                logger.info("playbook_soft_hints_injected %s", fields)
                if self._record is not None:
                    self._record.log_event("playbook_soft_hints_injected", **fields)
                self._publish({
                    "type": "playbook_soft_hints_injected",
                    "call_id": self._record.id if self._record is not None else None,
                    **fields,
                })
        triage_mode = self._triage_mode if direction == "inbound" else "off"
        if not getattr(self.modem, "supports_sms", True):
            note = (
                "This Bluetooth HFP line cannot send or receive SMS or read verification codes. "
                "Do not wait for SMS; ask the owner via ask_owner when needed."
            )
            scenario = f"{scenario}\n{note}" if scenario else note
        takeover_preference = (
            config.get_str("INBOUND_TAKEOVER_PREFERENCE")
            if direction == "inbound"
            and not isinstance(self.modem, BluetoothModem)
            and config.get_bool("INBOUND_TAKEOVER_ENABLED")
            and triage_mode == "off"
            and not self._takeover_ai_fallback
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
            # 读标志而不是 mode：裁决 continue_ai 后要能真的解除限制话术。
            # 曾写成 triage_mode == "enforce"，于是 _triage_pending 成了只写
            # 标志（4 处写、0 处读），放行裁决完全落空（#76）。
            triage_pending=self._triage_pending if direction == "inbound" else False,
            task_package=(
                self._profile_task_package if direction == "outbound" else None
            ),
        )

    def _opening_instructions(self, direction: str, *, module_greeting: str = "") -> str:
        """开场白指令：文本构造在 prompts 模块（纯函数，可独测）。"""
        lang = agent_language()
        return opening_instructions(
            direction,
            owner_name(lang),
            agent_persona(lang),
            self._outbound_task(lang),
            lang,
            opening=self._prompt_gen_opening if direction == "outbound" else None,
            module_greeting=module_greeting,
        )

    async def _in_call_carrier_sms(
        self, goal: str, generation: int, record: CallRecord | None,
    ) -> list[dict]:
        if self.hub is None:
            return []
        if self._wrap_up_sms_evidence:
            return list(self._wrap_up_sms_evidence)
        seen = self._wrap_up_sms_seen
        candidates = []
        for item in carrier_sms_evidence(
            self.hub.history(), service_number=self._wrap_up_sms_service_number,
            started_at=self._wrap_up_sms_started_at, ended_at=time.time(),
        ):
            key = (float(item["ts"]), str(item["text"]))
            if not math.isfinite(key[0]) or key in seen:
                continue
            # Mark before awaiting: failures/cancellation must never retry this SMS.
            seen.add(key)
            candidates.append(item)
        if not candidates:
            return []
        try:
            selected = await asyncio.to_thread(
                select_task_relevant_evidence, candidates, task=goal, lang=agent_language(),
            )
            code = "relevant" if selected else "not_selected"
            if selected and (len(selected) != 1 or selected[0] not in candidates):
                selected, code = [], "invalid_selection"
        except Exception:  # noqa: BLE001 -- fail closed, never log model/SMS payloads
            selected, code = [], "relevance_error"
        if (
            not self._agent_generation_current(generation) or self._record is not record
            or seen is not self._wrap_up_sms_seen or not self._wrap_up_carrier_sms_enabled
        ):
            return []
        for item in candidates:
            if record is not None:
                try:
                    record.log_event(
                        "in_call_sms_evidence", relevant=item in selected,
                        reason_code=code if item in selected or not selected else "not_selected",
                        chars=len(str(item["text"])),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("通话短信证据事件未记录: error_type=%s", type(exc).__name__)
        self._wrap_up_sms_evidence = list(selected)
        return list(selected)

    async def _stop_wrap_up_sms_judge(self) -> None:
        self._wrap_up_carrier_sms_enabled = False
        self._wrap_up_sms_seen = set()
        self._wrap_up_sms_evidence = []
        task = self._judge_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run_wrap_up_judge(
        self, transcripts: list[tuple[str, str]], goal: str
    ) -> None:
        """后台跑一次收尾裁判；判 wrap_up 就置标志，主循环据此说告别并挂断。

        judge_wrap_up 是同步的（内部带超时），放线程池跑，绝不阻塞音频主循环。
        """
        sms_enabled = self._wrap_up_carrier_sms_enabled
        generation = self._session_generation
        record = self._record
        evidence: list[dict] = []
        try:
            if sms_enabled:
                if not self._agent_generation_current(generation):
                    return
                evidence = await self._in_call_carrier_sms(goal, generation, record)
                if (
                    not self._agent_generation_current(generation) or self._record is not record
                    or not self._wrap_up_carrier_sms_enabled
                ):
                    return
            if evidence:
                result = await asyncio.to_thread(judge_wrap_up, transcripts, goal, carrier_sms=evidence)
                # Model-authored reasons can echo an SMS; keep bodies out of all events/logs.
                result = {**result, "reason": "carrier_sms_evidence"}
            else:
                result = await asyncio.to_thread(judge_wrap_up, transcripts, goal)
        except Exception as exc:  # noqa: BLE001
            if sms_enabled:
                logger.debug("收尾裁判调度异常: error_type=%s", type(exc).__name__)
            else:
                logger.debug("收尾裁判调度异常: %s", exc)
            return
        if sms_enabled and (
            not self._agent_generation_current(generation) or self._record is not record
            or not self._wrap_up_carrier_sms_enabled
        ):
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
        decision = result.get("decision")
        # hold 转换（WIL-120 二期）：on_hold 进入；裁判成功返回其他判定即退出
        # （失败结果不动状态——网络抖动不该把排队踢回普通计时）。
        if decision == "on_hold" and not self._on_hold:
            self._on_hold = True
            self._hold_started_ts = time.time()
            if record is not None:
                record.log_event("hold_started")
            self._publish({"type": "hold", "active": True})
            logger.info("收尾裁判判定进入排队等待（%s）", result.get("reason", ""))
        elif (
            self._on_hold
            and result.get("ok")
            and decision in ("continue", "wrap_up")
        ):
            held = max(0.0, time.time() - self._hold_started_ts)
            self._on_hold = False
            if record is not None:
                record.log_event("hold_ended", seconds=round(held, 1))
            self._publish({"type": "hold", "active": False})
            logger.info("排队等待结束（%.0fs），恢复正常判定", held)
        if decision == "wrap_up":
            self._wrap_up_requested = True
            self._wrap_up_reason = result.get("reason", "")

    def _request_repeat_stuck_wrap_up(self, reason: str) -> None:
        if self._on_hold:
            # 排队等待中的「重复」是等待音循环，不是会话卡死（WIL-120 二期）。
            logger.info("复读抑制在排队等待期触发，忽略: %s", reason)
            return
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
            if result.get("result_verification") == "carrier_sms" and getattr(self.modem, "supports_sms", True)
            else "none"
        )
        # 长通话覆盖（WIL-120 一期）：仅 profile 来源提供；动态生成结果无此键。
        max_seconds = result.get("max_call_seconds")
        self._profile_max_call_seconds = (
            max_seconds
            if isinstance(max_seconds, int) and not isinstance(max_seconds, bool)
            else None
        )
        self._profile_wrap_up_judge = result.get("wrap_up_judge") is not False
        package = result.get("task_package")
        self._profile_task_package = package if isinstance(package, dict) else None
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
                # 护窗是 monotonic 时刻：背靠背通话可能落在上一通的护窗内，
                # 那会让新通话开场白被静音，必须随每通重置。
                self._dtmf_guard_until = 0.0
                self._dtmf_guard_dropped_bytes = 0
                # 不重置会跨通话串味：上一通挂起的按键会跟下一通第一句对端话语
                # 配对，把上一位来电者的话写进这一通的记录（Codex 评审 P1，
                # 属隐私泄漏，不只是脏数据）。
                with self._outcome_lock:
                    self._pending_dtmf_outcome = None
                    self._last_remote_utterance = ""

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

            # 按键护窗（#45 候选方向 2「信号层」）：真机 2026-08-03 拨 10086，
            # 模型一边说「这边先按一下对应的键」一边按，4 次本机 success、IVR
            # 菜单一次没推进。双音与 Agent 语音共用 _outgoing_audio 这条上行，
            # 混叠后对端两者都识别不出。护窗期间丢弃 Agent 下行，让双音独占上行。
            #
            # 护窗必须**先装再动手**：qvts 的 send_dtmf 是阻塞 AT 往返，inband 的
            # 入队到实际送出也隔着一次 drain——若等发完才装，realtime 在这段空档
            # 产出的语音会正好落进按键窗口。
            guard_seconds = max(0.0, config.get_int("DTMF_GUARD_MS") / 1000.0)
            previous_guard = self._dtmf_guard_until
            self._dtmf_guard_until = time.monotonic() + guard_seconds

            ok = True
            sent = False
            tone_seconds = 0.0
            if mode in {"inband", "both"}:
                tone = dtmf_tone(
                    digits,
                    MODEM_RATE,
                    tone_ms=config.get_int("DTMF_TONE_MS"),
                    amplitude=config.get_float("DTMF_TONE_AMPLITUDE"),
                )
                if not tone:
                    self._dtmf_guard_until = previous_guard
                    return False, mode
                # 清空 + 入队要与 _drain_agent_audio 的「取空再写桥」互斥：drain
                # 已把语音取进本地 chunks 时，单清队列是空操作，那段语音照样写进
                # 桥、排在双音前面。
                with self._media_lock:
                    self._clear_outgoing_audio()
                    # 与 Agent 语音共用 _outgoing_audio，后续由 _drain_agent_audio
                    # 按既有下行链路送入桥；半双工 pending 判定也会自然把它当成正在说话。
                    if self._record is not None:
                        self._record.write_downlink(tone)
                    self._outgoing_audio.put(tone)
                # 双音本身的时长要计入护窗，否则语音会紧跟在双音尾巴上。
                tone_seconds = len(tone) / 2 / MODEM_RATE
                sent = True
            if mode in {"qvts", "both"}:
                # qvts 走 AT 带外，双音不进上行队列；但 AI 在按键瞬间继续说话，
                # 同样会占住 IVR 的按键识别窗口——两种 mode 都要护窗。
                if mode == "qvts":
                    with self._media_lock:
                        self._clear_outgoing_audio()
                ok = self.modem.send_dtmf(digits)
                sent = sent or ok
            if sent:
                # 发完再从「此刻」重算：AT 往返耗时不该吃掉护窗。只延不缩，
                # 免得连按时后一次把前一次的护窗改短。
                self._dtmf_guard_until = max(
                    self._dtmf_guard_until,
                    time.monotonic() + tone_seconds + guard_seconds,
                )
                # 推进校验的挂起点必须在这里，不能放在 _record_dtmf_action 里：
                # 那个方法在 DTMF_JUDGE_MODE=off（**默认值**）时会提前 return，
                # 于是本事件在生产配置下**一次也不会产生**（Codex 评审 P1）。
                self._arm_dtmf_outcome()
                self._recent_dtmf_sent[digits] = (time.monotonic(), source)
                self._record_dtmf_action(digits, source)
                if source != "spoken_followup":
                    self._cancel_pending_dtmf_locked(digits)
            else:
                # 没发出去就不该白白哑掉 Agent：预装的护窗要还原。
                self._dtmf_guard_until = previous_guard
            return ok, mode

    def _handle_peer_barge_in(self, bridge: AudioBridge | None) -> bool:
        """对端开口（provider VAD speech_started）→ 让 AI 让路；返回是否接受打断。

        返回 False（provider 侧据此**不发** ``response.cancel``）的两种情形：

        - **DTMF 护窗期内（WIL-94 坑 1）**：inband 双音与 Agent 语音共用
          ``_outgoing_audio``，IVR 提示音触发的「对端开口」若在此时清队列，
          会把排队中的双音打断——WIL-49 修好的 IVR 导航就回归了。护窗在
          ``_send_dtmf_raw`` 里**先装再入队**，所以这里先查护窗再动手是安全的。
        - **自激兜底已触发（WIL-94 坑 4）**：本通已退回半双工，后续事件全拒。

        自激指纹：回复首块音频到达后 ``_BARGE_IN_ECHO_WINDOW_SECONDS`` 内就被
        「打断」，连续 ``_BARGE_IN_ECHO_STRIKES`` 次。真人抢话通常更晚，且出现
        一次较晚的打断即清零计数。误判的代价是本通退回半双工（即今天的默认
        行为），远小于放任自激的代价（通话自循环）。
        """
        now = time.monotonic()
        if not self._barge_in:
            return False
        if now < self._dtmf_guard_until:
            logger.info("护窗期内忽略对端开口：barge-in 让位，双音优先（WIL-49）")
            return False
        pending = (
            bridge.pending_output_bytes()
            if bridge is not None and hasattr(bridge, "pending_output_bytes")
            else 0
        )
        agent_in_flight = pending > 0 or not self._outgoing_audio.empty()
        if agent_in_flight and self._turn_audio_started_at > 0:
            if now - self._turn_audio_started_at <= _BARGE_IN_ECHO_WINDOW_SECONDS:
                self._barge_in_echo_strikes += 1
                if self._barge_in_echo_strikes >= _BARGE_IN_ECHO_STRIKES:
                    self._barge_in = False
                    logger.warning(
                        "疑似自激：连续 %d 次回复出声 %.1fs 内即被「打断」，"
                        "本通退回半双工（WIL-94 兜底）",
                        self._barge_in_echo_strikes,
                        _BARGE_IN_ECHO_WINDOW_SECONDS,
                    )
                    record = self._record
                    if record is not None:
                        record.log_event(
                            "barge_in_fallback",
                            strikes=self._barge_in_echo_strikes,
                            window_s=_BARGE_IN_ECHO_WINDOW_SECONDS,
                        )
                    return False
            else:
                self._barge_in_echo_strikes = 0
        return self._discard_agent_playback(bridge, "对端开口打断 AI")

    def _log_winddown(self, record: CallRecord | None, reason: str) -> None:
        """收尾起因落盘（WIL-95 第二期）：termination_kind 的证据之一。

        只在「AI 主动收尾」的两个路径打（外呼硬时限 / 收尾裁判）；hangup 工具、
        接管、对端先挂各有既有事件，metrics 层统一归类。埋点失败不影响通话。
        """
        if record is None:
            return
        try:
            record.log_event("winddown", reason=reason)
        except Exception:  # noqa: BLE001
            pass

    def _discard_agent_playback(self, bridge: AudioBridge | None, reason: str) -> bool:
        """清掉 AI 未播出的下行积压（桥内 + 队列）；护窗期拒绝，双音优先。

        barge-in 让路与复读晚截（ResponseAudioGate ``on_late_cut``）共用的
        清积压动作。护窗语义对两者一致：DTMF 期间谁都不许清队列。
        """
        if time.monotonic() < self._dtmf_guard_until:
            logger.info("护窗期内拒绝清积压（%s）：双音优先（WIL-49）", reason)
            return False
        with self._media_lock:
            # 双检护窗：_send_dtmf_raw 是「先装护窗、再持本锁清队+入队双音」。
            # 首查护窗时它可能还没装；等拿到锁，若双音已入队则护窗必已装上——
            # 锁内重查一次，堵住「首查未装、动手时双音在队」的毫秒级竞态。
            if time.monotonic() < self._dtmf_guard_until:
                logger.info("护窗在清积压途中装上（%s）：让位，双音优先", reason)
                return False
            dropped = 0
            if bridge is not None and hasattr(bridge, "discard_pending_output"):
                dropped = bridge.discard_pending_output()
            cleared = 0
            try:
                while True:
                    self._outgoing_audio.get_nowait()
                    cleared += 1
            except Empty:
                pass
        if dropped or cleared:
            logger.info(
                "%s：丢弃未播积压 %d 字节 / 队列 %d 块", reason, dropped, cleared
            )
        return True

    # 一轮之内 provider 是突发写音频的（分块几乎连着到），轮次之间才有真空档。
    # 用这个空档判定「新的一轮开始了」，就不必依赖各 provider 的 response 事件。
    _TURN_GAP_SECONDS = 1.5

    def _dead_media_expired(self, pcm_8k: bytes, now: float) -> bool:
        """上行还在出帧、内容却是**纯数字静音** → 对端早已挂断（WIL-100）。

        为什么需要这条，而不是靠已有的两道保护：

        - ``NO CARRIER`` / ``+CEND:``：串口抖动时会被吞掉（`modem.py:225`
          记的 2026-07-08 事故就是这个）。
        - CLCC 消失判定（`modem.py:_process_clcc_response`）：前提是模组自己
          知道通话没了。2026-08-06 19:09 那通，两道都没触发——按 CLCC 失败
          路径最多 60 秒就会收尾，实际僵尸了十分钟，**只能推断模组一直报着
          有活跃通话**，即模组自己也没察觉对端挂断。

        此时唯一还能判的就是媒体本身。三种状态的签名互不相同：

        ==========================  ========  ==========
        状态                        frames    peak
        ==========================  ========  ==========
        通话中，AI 在说             0（半双工屏蔽）  0
        通话中，对方在说            >0        439~591
        僵尸（对端已走）            >0        **0**
        ==========================  ========  ==========

        真实电话音频始终带底噪（实测 RMS 45~56），**出帧却精确为 0** 不是真
        音频。所以判据是「有帧 + 峰值恒为 0」持续足够久，而不是「安静」。

        阈值取得保守：误挂断比晚挂断严重得多。
        """
        if self._dead_media_seconds <= 0:
            return False
        if not pcm_8k:
            # 没帧不代表对端走了——半双工屏蔽期间本来就没帧，不能据此计时。
            return False
        if any(pcm_8k):
            # 任意非零字节即视为有真实音频（含底噪），重新计时。
            self._dead_media_silent_seconds = 0.0
            return False
        # 累计的是**收到的静音音频时长**，不是墙上时钟（2026-08-06 Codex 评审 P1）。
        # 按墙上时钟算的话，「静音 50 秒 + 半双工屏蔽无帧 20 秒 + 一块静音」就会
        # 立刻越过 60 秒阈值——而实际只收到 50 秒静音，与注释说的语义自相矛盾。
        self._dead_media_silent_seconds += len(pcm_8k) / (MODEM_RATE * 2)
        return self._dead_media_silent_seconds >= self._dead_media_seconds

    def _check_turn_length(
        self, pcm_agent: bytes, agent: VoiceAgent, record: CallRecord | None
    ) -> bool:
        """单轮长度闸门（WIL-90 / WIL-85 N2）。返回 True 表示**这块下行要丢掉**。

        为什么必须有这个闸门，而不能只在提示词里写「说短点」：WIL-83 已实测
        证明模型**不可靠地遵守**提示词里的禁令——受限话术明令禁止「说会转告」，
        AI 照说不误。判官范式：确定性策略要放在模型之外。

        分工：提示词负责把中位数压下来，这里只兜住跑飞的长尾
        （WIL-89 基线：中位 6.8s、p90 17.8s、最长 36.6s；默认阈值 20s）。

        **截断靠丢弃下行，不靠 provider 停止生成。** 2026-08-06 真机验证推翻了
        原设计：OpenAI 把整轮音频以突发写完的速度快过我们累计到阈值，等闸门
        触发时响应早已结束，response.cancel 直接返回 response_cancel_not_active，
        队列照常放完——对端把 14.4s 和 29.6s 的两轮整段听完了，而事件流里却记着
        turn_length_capped。所以限制必须落在「送不送进下行队列」这一步。

        代价要说清：这是**硬切**，被截的那一轮结尾会突兀。默认阈值取在实测
        p90 之上就是为了让它只在明显跑飞时才触发。
        """
        if not pcm_agent:
            return False
        # 轮次边界检测不属于闸门：排队/端到端埋点的「每轮一次」标志也靠它复位，
        # 闸门关闭（max_turn_seconds<=0）时同样要走到（2026-08-12 教训：早退在
        # 这之前会让埋点在首轮之后永久哑掉）。
        now = time.monotonic()
        with self._turn_lock:
            if now - self._turn_last_chunk_at > self._TURN_GAP_SECONDS:
                # 空档 → 新的一轮，计数与「已超限」标志一起重置。
                self._turn_audio_bytes = 0
                self._turn_cancel_sent = False
                self._turn_first_audio_logged = False
                self._turn_trim_budget = _TURN_TRIM_CAP_BYTES
                self._turn_trimmed_bytes = 0
            self._turn_last_chunk_at = now
        rate = getattr(agent, "output_rate", 0) or self._agent_output_rate
        if self._max_turn_seconds <= 0 or rate <= 0:
            return False
        with self._turn_lock:
            if self._turn_cancel_sent:
                return True  # 本轮已超限，后续分块一律丢弃
            self._turn_audio_bytes += len(pcm_agent)
            seconds = self._turn_audio_bytes / (rate * 2)
            if seconds <= self._max_turn_seconds:
                return False
            self._turn_cancel_sent = True

        # 闸门本身**已经生效**（调用方会丢掉这块及之后的下行）。下面再顺手请
        # provider 停止生成，纯属省 token / 省时延——**不是**限制生效的手段。
        #
        # 2026-08-06 真机验证：靠 response.cancel 掐生成完全无效，OpenAI 早在
        # 我们累计到阈值之前就把整轮生成完了，回的是 response_cancel_not_active。
        # 所以这里不再因为它失败而告警——那是预期内的常态，不是故障。
        logger.info("单轮已达 %.1fs 上限，截断本轮剩余下行", self._max_turn_seconds)
        if record is not None:
            record.log_event(
                "turn_length_capped",
                seconds=round(seconds, 1),
                limit=self._max_turn_seconds,
            )
        loop = self._loop
        if loop is not None and loop.is_running():
            with contextlib.suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(self._cancel_agent_turn(agent), loop)
        return True

    async def _cancel_agent_turn(self, agent: VoiceAgent) -> None:
        """把打断真正下发给 provider，并记录它到底有没有生效。

        返回值必须落日志：豆包没有实现这条能力（WIL-75 的形态），
        不区分「已下发」与「不支持」就又是一次静默失效。
        """
        try:
            delivered = await agent.cancel_response()
        except Exception as exc:  # noqa: BLE001
            logger.warning("打断本轮回复异常: error_type=%s", type(exc).__name__)
            return
        if not delivered:
            # 只是没省下这点生成而已；截断由调用方丢弃下行完成，不受影响。
            logger.debug("本 provider 未接受打断请求（不影响截断生效）")

    def _flush_dtmf_guard_drop(self, record: CallRecord | None, rate: int) -> None:
        """护窗结束后落一条「丢了多少 Agent 语音」的事件。

        录音只记实际发出的内容（刻意如此），但**文字 transcript 不受护窗影响**：
        模型「说」的那句话仍会完整进 transcript，哪怕它一个字节都没发出去。
        事后复盘会看到转写与录音对不上，误判成丢包或录音坏了——这条事件让这个
        差异在记录里自解释（#81）。
        """
        with self._guard_drop_lock:
            dropped = self._dtmf_guard_dropped_bytes
            self._dtmf_guard_dropped_bytes = 0
        if dropped <= 0 or record is None:
            return
        try:
            record.log_event(
                "agent_audio_dropped",
                reason="dtmf_guard",
                bytes=dropped,
                duration_ms=round(dropped / 2 / max(1, rate) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - 记账失败不该影响通话
            logger.warning(
                "记录护窗丢弃事件失败: error_type=%s", type(exc).__name__
            )

    def _arm_dtmf_outcome(self) -> None:
        """按键发出后挂起一条待配对的推进证据。"""
        with self._outcome_lock:
            self._pending_dtmf_outcome = {
                "action_id": "",
                "pressed_at": time.monotonic(),
                "menu_before": self._last_remote_utterance[:_OUTCOME_TEXT_CHARS],
            }

    def _emit_dtmf_outcome(
        self, record: CallRecord, pending: dict, text: str, *, status: str
    ) -> None:
        """落一条按键推进证据。``status`` 三态，**不是布尔**（WIL-97）。

        - ``observed``：窗口内拿到了对端下一句，证据有效。
        - ``unobserved``：窗口内对端没说话。**这不等于按键没生效**——只是我们
          停止观察了。2026-08-06 拨 10086：按 1 之后菜单确实推进了，对端 9207ms
          才应答，而窗口是 8000ms，于是被记成 expired。一个用来纠正假阳性的
          机制，自己产生了假阴性。
        - ``late``：超窗之后才等到对端说话。专门用来攒「窗口到底该多长」的数据，
          而不是拍脑袋改阈值。
        """
        try:
            record.log_event(
                "dtmf_outcome",
                action_id=pending["action_id"],
                menu_before=pending["menu_before"],
                remote_after=text[:_OUTCOME_TEXT_CHARS],
                latency_ms=round((time.monotonic() - pending["pressed_at"]) * 1000),
                status=status,
                window_ms=round(_OUTCOME_WINDOW_SECONDS * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - 记账失败不该影响通话
            logger.warning(
                "记录按键推进证据失败: error_type=%s", type(exc).__name__
            )

    def _expire_dtmf_outcome(self, record: CallRecord | None) -> None:
        """按键后对端始终没开口——把「沉默」本身记下来（WIL-101）。

        沉默是有意义的证据：IVR 不理会一次按键时，往往就是什么都不说。若只在
        「有下一句」时才落事件，这类失败会整个消失，而它恰恰是最该被找出来的。

        **判死用的是宽限期而不是证据窗口**，这一点很关键（WIL-97 + WIL-101）：
        按证据窗口（8s）判死的话，真机那例 9207ms 的应答会在对端开口之前就被
        记成「沉默」，正好把 WIL-97 刚修好的假阴性又造回来。所以这里等到宽限期
        （30s）——窗口只用来区分 observed / late，宽限期才是「不再等了」的界线。

        由此也保住了「一次按键只落一条事件」：宽限期内开口走
        ``_settle_dtmf_outcome``，超过宽限期才由这里落 unobserved，两者互斥。
        """
        with self._outcome_lock:
            pending = self._pending_dtmf_outcome
            if pending is None or (
                time.monotonic() - pending["pressed_at"] < _OUTCOME_LATE_GRACE_SECONDS
            ):
                return
            self._pending_dtmf_outcome = None
        if record is not None:
            self._emit_dtmf_outcome(record, pending, "", status="unobserved")

    def _settle_dtmf_outcome(self, record: CallRecord | None, text: str) -> None:
        """把「按键 → 对端下一句」配成一条 dtmf_outcome 事件。

        为什么要它（#50 第 4 项）：本机 `result: success` 已被反复证明是**假阳**
        ——2026-08-03 拨 10086，4 次按键全 success，IVR 菜单一次没推进。判断按键
        有没有生效，唯一可靠的依据是**对端接下来说了什么**。

        本事件只**记录证据**，不下判断。这是刻意的，来自 WIL-82 手工判读的三次
        踩坑：

        1. **单位**：必须按按键记，不能按通话记 —— 同一通里不同按键结果可以不同；
        2. **因果**：推进必须发生在按键**之后**。10086 菜单超时会自动转接，把按键
           前的转接算作推进就是把 IVR 的自动行为记到按键头上（实测差 18 秒）；
        3. **不写关键词表**：ASR 会输出繁体（「正在**為**您**轉**回」），简体关键词
           表直接漏判。这既是非枚举硬原则，也是实测教训。

        所以这里只如实记录 前一句 / 后一句 / 时延，判读留给离线分析或判官。
        """
        with self._outcome_lock:
            pending = self._pending_dtmf_outcome
            self._last_remote_utterance = text
            if pending is None:
                return
            self._pending_dtmf_outcome = None
            elapsed = time.monotonic() - pending["pressed_at"]
        if record is None:
            return
        if elapsed >= _OUTCOME_LATE_GRACE_SECONDS:
            # 几分钟后的无关话语不能配对——被无视的那次按键会跟它凑成假证据
            # （Codex 评审 P1，原有保护）。这种情况下只如实记「没观察到」。
            self._emit_dtmf_outcome(record, pending, "", status="unobserved")
            return
        # 窗口内 = observed；超窗但仍在宽限期内 = late。
        #
        # late 是本次修的核心（WIL-97）：真机 2026-08-06 拨 10086，按 1 之后菜单
        # **确实推进**了（对端「回廣東一零零八六,請稍候」），只是 9207ms 才应答，
        # 而窗口是 8000ms。原实现在这里把文本丢掉、记成超时，于是一个用来纠正
        # 假阳性的机制自己产出了假阴性，会让按键有效率被系统性低估。
        #
        # 现在保留对端原话与真实时延——这正是「窗口该开多长」所需的样本，
        # 而不是拍脑袋把 8 秒改成 15 秒（本票明确要求先扩样本）。
        status = "observed" if elapsed < _OUTCOME_WINDOW_SECONDS else "late"
        self._emit_dtmf_outcome(record, pending, text, status=status)

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
        # 影子判官开着时，把它的 action_id 补进待配对记录，便于两边对账。
        with self._outcome_lock:
            if self._pending_dtmf_outcome is not None:
                self._pending_dtmf_outcome["action_id"] = entry.action_id

    def _resolve_dtmf_mode(self) -> str:
        if isinstance(self.modem, BluetoothModem):
            return "qvts"  # Existing out-of-band dispatch delegates to oFono SendTones.
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
            if self._takeover_resuming:
                return False
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
            self._takeover_hold_audio_at = None
            self._takeover_attempted = False
            self._takeover_ai_fallback = False
            self._takeover_resume_pending = False
            self._takeover_ai_fallback_notified = False
            self._takeover_resuming = False
            self._takeover_started_at = None
            self._takeover_deadline = None
            if direction != "inbound" or isinstance(self.modem, BluetoothModem):
                if direction == "inbound" and isinstance(self.modem, BluetoothModem):
                    logger.info("Bluetooth App takeover disabled; owner can select the phone earpiece")
                self._takeover_coordinator = None
                return
            router = _CallSessionMediaRouter()
            self._takeover_coordinator = InboundTakeoverCoordinator(
                call_id=f"call_{secrets.token_urlsafe(18)}",
                generation=self._session_generation,
                media_router=router,
            )

    @staticmethod
    def _takeover_endpoint_rejection() -> str | None:
        """Local hosted enrollment is a possibility signal, not phone presence."""
        if not config.get_bool("REMOTE_WEB_DIALER_ENABLED") or not config.get_bool("REMOTE_CLOUD_ENABLED"):
            return "hosted_takeover_disabled"
        result: Queue[str | None] = Queue(maxsize=1)

        def read_registration() -> None:
            try:
                reason = None if CloudCredentialStore().load() is not None else "edge_not_registered"
            except Exception:  # keyring failures must not strand a provider tool call
                reason = "local_registration_unavailable"
            result.put_nowait(reason)

        threading.Thread(target=read_registration, name="takeover-registration", daemon=True).start()
        try:
            return result.get(timeout=1.0)
        except Empty:
            return "local_registration_timeout"

    async def _resume_ai_after_takeover(
        self, agent: VoiceAgent, bridge: AudioBridge, generation: int
    ) -> None:
        with self._takeover_lock:
            if not self._takeover_resume_pending or not self._agent_generation_current(generation):
                return
            self._takeover_resume_pending = False
            self._takeover_ai_fallback_notified = True
            self._takeover_resuming = True
            self._triage_terminal = False
            self._triage_pending = False
        self._stop_triage_judge(join_timeout=0.0)
        try:
            try:
                await asyncio.wait_for(agent.cancel_response(), timeout=1.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("接管降级取消旧回复失败: error_type=%s", type(exc).__name__)
            if not self._agent_generation_current(generation):
                return
            self._clear_outgoing_audio()
            if hasattr(bridge, "discard_pending_output"):
                bridge.discard_pending_output()
            await asyncio.wait_for(self._push_triage_release_instructions(agent), timeout=1.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("接管降级更新失败: error_type=%s", type(exc).__name__)
        finally:
            with self._takeover_lock:
                if generation == self._session_generation:
                    self._takeover_resuming = False
        if not self._agent_effect_allowed(generation):
            return
        instructions = (
            "The owner is currently unable to take this call and you remain responsible for it. "
            "Use the conversation context to explain this naturally, continue helping the caller, "
            "and understand and record their purpose and any message for the owner. "
            "Do not promise another transfer."
            if agent_language() == "en" else
            "机主当前无法接听，这通来电仍由你负责。请结合对话上下文自然说明现状，"
            "继续帮助来电者，了解并记录来意以及需要转告机主的信息，不要承诺再次转接。"
        )
        try:
            await asyncio.wait_for(agent.say(instructions), timeout=3.0)
            self._drain_agent_audio(bridge)
        except Exception as exc:  # noqa: BLE001
            logger.warning("接管降级指令发送失败: error_type=%s", type(exc).__name__)

    def _expire_takeover(self, *, generation: int, now: float | None = None) -> None:
        observed = time.monotonic() if now is None else now
        with self._takeover_lock:
            deadline = self._takeover_deadline
            if deadline is not None and observed >= deadline:
                self._finish_takeover_precommit("OWNER_TIMEOUT", generation=generation, now=observed)

    def _finish_takeover_precommit(
        self, reason: str, *, generation: int, now: float | None = None
    ) -> None:
        """One finalizer for timeout and all other failures before mobile commit."""
        with self._takeover_lock:
            coordinator, request = self._takeover_coordinator, self._takeover_request
            if (
                coordinator is None or request is None
                or generation != request.generation
                or not self._agent_generation_current(generation)
            ):
                return
            result = coordinator.rollback_precommit(reason.lower())
            if not result.accepted:
                return
            observed = time.monotonic() if now is None else now
            started = self._takeover_started_at if self._takeover_started_at is not None else observed
            elapsed_ms = round(max(0.0, observed - started) * 1000, 1)
            self._takeover_request = None
            self._takeover_deadline = None
            self._takeover_started_at = None
            self._takeover_hold_generation = None
            self._takeover_hold_done = True
            self._takeover_ai_fallback = True
            self._takeover_resume_pending = True
            self._triage_terminal = False
            self._triage_pending = False
            self._clear_outgoing_audio()
            for queue in (self._takeover_offer_queue, self._takeover_session_queue):
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except Empty:
                        break
            try:
                self._takeover_revoke_queue.put_nowait(InboundTakeoverRevoke(
                    offer_id=request.offer_id, call_id=request.call_id, reason=reason,
                ))
            except Full:
                pass
            if self._record is not None:
                self._record.log_event(
                    "takeover_timeout_reclaimed" if reason == "OWNER_TIMEOUT" else "takeover_rollback",
                    reason=reason, elapsed_ms=elapsed_ms, generation=generation,
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
        endpoint_check: Callable[[], str | None] | None = None,
    ) -> dict:
        if isinstance(self.modem, BluetoothModem):
            return {"success": False, "code": "TAKEOVER_UNSUPPORTED",
                    "message": "蓝牙线路不支持 App 接管，请在手机上切回听筒"}
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

        # Keychain/Credential Manager can block; never hold the media fence lock during I/O.
        endpoint_reason = (endpoint_check or self._takeover_endpoint_rejection)()
        with self._takeover_lock:
            if not self._agent_generation_current(generation):
                return {
                    "success": False, "code": "STALE_AGENT_GENERATION",
                    "message": "当前 Agent 会话已失效",
                }
            coordinator = self._takeover_coordinator
            if coordinator is None or coordinator.state is not TakeoverState.AI_ACTIVE or self._takeover_resuming:
                return {
                    "success": False,
                    "code": "TAKEOVER_NOT_AI_ACTIVE",
                    "message": "当前通话不允许重复请求真人接管",
                }
            reason = "attempt_already_used" if self._takeover_attempted else endpoint_reason
            if reason is not None:
                self._takeover_ai_fallback = True
                self._takeover_resume_pending = not self._takeover_ai_fallback_notified
                code = "TAKEOVER_ATTEMPT_EXHAUSTED" if self._takeover_attempted else "NO_TAKEOVER_ENDPOINT"
                if self._record is not None:
                    self._record.log_event("takeover_unavailable", reason=reason, code=code, trigger=trigger)
                return {
                    "success": False, "code": code,
                    "message": "机主当前无法接听。继续帮助来电者，了解并记录来意及需转告的信息，不再承诺转接。",
                }
            result = coordinator.begin_takeover()
            if not result.accepted:
                return {
                    "success": False,
                    "code": str(result.code or TakeoverRejection.INVALID_STATE),
                    "message": "当前通话状态不允许真人接管",
                }
            created_at = time.time()
            timeout = config.get_float("INBOUND_TAKEOVER_TIMEOUT_SECONDS")
            if not math.isfinite(timeout) or timeout <= 0:
                timeout = 20.0
            request = InboundTakeoverOfferRequest(
                offer_id=f"offer_{secrets.token_urlsafe(18)}",
                nonce=secrets.token_urlsafe(24),
                call_id=coordinator.call_id,
                generation=coordinator.generation,
                created_at=created_at,
                expires_at=created_at + timeout,
            )
            self._takeover_request = request
            self._takeover_hold_generation = generation
            self._takeover_hold_done = False
            self._takeover_hold_audio_at = None
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
            self._takeover_attempted = True
            self._takeover_ai_fallback = False
            self._takeover_resume_pending = False
            self._takeover_ai_fallback_notified = False
            self._takeover_started_at = time.monotonic()
            self._takeover_deadline = self._takeover_started_at + timeout

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
            if time.time() >= request.expires_at or (
                self._takeover_deadline is not None and time.monotonic() >= self._takeover_deadline
            ):
                self._finish_takeover_precommit("OWNER_TIMEOUT", generation=request.generation)
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

    async def _stop_agent_resources(self, agent, bridge: AudioBridge | None, *, threaded: bool = False) -> None:
        if isinstance(self.modem, BluetoothModem):
            self._bluetooth_cleanup_started = True
        if agent is not None:
            try:
                if self._parallel_agent_connect:
                    await self._cleanup_step("agent_stop", agent.stop())
                else:
                    await agent.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 Agent 出错: %s", exc)
        if bridge is not None:
            try:
                if threaded:
                    await self._cleanup_step("bridge_stop", self._threaded_modem_call(bridge.stop))
                else:
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

    async def _shutdown_agent(self, agent, bridge: AudioBridge | None, *, threaded: bool = False) -> None:
        await self._stop_agent_resources(agent, bridge, threaded=threaded)
        # 会话结束（含异常/端口冲突）时确保挂断物理通话，避免线路悬空。
        try:
            if threaded:
                await self._cleanup_step("modem_hangup", self._threaded_modem_call(self.modem.hangup))
            else:
                self.modem.hangup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("挂断物理通话出错: %s", exc)
        logger.info("通话 Agent 会话已结束")
        self._publish(
            {"type": "call", "status": "ended", "caller": self.current_caller}
        )

    def _drain_agent_audio(self, bridge: AudioBridge) -> None:
        with self._media_lock:
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
        modem: SerialModem | None = None,
        call_logger: CallLogger | None = None,
        sms_email_forwarder: SmsEmailForwarder | None = None,
        vendor: str = "quectel",
    ) -> None:
        # modem/call_logger 参数供测试注入；默认按串口/环境配置自建。
        self.modem = modem or create_modem(modem_port, baudrate, vendor)
        self.audio_keyword = audio_keyword
        self.provider = provider
        self.audio_mode = audio_mode
        self.pcm_port = pcm_port
        self.pcm_baudrate = pcm_baudrate
        self.tx_gain = tx_gain
        self.hub = hub
        self._ring_lock = threading.Lock()
        self._bluetooth_incoming_records: dict[str, CallRecord] = {}
        self._remote_setup_lock = threading.Lock()
        self._remote_worker: RemoteDialerWorker | None = None
        self._remote_invite: RemoteDialerInvite | None = None
        self._remote_session_device_id: str | None = None
        self._remote_call_owner: RemoteWebDialerCoordinator | None = None
        # 模组连接状态与后台 supervisor：首启时模组不在也不阻塞 Web，
        # supervisor 反复重连直到成功（首次连上后由 modem 读循环自愈接管）。
        # 注入 modem（测试/直连）视为已就绪；自建的由 supervisor 连上后置 True。
        self.modem_connected = modem is not None
        self._reported_modem_resetting = False
        self._modem_state_lock = threading.Lock()
        self._modem_disconnected_monotonic: float | None = None
        self._modem_disconnected_at: str | None = None
        self._service_running = False
        self._supervisor_thread: threading.Thread | None = None
        self._sim7600_reset: Sim7600CallReset | None = None
        self._reset_queue_pending = False
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
        if isinstance(self.modem, Sim7600Modem) and config.get_bool("MODEM_RESET_AFTER_CALL"):
            self._sim7600_reset = Sim7600CallReset(
                self.modem, audio_mode, pcm_port, self._set_modem_connected,
            )

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
        if self._sim7600_reset is not None:
            with self._ring_lock:
                if self.modem.recovery_in_progress or not self.modem_connected:
                    self._reset_queue_pending = True
                    return
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
            if isinstance(self.modem, BluetoothModem):
                if not self.modem.inbound_ready:
                    return
                with self._ring_lock:
                    if self.session.is_busy or self._remote_call_owner is not None:
                        self.modem.skip_inbound("session_busy")
                        return
                    missing, _ = self._reject_if_credentials_missing()
                    if missing:
                        self.modem.skip_inbound("credentials_missing")
                        return
                    path = self.modem.claim_inbound()
                    if not path:
                        return
                    self.session.current_caller = caller
                    self.session.start(inbound_record=self._bluetooth_incoming_records.get(path))
                return
            if not getattr(self.modem, "auto_answer", True):
                self._publish({"type": "call", "status": "incoming_skipped", "caller": caller,
                               "reason": "bluetooth_outbound_only"})
                return
            # 同一通来电会被 RING 主动上报和 CLCC 轮询重复触发，需去重：
            # 已有会话进行中时直接忽略，避免重复接听 / 抢占 PCM 串口导致崩溃。
            with self._ring_lock:
                if self._sim7600_reset is not None and (
                    self.modem.recovery_in_progress or not self.modem_connected
                ):
                    return
                if self.session.is_busy or self._remote_call_owner is not None:
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
            shutdown_owned = self.session.owns_modem_shutdown
            self.session.stop()
            if not shutdown_owned:
                self.modem.hangup()

        def on_sms(sender: str | None, text: str, sms_ts: str = "") -> None:
            logger.info("收到短信 来自=%s 时间=%s 字符数=%d", sender or "未知", sms_ts, len(text))
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
            if self._sim7600_reset is not None:
                connected = self.modem.is_connected()
            self._set_modem_connected(connected)

        def on_sim_identity(identity: SimIdentity) -> None:
            self._publish({"type": "sim_status", **identity.as_dict()})

        self.modem.on_ring(on_ring)
        self.modem.on_hangup(on_hangup)
        self.modem.on_sms(on_sms)
        if isinstance(self.modem, BluetoothModem):
            self.modem.on_call_event(self._bluetooth_call_event)
        connection_registrar = getattr(self.modem, "on_connection_state", None)
        if callable(connection_registrar):
            connection_registrar(on_connection_state)
        sim_registrar = getattr(self.modem, "on_sim_identity", None)
        if callable(sim_registrar):
            sim_registrar(on_sim_identity)

    def _bluetooth_call_event(self, event: dict) -> None:
        status, path = event["status"], event["call_path"]
        if status == "sco_closed":
            self.session.phone_audio_lost()
        if status == "owner_took_over":
            self.session.phone_owner_took_over()
        if status == "incoming_ringing":
            try:
                self._bluetooth_incoming_records[path] = self.call_logger.begin_call(
                    "inbound", event.get("caller"), source="bluetooth", recording_enabled=False,
                )
            except Exception:
                logger.warning("Bluetooth incoming record creation failed", exc_info=True)
        record = self._bluetooth_incoming_records.get(path)
        if record is not None:
            if status == "incoming_identified":
                record.number = event.get("caller")
            if status == "incoming_owner_answered":
                record.log_event("answered", owner=True)
            if status != "owner_took_over":
                record.log_event(
                    status, **{key: value for key, value in event.items() if key not in {"type", "status"}},
                )
            if status == "incoming_finished":
                if not event.get("session_started"):
                    record.finish(event["outcome"])
                self._bluetooth_incoming_records.pop(path, None)
        self._publish(event)

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
        blocked, message = self._reject_if_playbook_info_missing(
            number, task=task, preset_hint=preset_hint, preset_id=preset_id
        )
        if blocked:
            return False, message
        self._remember_outbound_task(task)
        with self._ring_lock:
            if self._sim7600_reset is not None:
                guard_failure = self._dial_guard(number)
                if guard_failure is not None:
                    return False, guard_failure.message
            if (self.session.is_busy or self._remote_call_owner is not None
                    or (isinstance(self.modem, BluetoothModem) and self.modem.phone_call_busy)):
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

    def _reject_if_playbook_info_missing(
        self,
        number: str,
        *,
        task: str | None,
        preset_hint: str | None,
        preset_id: str | None,
    ) -> tuple[bool, str | None]:
        """拨前硬拦截（WIL-129）：热线情报库要求的必备信息，预设里必须齐。

        情报库存"要什么"（键定义），预设 task_package.verification 存"是什么"
        （值）。种子/人工字段及达到独立证据阈值的 learner 字段缺失时拒绝拨号；
        证据不足的 learner 字段仅注入给 AI 参考，不参与拨前拦截。信息缺口应在拨号前暴露，
        而不是在通话里白打一通（611 演练实锤）。库未命中/开关关闭时放行；
        检查器自身异常也放行（挡住拨号主链路比漏检更糟）。
        """
        try:
            if not call_playbooks.playbooks_enabled():
                return False, None
            playbook = call_playbooks.lookup_playbook(number)
            if playbook is None:
                return False, None
            lang = agent_language()
            profile = None
            if preset_id:
                profile = lookup_profile_by_id(preset_id, number, task or "", lang=lang)
            if profile is None:
                match_key = preset_hint if preset_hint is not None else (task or "")
                profile = lookup_profile(number, match_key, lang=lang)
            package = (profile or {}).get("task_package")
            missing = call_playbooks.missing_required_info(
                playbook, package if isinstance(package, dict) else None
            )
            if not missing:
                return False, None
            return True, call_playbooks.missing_info_message(missing, lang)
        except Exception:  # noqa: BLE001
            logger.exception("呼叫情报库拨前检查异常，放行")
            return False, None

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

        def _livekit_media_host() -> str:
            # 局部导入：web 层依赖 call_agent，模块级导入会成环。
            from .web.remote_gateway import livekit_media_host

            return livekit_media_host(config.get_str("LIVEKIT_URL"))

        enabled = config.get_bool("REMOTE_WEB_DIALER_ENABLED") and not isinstance(self.modem, BluetoothModem)
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
            # PWA 用它做 fragment 邀请的 exact-host 白名单（#41）。非机密：
            # 同一个 host 本来就写在 CSP connect-src 里。空串 = 不接受任何 fragment。
            "media_host": _livekit_media_host(),
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
            local_session_active = self.session.is_busy
            remote_call_active = self._remote_call_owner is not None
        phone_active = isinstance(self.modem, BluetoothModem) and self.modem.phone_call_busy
        return remote_session_active or local_session_active or remote_call_active or phone_active

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
        if isinstance(self.modem, BluetoothModem):
            return "蓝牙线路 Phase 1 仅支持 AI 外呼，暂不支持非 AI 的远程网页通话"
        with self._ring_lock:
            worker = self._remote_worker
            if worker is None or worker.coordinator is not owner:
                return "远程拨号会话已过期"
            if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
                return "远程网页拨号已关闭"
            guard_failure = self._dial_guard(None)
            if guard_failure is not None:
                return guard_failure
            if self.session.is_busy or self._remote_call_owner is not None:
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
        if isinstance(self.modem, BluetoothModem) and not self.modem.is_connected():
            return DialGuardFailure("MODEM_OFFLINE", "手机未连接，请检查手机蓝牙与 HFP 连接")
        return check_dial_guard(
            modem_online=self.modem_connected and not self.modem.recovery_in_progress
            if self._sim7600_reset is not None else self.modem_connected,
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
            if isinstance(self.modem, BluetoothModem):
                self._set_modem_connected(self.modem.is_connected())
                return  # HFP listener owns reconnection, not the serial-port watchdog.
            self._set_modem_connected(True)
            logger.info("模组已连接，等待来电…")
            self._watch_modem_link()
            return

    # 断连超过该秒数仍未自愈，就由 supervisor 重新走一遍连接流程。
    _LINK_WATCHDOG_SECONDS = 45.0
    _LINK_WATCHDOG_POLL = 5.0
    # 单轮重建的时限：connect() 会跑整套初始化（SMS/SIM 查询），正常几秒内完成。
    # 真机 2026-08-12 出现过 connect() 5 分钟不返回（WIL-111），故必须有界——
    # 超时就放弃本轮、下轮再试，绝不让看门狗线程被挂死而失去看护能力。
    _LINK_RECONNECT_TIMEOUT = 30.0

    def _modem_port_ready(self) -> bool:
        """串口是否已存在。

        macOS 上串口是 USB 桥建出来的 PTY symlink；桥执行 PCM 自愈期间会删掉它、
        约一分半后重建。此时去 connect 注定失败，还可能卡在半开状态（WIL-111），
        故先看文件在不在，不在就安静等下一轮。``auto`` 哨兵交给探测逻辑，不预判。
        """
        port = getattr(self.modem, "port", "")
        if not port or port == platforms.AUTO_PORT:
            return True
        return os.path.exists(port)

    def _reconnect_modem_bounded(self) -> bool:
        """在时限内重建模组连接；超时返回 False（本轮放弃，下轮再试）。

        connect() 可能因串口/锁竞争长时间阻塞，直接在看门狗线程里调用会把看门狗
        本身挂死。放到独立线程并 join(timeout)：即便它卡住，看门狗仍能继续巡检。
        """
        result: dict[str, BaseException | None] = {"error": None}

        def _do() -> None:
            try:
                self.modem.connect()
                self.modem.initialize_for_voice(self.audio_mode)
                self.modem.start_listener()
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(
            target=_do, name="modem-watchdog-reconnect", daemon=True
        )
        worker.start()
        worker.join(timeout=self._LINK_RECONNECT_TIMEOUT)
        if worker.is_alive():
            logger.warning(
                "看门狗重建连接超过 %.0fs 未完成，本轮放弃（下轮再试）",
                self._LINK_RECONNECT_TIMEOUT,
            )
            return False
        if result["error"] is not None:
            logger.warning("看门狗重建连接失败，稍后再试: %s", result["error"])
            return False
        return True

    def _watch_modem_link(self) -> None:
        """连接看门狗：模组长时间断开就重新建连（不依赖 modem 内部重连线程活着）。

        真机事故（2026-08-12，WIL-110）：USB 桥执行 PCM 卡死自愈期间串口消失约
        100 秒，桥恢复后 app 的重连线程已因异常穿透而死亡，进程活着却永远连不
        回来——来电全部无人接听，只能手工重启服务。modem 侧的异常穿透已修，但
        这里再加一道与线程状态无关的兜底：只要「断开持续够久」就重建连接
        （``start_listener`` 对已存活的线程是幂等的，不会重复起线程）。
        """
        # 鸭子类型的 modem（测试替身/旧实现）可能没有 is_connected，
        # 那就没有可看护的信号，直接退出而不是让看门狗线程抛异常死掉。
        is_connected = getattr(self.modem, "is_connected", None)
        if not callable(is_connected):
            return
        disconnected_since: float | None = None
        while self._service_running:
            time.sleep(self._LINK_WATCHDOG_POLL)
            if not self._service_running:
                return
            if self._sim7600_reset is not None and self.modem.recovery_in_progress:
                disconnected_since = None
                continue
            if is_connected():
                disconnected_since = None
                continue
            now = time.monotonic()
            if disconnected_since is None:
                disconnected_since = now
                continue
            if now - disconnected_since < self._LINK_WATCHDOG_SECONDS:
                continue
            if not self._modem_port_ready():
                # 桥多半正在做 PCM 自愈（串口被删、约 1 分半后重建）：安静等它建好，
                # 不把计时清零，端口一回来立刻重连。
                continue
            logger.warning(
                "模组已断开超过 %.0fs，看门狗重建连接", self._LINK_WATCHDOG_SECONDS
            )
            disconnected_since = None
            if not self._reconnect_modem_bounded():
                continue
            self._set_modem_connected(True)
            logger.info("看门狗已恢复模组连接，等待来电…")

    @property
    def modem_resetting(self) -> bool:
        return isinstance(self.modem, Sim7600Modem) and self.modem.recovery_in_progress

    def _set_modem_connected(self, connected: bool, error: str | None = None) -> None:
        """更新模组连接状态并广播给 UI（仅状态翻转时发事件，避免重连期刷屏）。"""
        if connected and self._sim7600_reset is not None:
            connected = self.modem.is_connected()
        with self._modem_state_lock:
            resetting = self.modem_resetting
            connection_changed = connected != self.modem_connected
            if not connection_changed and resetting == self._reported_modem_resetting:
                return
            self.modem_connected = connected
            self._reported_modem_resetting = resetting
            event: dict = {"type": "modem_status", "connected": connected, "modem_resetting": resetting}
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
                if connection_changed:
                    self._modem_disconnected_monotonic = time.monotonic()
                    self._modem_disconnected_at = datetime.now(UTC).isoformat()
                event["disconnected_at"] = self._modem_disconnected_at
            if error:
                event["error"] = error
        self._publish(event)
        if not connection_changed:
            return  # Reset completion while still offline is a UI-only status change.
        if connected:
            if "recovery_seconds" in event:
                logger.info(
                    "模组连接已恢复: recovery_seconds=%.3f",
                    event["recovery_seconds"],
                )
            self._publish({"type": "system", "text": "服务已启动，等待来电"})
            if self._sim7600_reset is not None:
                with self._ring_lock:
                    resume_queue = self._reset_queue_pending
                    self._reset_queue_pending = False
                if resume_queue:
                    self._handle_session_ended()
        else:
            logger.warning("模组连接已断开: disconnected_at=%s", event["disconnected_at"])

    def stop_service(self) -> None:
        """停止 supervisor 与当前会话，关闭模组（供退出时调用）。"""
        self._service_running = False
        if self._sim7600_reset is not None:
            self._sim7600_reset.stop()
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
