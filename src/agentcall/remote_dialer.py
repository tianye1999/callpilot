"""Remote Web Dialer orchestration and short-lived LiveKit session issuance.

The browser and Edge exchange control messages over the same room data channel as
the media.  Edge therefore needs outbound connectivity only; the local admin Web
server is never part of the public call path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from queue import Empty, Full, Queue
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlparse

from .audio_bridge import MODEM_RATE, create_audio_bridge
from .call_log import CallLogger, CallRecord
from .dtmf import dtmf_tone
from .pcm_stats import PcmFlowStats

logger = logging.getLogger(__name__)

REMOTE_AUDIO_RATE = MODEM_RATE
REMOTE_CALL_SOURCE = "remote_web_dialer"
REMOTE_CONTROL_TOPIC = "callpilot.control"
REMOTE_STATUS_TOPIC = "callpilot.status"

_NUMBER_RE = re.compile(r"\+?[0-9*#]{1,32}")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
_DTMF_RE = re.compile(r"[0-9*#]{1,16}")


class RemoteMediaEndpoint(Protocol):
    """Media/control contract implemented by LiveKit and in-memory test fakes."""

    @property
    def media_ready(self) -> bool: ...

    @property
    def browser_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def next_command(self, timeout: float) -> dict[str, Any] | None: ...

    def take_browser_audio(self, max_chunks: int = 10) -> list[bytes]: ...

    def push_modem_audio(self, pcm: bytes) -> None: ...

    async def send_event(self, event: dict[str, Any]) -> None: ...


class AudioBridgeLike(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read_modem_chunk(self) -> bytes: ...

    def write_modem_chunks(self, chunks: Iterable[bytes]) -> None: ...


class RemoteModem(Protocol):
    def dial(self, number: str) -> str: ...

    def is_call_connected(self) -> bool: ...

    def initialize_for_voice(self, audio_mode: str) -> None: ...

    def send_dtmf(self, digits: str) -> bool: ...

    def pcm_ready(self) -> bool: ...

    def hangup(self) -> None: ...


@dataclass(frozen=True)
class RemoteDialerInvite:
    session_id: str
    url: str
    expires_at: float


@dataclass(frozen=True)
class IssuedLiveKitSession:
    invite: RemoteDialerInvite
    room_name: str
    browser_identity: str
    edge_identity: str
    browser_token: str
    edge_token: str
    livekit_url: str


@dataclass(frozen=True)
class RemoteDialerRuntimeConfig:
    audio_mode: str
    audio_keyword: str
    pcm_port: str | None
    pcm_baudrate: int
    tx_gain: float
    disconnect_grace_seconds: float = 5.0
    outbound_max_seconds: float = 1800.0
    connect_timeout_seconds: float = 45.0
    dtmf_mode: str = "inband"
    recording_enabled: bool = True


def _validate_url(value: str, *, schemes: set[str], label: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.netloc or parsed.username or parsed.password:
        allowed = "/".join(sorted(schemes))
        raise ValueError(f"{label} 必须是合法的 {allowed} URL")
    return value


def issue_livekit_session(
    *,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    public_url: str,
    ttl_seconds: int = 300,
    now: float | None = None,
) -> IssuedLiveKitSession:
    """Issue room-scoped browser/Edge credentials and encode only the browser token.

    The invite payload lives in the URL fragment, so a static page host never sees it
    in an HTTP request.  The page erases the fragment immediately after parsing it.
    """

    from livekit import api

    livekit_url = _validate_url(livekit_url, schemes={"wss"}, label="LIVEKIT_URL")
    public_url = _validate_url(public_url, schemes={"https"}, label="REMOTE_CONTROL_URL")
    if urlparse(public_url).fragment:
        raise ValueError("REMOTE_CONTROL_URL 不允许包含 URL fragment")
    api_key = (api_key or "").strip()
    api_secret = (api_secret or "").strip()
    if not api_key or not api_secret:
        raise ValueError("缺少 LIVEKIT_API_KEY 或 LIVEKIT_API_SECRET")
    if ttl_seconds < 30 or ttl_seconds > 900:
        raise ValueError("远程拨号邀请有效期必须在 30-900 秒之间")

    session_id = secrets.token_urlsafe(18)
    room_name = f"callpilot-{secrets.token_hex(12)}"
    browser_identity = f"web-{secrets.token_hex(8)}"
    edge_identity = f"edge-{secrets.token_hex(8)}"
    ttl = timedelta(seconds=ttl_seconds)

    browser_grant = api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        can_publish_sources=["microphone"],
    )
    edge_grant = api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        can_publish_sources=["microphone"],
    )
    browser_token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(browser_identity)
        .with_ttl(ttl)
        .with_grants(browser_grant)
        .to_jwt()
    )
    edge_token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(edge_identity)
        .with_ttl(ttl)
        .with_grants(edge_grant)
        .to_jwt()
    )
    payload = {
        "v": 1,
        "url": livekit_url,
        "token": browser_token,
        "sessionId": session_id,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    created_at = time.time() if now is None else now
    invite = RemoteDialerInvite(
        session_id=session_id,
        url=f"{public_url}#{encoded}",
        expires_at=created_at + ttl_seconds,
    )
    return IssuedLiveKitSession(
        invite=invite,
        room_name=room_name,
        browser_identity=browser_identity,
        edge_identity=edge_identity,
        browser_token=browser_token,
        edge_token=edge_token,
        livekit_url=livekit_url,
    )


class RemoteWebDialerCoordinator:
    """One-invite, one-browser, one-call state machine.

    ``run`` executes on the same asyncio loop as the media endpoint.  Modem/UAC
    operations remain synchronous and single-owner, matching the existing call path.
    Cross-thread shutdown requests are represented by threading events and observed
    by the 50 ms coordinator tick.
    """

    def __init__(
        self,
        *,
        session_id: str,
        expires_at: float,
        modem: RemoteModem,
        endpoint: RemoteMediaEndpoint,
        runtime: RemoteDialerRuntimeConfig,
        bridge_factory: Callable[..., AudioBridgeLike] = create_audio_bridge,
        call_logger: CallLogger | None = None,
        reserve_line: Callable[[RemoteWebDialerCoordinator], str | None] | None = None,
        release_line: Callable[[RemoteWebDialerCoordinator], None] | None = None,
        publish_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.expires_at = expires_at
        self.modem = modem
        self.endpoint = endpoint
        self.runtime = runtime
        self.bridge_factory = bridge_factory
        self.call_logger = call_logger
        self._reserve_line = reserve_line or (lambda _owner: None)
        self._release_line = release_line or (lambda _owner: None)
        self._publish_event = publish_event

        self.edge_ready = threading.Event()
        self.finished = threading.Event()
        self.call_active = threading.Event()
        # call_active 在 ATD 前即置位（振铃阶段）；_call_connected 仅在模组物理
        # 接通后置位，用于区分「振铃期」与「已接通」两种断线守护策略（#75）。
        self._call_connected = threading.Event()
        self.last_error: str | None = None
        self._stop_requested = threading.Event()
        self._stop_reason = "edge_shutdown"
        self._call_stop_requested = threading.Event()
        self._call_stop_reason = "user_hangup"
        self._call_task: asyncio.Task[None] | None = None
        self._bridge: AudioBridgeLike | None = None
        self._record: CallRecord | None = None
        self._line_reserved = False
        self._dial_attempted = False
        self._idempotency_key: str | None = None
        self._last_status: dict[str, Any] = {"type": "status", "status": "starting"}
        self._local_commands: Queue[dict[str, Any]] = Queue(maxsize=16)

    def request_stop(self, reason: str = "edge_shutdown") -> None:
        self._stop_reason = reason
        self._stop_requested.set()
        if self.call_active.is_set():
            self.request_call_stop(reason)

    def request_call_stop(self, reason: str = "remote_party_hangup") -> None:
        self._call_stop_reason = reason
        self._call_stop_requested.set()

    def submit_local_command(self, command: dict[str, Any]) -> bool:
        """Queue a trusted local-dashboard command without crossing asyncio loops."""

        try:
            self._local_commands.put_nowait(dict(command))
        except Full:
            return False
        return True

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "edge_ready": self.edge_ready.is_set(),
            "browser_connected": self.endpoint.browser_connected,
            "media_ready": self.endpoint.media_ready,
            "call_active": self.call_active.is_set(),
            "expires_at": self.expires_at,
            "status": self._last_status.get("status", "starting"),
        }

    async def run(self) -> None:
        media_was_ready = False
        disconnect_since: float | None = None
        prev_connected_phase = False
        try:
            await self.endpoint.connect()
            self.edge_ready.set()
            await self._send_status("waiting_for_phone")
            while not self._stop_requested.is_set():
                now = time.time()
                if not self._dial_attempted and now >= self.expires_at:
                    await self._send_status(
                        "failed", code="invite_expired", reason="invite_expired"
                    )
                    break

                if self.endpoint.media_ready and not media_was_ready:
                    media_was_ready = True
                    await self._send_status("media_ready")
                elif not self.endpoint.media_ready and media_was_ready:
                    media_was_ready = False

                # phase-aware 断线守护（#75）：
                # - 振铃期（未物理接通）：只有**控制连接**真断（browser_connected
                #   =False）才按 grace 挂断；音轨暂时 mute/重协商（media_ready=False
                #   但参与者仍在）不中断振铃，否则长振铃真人号会被误挂（接通即掉）。
                # - 已接通：媒体轨长期丢失才按 grace 安全收尾。
                if self.call_active.is_set():
                    connected_phase = self._call_connected.is_set()
                    if connected_phase != prev_connected_phase:
                        # 阶段切换（振铃→接通）：判据身份变化，重置计时——
                        # 接通后应享有完整媒体 grace，不沿用振铃期的控制断计时
                        # （否则接通瞬间残留计时到点会立即误挂，仍表现为「接通即掉」）。
                        disconnect_since = None
                        prev_connected_phase = connected_phase
                    if connected_phase:
                        disconnected = not self.endpoint.media_ready
                        stop_reason, phase = "media_disconnected", "connected"
                    else:
                        disconnected = not self.endpoint.browser_connected
                        stop_reason, phase = "control_disconnected", "ringing"
                    if disconnected:
                        if disconnect_since is None:
                            disconnect_since = time.monotonic()
                        elif (
                            time.monotonic() - disconnect_since
                            >= self.runtime.disconnect_grace_seconds
                        ):
                            logger.info(
                                "远程通话本端 ATH: reason=%s phase=%s", stop_reason, phase
                            )
                            self.request_call_stop(stop_reason)
                    else:
                        disconnect_since = None
                else:
                    disconnect_since = None
                    prev_connected_phase = False

                command: dict[str, Any] | None
                try:
                    command = self._local_commands.get_nowait()
                except Empty:
                    command = await self.endpoint.next_command(0.05)
                if command is not None:
                    await self._handle_command(command)

                if self._call_task is not None and self._call_task.done():
                    await self._call_task
                    break
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.warning("远程网页拨号会话失败: error_type=%s", type(exc).__name__)
            await self._send_status(
                "failed", code="edge_error", reason="edge_error"
            )
        finally:
            if self._call_task is not None and not self._call_task.done():
                self.request_call_stop(self._stop_reason)
                try:
                    await asyncio.wait_for(self._call_task, timeout=3.0)
                except TimeoutError:
                    self._call_task.cancel()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("远程通话收尾失败: %s", type(exc).__name__)
            await self.endpoint.close()
            self.finished.set()

    async def _handle_command(self, command: dict[str, Any]) -> None:
        command_type = command.get("type")
        if command_type == "dial":
            await self._handle_dial(command)
        elif command_type == "hangup":
            if self._call_task is None:
                await self._send_status("ended", reason="user_hangup")
                self._stop_requested.set()
            else:
                self.request_call_stop("user_hangup")
        elif command_type == "dtmf":
            await self._handle_dtmf(command)
        elif command_type == "ping":
            await self._send_ephemeral_status(
                str(self._last_status.get("status", "waiting_for_phone")),
                event="pong",
            )
        else:
            await self._send_status(
                "failed", code="invalid_command", reason="invalid_command"
            )

    async def _handle_dial(self, command: dict[str, Any]) -> None:
        number = command.get("number")
        idempotency_key = command.get("idempotency_key")
        if not isinstance(number, str) or _NUMBER_RE.fullmatch(number.strip()) is None:
            await self._send_status("failed", code="invalid_number", reason="invalid_number")
            return
        if (
            not isinstance(idempotency_key, str)
            or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
        ):
            await self._send_status(
                "failed", code="invalid_idempotency_key", reason="invalid_request"
            )
            return
        if self._idempotency_key == idempotency_key:
            replay = dict(self._last_status)
            replay["replayed"] = True
            await self.endpoint.send_event(replay)
            return
        if self._dial_attempted:
            await self._send_status(
                "failed", code="call_already_started", reason="call_already_started"
            )
            return
        if not self.endpoint.media_ready:
            await self._send_status(
                "failed", code="media_not_ready", reason="media_not_ready"
            )
            return

        self._dial_attempted = True
        self._idempotency_key = idempotency_key
        self._call_stop_requested.clear()
        self._call_task = asyncio.create_task(self._run_call(number.strip()))

    async def _handle_dtmf(self, command: dict[str, Any]) -> None:
        digits = command.get("digits")
        if not isinstance(digits, str) or _DTMF_RE.fullmatch(digits) is None:
            await self._send_ephemeral_status(
                "connected" if self.call_active.is_set() else "failed",
                event="dtmf_failed",
                code="invalid_dtmf",
            )
            return
        bridge = self._bridge
        if not self.call_active.is_set() or bridge is None:
            await self._send_ephemeral_status(
                self._last_status.get("status", "failed"),
                event="dtmf_failed",
                code="call_not_connected",
            )
            return

        mode = self._resolved_dtmf_mode()
        ok = True
        if mode in {"inband", "both"}:
            tone = dtmf_tone(digits, REMOTE_AUDIO_RATE)
            if not tone:
                ok = False
            else:
                if self._record is not None:
                    self._record.write_downlink(tone)
                bridge.write_modem_chunks([tone])
        if mode in {"qvts", "both"}:
            ok = self.modem.send_dtmf(digits) and ok
        if self._record is not None:
            self._record.log_event(
                "dtmf",
                count=len(digits),
                mode=mode,
                result="success" if ok else "failure",
                source=REMOTE_CALL_SOURCE,
            )
        await self._send_ephemeral_status(
            "connected",
            event="dtmf_sent" if ok else "dtmf_failed",
            code=None if ok else "dtmf_failed",
        )

    async def _run_call(self, number: str) -> None:
        bridge: AudioBridgeLike | None = None
        record: CallRecord | None = None
        connected = False
        snapshot_task: asyncio.Task[None] | None = None
        finish_status = "failed"
        finish_reason = "edge_error"
        try:
            reserve_error = self._reserve_line(self)
            if reserve_error:
                finish_reason = "line_unavailable"
                await self._send_status(
                    "failed", code="line_unavailable", reason=reserve_error
                )
                return
            self._line_reserved = True
            self.call_active.set()
            record = self._begin_record(number)
            self._record = record
            if record is not None:
                record.log_event("remote_session", source=REMOTE_CALL_SOURCE)

            # The media-ready check is intentionally repeated immediately before ATD.
            if not self.endpoint.media_ready:
                finish_reason = "media_not_ready"
                await self._send_status(
                    "failed", code="media_not_ready", reason=finish_reason
                )
                return

            self.modem.dial(number)
            await self._send_status("dialing")
            self._publish({"type": "remote_call", "status": "dialing"})
            if record is not None:
                record.log_event("dialing", source=REMOTE_CALL_SOURCE)

            deadline = time.monotonic() + self.runtime.connect_timeout_seconds
            while time.monotonic() < deadline:
                # 先判接通、再判 stop：避免接通与 grace 同 tick 时 stop 抢先，
                # 把已接起的电话误判为 not_connected（#75）。
                if self.modem.is_call_connected():
                    connected = True
                    break
                if self._call_stop_requested.is_set():
                    break
                await asyncio.sleep(0.05)
            if not connected:
                finish_status = "not_connected"
                finish_reason = (
                    self._call_stop_reason
                    if self._call_stop_requested.is_set()
                    else "not_connected"
                )
                logger.info(
                    "远程外呼未接通即收尾: reason=%s phase=ringing", finish_reason
                )
                await self._send_status("ended", reason=finish_reason)
                return

            # 物理接通：此后断线守护改用「媒体轨」判据（振铃期用「控制连接」）。
            self._call_connected.set()
            self.modem.initialize_for_voice(self.runtime.audio_mode)
            bridge = self.bridge_factory(
                mode=self.runtime.audio_mode,
                device_keyword=self.runtime.audio_keyword,
                pcm_port=self.runtime.pcm_port,
                pcm_baudrate=self.runtime.pcm_baudrate,
                tx_gain=self.runtime.tx_gain,
            )
            if hasattr(bridge, "set_ready_check"):
                bridge.set_ready_check(self.modem.pcm_ready)  # type: ignore[attr-defined]
            bridge.start()
            self._bridge = bridge
            if record is not None:
                record.log_event("answered", source=REMOTE_CALL_SOURCE)
            await self._send_status("connected")
            self._publish({"type": "remote_call", "status": "connected"})
            # connected 若因数据通道瞬时断连丢包，靠独立任务周期重发规范快照兜住
            # （固定 connected，不随 _last_status 漂移）；与音频泵解耦，网络发送不
            # 阻塞 10ms 热循环（#74）。
            snapshot_task = asyncio.create_task(
                self._status_snapshot_loop({"type": "status", "status": "connected"})
            )

            started_at = time.monotonic()
            # 上行第二段观测：泵从 endpoint 取走并写入音频桥的帧统计。
            # 与 uplink1_lk_in（LiveKit 入站）、uplink3_as_write（ffmpeg AS
            # 写入）对照，可二分「链路建立但无声」断在哪一段。
            take_stats = PcmFlowStats("uplink2_pump_take")
            pending_output_bytes = getattr(bridge, "pending_output_bytes", None)
            while not self._call_stop_requested.is_set():
                if not self.modem.is_call_connected():
                    self._call_stop_reason = "remote_party_hangup"
                    break
                if (
                    self.runtime.outbound_max_seconds > 0
                    and time.monotonic() - started_at >= self.runtime.outbound_max_seconds
                ):
                    self._call_stop_reason = "max_duration"
                    break

                browser_chunks = self.endpoint.take_browser_audio()
                if browser_chunks:
                    if record is not None:
                        for chunk in browser_chunks:
                            record.write_downlink(chunk)
                    bridge.write_modem_chunks(browser_chunks)
                    for chunk in browser_chunks:
                        take_stats.add(chunk)
                if take_stats.due():  # 到期才取 pending，避免每 10ms 拿一次桥锁
                    take_stats.maybe_log(
                        pending=(
                            pending_output_bytes() if pending_output_bytes else "n/a"
                        ),
                    )

                modem_pcm = bridge.read_modem_chunk()
                if modem_pcm:
                    if record is not None:
                        record.write_uplink(modem_pcm)
                    self.endpoint.push_modem_audio(modem_pcm)

                await asyncio.sleep(0.01)

            finish_status = "completed"
            finish_reason = self._call_stop_reason
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            finish_status = "failed"
            finish_reason = "edge_error"
            logger.exception("远程网页通话失败: %s", type(exc).__name__)
            await self._send_status(
                "failed", code="edge_error", reason="edge_error"
            )
        finally:
            if snapshot_task is not None:
                snapshot_task.cancel()
            self._bridge = None
            if bridge is not None:
                try:
                    bridge.stop()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("关闭远程音频桥失败: %s", type(exc).__name__)
            if self._line_reserved:
                try:
                    self.modem.hangup()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("远程通话挂断模组失败: %s", type(exc).__name__)
            if record is not None:
                try:
                    record.finish(finish_status)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("远程通话记录落盘失败: %s", type(exc).__name__)
            if self._line_reserved:
                self._release_line(self)
                self._line_reserved = False
            self._record = None
            self.call_active.clear()
            self._call_connected.clear()
            await self._send_status(
                "ended", reason=finish_reason, outcome=finish_status
            )
            self._publish(
                {
                    "type": "remote_call",
                    "status": "ended",
                    "reason": finish_reason,
                    "outcome": finish_status,
                }
            )

    def _begin_record(self, number: str) -> CallRecord | None:
        if self.call_logger is None:
            return None
        try:
            return self.call_logger.begin_call(
                "outbound",
                number,
                source=REMOTE_CALL_SOURCE,
                recording_enabled=self.runtime.recording_enabled,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建远程通话记录失败: %s", type(exc).__name__)
            return None

    def _resolved_dtmf_mode(self) -> str:
        mode = self.runtime.dtmf_mode.strip().lower()
        if mode not in {"inband", "qvts", "both"}:
            mode = "inband"
        if self.runtime.audio_mode not in {"uac", "uac_ffmpeg"}:
            return "qvts"
        return mode

    async def _send_status(self, status: str, **fields: Any) -> None:
        event = {"type": "status", "status": status}
        event.update({key: value for key, value in fields.items() if value is not None})
        self._last_status = event
        try:
            await self.endpoint.send_event(event)
        except Exception as exc:  # noqa: BLE001
            # Status delivery is advisory. A transient data-channel reconnect must
            # not tear down an otherwise healthy physical call.
            logger.debug("远程状态发送失败: %s", type(exc).__name__)

    async def _send_ephemeral_status(self, status: str, **fields: Any) -> None:
        event = {"type": "status", "status": status}
        event.update({key: value for key, value in fields.items() if value is not None})
        try:
            await self.endpoint.send_event(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("远程状态发送失败: %s", type(exc).__name__)

    async def _status_snapshot_loop(
        self, snapshot: dict[str, Any], interval: float = 1.0
    ) -> None:
        """通话期间每 ~1s 重发一个**固定**状态快照的独立任务。

        ``send_event`` 在数据通道瞬时断连（``browser_connected`` 为 False）时会
        静默丢弃，首个 ``connected`` 若恰好丢包，客户端会卡在旧状态（真人号响铃
        较久时更易触发）。本任务与音频泵解耦（网络发送不阻塞 10ms 热循环），且
        重发调用方传入的固定快照，不读可能被 ``media_ready``/``failed`` 覆盖的
        ``_last_status``。最小修复；完整 seq/ping/HTTP 兜底方案见 #74。
        """
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.endpoint.send_event(dict(snapshot))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("远程状态快照重发失败: %s", type(exc).__name__)
        except asyncio.CancelledError:
            pass

    def _publish(self, event: dict[str, Any]) -> None:
        if self._publish_event is not None:
            self._publish_event(event)


class RemoteDialerWorker:
    """Own the coordinator loop in a daemon thread and expose synchronous control."""

    def __init__(self, coordinator: RemoteWebDialerCoordinator) -> None:
        self.coordinator = coordinator
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self, timeout: float = 10.0) -> None:
        if self.is_running:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="remote-web-dialer",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.coordinator.edge_ready.wait(timeout=0.05):
                return
            if self.coordinator.finished.is_set():
                break
        self.coordinator.request_stop("startup_failed")
        detail = self.coordinator.last_error or "连接 LiveKit 超时"
        raise RuntimeError(f"远程媒体端点启动失败: {detail}")

    def stop(self, reason: str = "edge_shutdown", *, join_timeout: float = 3.0) -> None:
        self.coordinator.request_stop(reason)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)

    def _run(self) -> None:
        try:
            asyncio.run(self.coordinator.run())
        except Exception as exc:  # noqa: BLE001
            self.coordinator.last_error = str(exc)
            logger.warning("远程拨号 worker 异常退出: error_type=%s", type(exc).__name__)
            self.coordinator.finished.set()
