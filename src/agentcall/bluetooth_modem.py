"""oFono HFP phone line with explicit, per-call AI audio ownership."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from queue import Empty, Queue
from typing import Any

from . import config
from .bluetooth_audio import BluetoothAudioBridge
from .bluetooth_inbound import IncomingCall, masked_number
from .modem import SerialModem
from .sim_identity import UNKNOWN_SIM, identify_hfp, with_service_number_override

logger = logging.getLogger(__name__)


class BluetoothModem(SerialModem):
    """Reuse the modem callback contract, never the serial/AT transport.

    Signals are queued off the D-Bus loop: callbacks may synchronously call the
    modem. The listener owns refresh/reconnect; CallSession owns Register/Dial/Answer.
    """

    supports_sms = False
    auto_answer = False
    line_kind = "bluetooth"
    POST_ANSWER_SETTLE_SECONDS = 0.0
    AGENT_PATH = "/org/callpilot/HandsfreeAudioAgent"
    HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
    ANSWER_ACTIVE_TIMEOUT_SECONDS = 3.0

    def __init__(self, address: str, *, transport: Any = None,
                 clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float], object] | None = None) -> None:
        super().__init__("")
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address):
            raise ValueError("请在本地 .env 配置 BLUETOOTH_PHONE_ADDRESS（手机蓝牙地址）")
        self.address = address.upper()
        self._transport = transport
        self._clock = clock
        self._transport_ready = False
        self._audio_lock = threading.RLock()
        self._owner_lock = threading.RLock()
        self._events: Queue[tuple] = Queue()
        self._stop_event = threading.Event()
        self._wait = wait if wait is not None else self._stop_event.wait
        self._modem_path = ""
        self._calls: dict[str, dict] = {}
        self._incoming: dict[str, IncomingCall] = {}
        self._answer_candidate = ""
        self._claimed_inbound = ""
        self._on_call_event: Callable[[dict], None] | None = None
        self._owner_released = threading.Event()
        self._sco_confirm_at: float | None = None
        self._sco_resolution = threading.Event()
        self._sco_resolution.set()
        self.owner_takeover_confirmed = False
        self._reconnect_logs: dict[str, float] = {}
        self._owned_path = ""
        self.ai_owned = False
        self._registered = False
        self._cards: set[str] = set()
        self.audio_bridge = BluetoothAudioBridge()
        self.active_since = 0.0
        self.signal_strength: int | None = None
        self._retry_at = 0.0
        self._retry_delay = 2.0
        self._audio_retry_at = 0.0
        self._audio_attempts = 0

    def _call(self, path: str, interface: str, member: str,
              signature: str = "", body: list | None = None, *, destination: str = "org.ofono") -> list:
        return self._transport.call(destination, path, interface, member, signature, body)

    def connect(self) -> None:
        if self._closed:
            raise RuntimeError("Bluetooth line is closed")
        if not self._transport_ready:
            if self._transport is None:
                from .ofono_dbus import OfonoDBus
                self._transport = OfonoDBus(self.AGENT_PATH)
            self._transport.start(lambda *event: self._events.put(event), self._accept_audio)
            self._transport_ready = True

    def initialize_for_voice(self, audio_mode: str = "uac") -> None:
        # Never register audio here: service startup invokes this outside a call.
        pass

    def on_connection_state(self, callback: Any) -> None:
        self._on_connection_state = callback

    def _emit_connection_state(self, online: bool) -> None:
        if online != self._connection_online:
            self._connection_online = online
            if self._on_connection_state:
                self._on_connection_state(online)

    def start_listener(self) -> None:
        if not self._closed and (self._reader_thread is None or not self._reader_thread.is_alive()):
            self._reader_thread = threading.Thread(target=self._listen, name="hfp-listener", daemon=True)
            self._reader_thread.start()

    def _listen(self) -> None:
        while not self._stop_event.is_set():
            try:
                while True:
                    event = self._events.get_nowait()
                    try:
                        self.handle_signal(*event)
                    except Exception:
                        logger.warning("oFono signal callback failed", exc_info=True)
            except Empty:
                pass
            try:
                self.poll_once(self._clock())
            except Exception:
                logger.warning("oFono refresh failed; phone offline", exc_info=True)
                self._offline()
            self._stop_event.wait(0.5)

    def _matches_modem(self, path: str) -> bool:
        return path.startswith("/hfp/") and self.address.replace(":", "_") in path.upper()

    def poll_once(self, now: float) -> None:
        with self._owner_lock:
            if not self._closed:
                self.connect()
                self._poll_once(now)

    def _poll_once(self, now: float) -> None:
        try:
            modems = self._call("/", "org.ofono.Manager", "GetModems")[0]
        except Exception:
            modems = []
        selected = next(((path, props) for path, props in modems if self._matches_modem(path)), None)
        if selected is None or not selected[1].get("Online", False):
            self._offline()
            if now >= self._retry_at:
                self._retry_at = now + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2, 30)
                self._reconnect_phone()
            return
        self._modem_path = selected[0]
        self._retry_delay, self._retry_at = 2.0, 0.0
        self._emit_connection_state(True)
        props = self._call(self._modem_path, "org.ofono.NetworkRegistration", "GetProperties")[0]
        previous_strength, previous_identity = self.signal_strength, self.sim_identity
        self.signal_strength = int(props["Strength"]) if "Strength" in props else None
        self._set_sim_identity(with_service_number_override(identify_hfp(props), config.get_str("CARRIER_HOTLINE")))
        if (self.signal_strength != previous_strength and self.sim_identity == previous_identity
                and self._on_sim_identity):
            self._on_sim_identity(self.sim_identity)
        calls = self._call(self._modem_path, "org.ofono.VoiceCallManager", "GetCalls")[0]
        current = {path for path, _ in calls}
        for path, properties in calls:
            self._call_changed(path, properties)
        for path in set(self._calls) - current:
            self._call_removed(path)
        for path, incoming in list(self._incoming.items()):
            self._offer_incoming(path, incoming, now)
        if self._sco_confirm_at is not None and now >= self._sco_confirm_at:
            # GetCalls above is fresh, not the cache that preceded SCO EOF.
            path = self._owned_path
            still_active = any(p == path and props.get("State") == "active" for p, props in calls)
            self._resolve_audio_loss(took_over=still_active)
            if still_active:
                self._emit_call_event("owner_took_over", path, reason="active_after_sco_closed")
            self.end_ai_call(preserve_call=True)
        with self._owner_lock:
            if (self.ai_owned and not self._owner_released.is_set() and self.is_call_connected()
                    and now >= max(self.active_since + 4, self._audio_retry_at)
                    and self.audio_bridge.packet_count == 0 and self._audio_attempts < 3):
                self._audio_retry_at = now + 5
                self._audio_attempts += 1
                self._refresh_cards()
                for card in self._cards:
                    try:
                        self._call(card, "org.ofono.HandsfreeAudioCard", "Connect")
                    except Exception:
                        logger.warning("SCO Connect not ready; bounded retry pending", exc_info=True)

    def _reconnect_phone(self) -> None:
        try:
            self._connect_profile()
        except Exception as exc:
            name = str(getattr(exc, "error_name", ""))
            detail = str(getattr(exc, "detail", str(exc))).lower()
            expected = next((kind for kind in ("InProgress", "NotReady", "NotAvailable", "NotConnected",
                                               "ConnectionAttemptFailed", "AlreadyConnected")
                             if name.endswith("." + kind)), "")
            if "host is down" in detail:
                expected = "HostDown"
            if not expected:
                logger.warning("HFP reconnect failed unexpectedly", exc_info=True)
                return
            now = self._clock()
            if now - self._reconnect_logs.get(expected, float("-inf")) >= 30:
                self._reconnect_logs[expected] = now
                logger.info("HFP phone offline; reconnect pending (%s)", expected)

    def _connect_profile(self) -> None:
        objects = self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                             destination="org.bluez")[0]
        for path, interfaces in objects.items():
            device = interfaces.get("org.bluez.Device1", {})
            if device.get("Address", "").upper() == self.address:
                self._call(path, "org.bluez.Device1", "ConnectProfile", "s", [self.HFP_AG_UUID],
                           destination="org.bluez")
                return

    def _offline(self) -> None:
        self._emit_connection_state(False)
        self.signal_strength = None
        self._set_sim_identity(UNKNOWN_SIM)
        self._modem_path = ""
        if self.ai_owned:
            self.end_ai_call()
            if self._on_hangup:
                self._on_hangup()
        for path in list(self._incoming):
            self._finish_incoming(path, reason="phone_offline")
        self._calls.clear()

    def handle_signal(self, path: str, interface: str, member: str, body: list) -> None:
        with self._owner_lock:
            if not self._closed:
                self._handle_signal(path, interface, member, body)

    def _handle_signal(self, path: str, interface: str, member: str, body: list) -> None:
        if interface == "org.ofono.Manager":
            if member == "ServiceChanged":
                self._offline()
            elif member == "ModemRemoved" and body[0] == self._modem_path:
                self._offline()
        elif path == self._modem_path and interface == "org.ofono.Modem":
            if member == "PropertyChanged" and body == ["Online", False]:
                self._offline()
        elif interface == "org.ofono.HandsfreeAudioAgent" and member == "Release":
            self._registered = False
            self.hangup()
            if self._on_hangup:
                self._on_hangup()
        elif path == self._modem_path and interface == "org.ofono.VoiceCallManager":
            if member == "CallAdded":
                self._call_changed(body[0], body[1])
            elif member == "CallRemoved":
                self._call_removed(body[0])
        elif (self._modem_path and interface == "org.ofono.VoiceCall"
              and path.startswith(self._modem_path + "/")):
            if member == "PropertyChanged":
                self._call_changed(path, {body[0]: body[1]})

    def _call_changed(self, path: str, props: dict) -> None:
        incoming: IncomingCall | None
        previous = self._calls.get(path, {})
        state = props.get("State", previous.get("State"))
        self._calls[path] = {**previous, **props}
        if state in {"incoming", "waiting"} and path not in self._incoming:
            incoming = IncomingCall.ringing(self._calls[path].get("LineIdentification"), self._clock(),
                                            waiting=state == "waiting")
            self._incoming[path] = incoming
            self._emit_call_event("incoming_ringing", path, caller=incoming.number)
            if incoming.deadline is None:
                self._emit_call_event("incoming_skipped", path, reason="waiting" if state == "waiting" else "policy")
            else:
                self._offer_incoming(path, incoming, self._clock())
            if self._on_ring and not incoming.offered:
                self._on_ring(incoming.number)
        incoming = self._incoming.get(path)
        if incoming and not incoming.number and props.get("LineIdentification") and not incoming.session_started:
            incoming.identify(props["LineIdentification"], self._clock())
            self._emit_call_event("incoming_identified", path)
            self._offer_incoming(path, incoming, self._clock())
        if incoming and state == "waiting":
            incoming.deadline = None
            incoming.allowed_numbers = ()
            if self._answer_candidate == path:
                self._answer_candidate = ""
        if incoming and state not in {"incoming", "waiting"}:
            incoming.deadline = None
            if self._answer_candidate == path and not incoming.session_started:
                self._answer_candidate = ""
            if state == "active" and not incoming.ai_answered and not self.ai_owned and not incoming.owner_answered:
                incoming.owner_answered = True
                self._emit_call_event("incoming_owner_answered", path)
        if self.ai_owned and path == self._owned_path:
            if state == "active" and not self.is_call_connected():
                self.active_since = self._clock()
                self._call_connected_event.set()
                if self._on_call_connected:
                    self._on_call_connected(self._last_dialed)
            if state == "active" and self.audio_bridge.closed.is_set():
                self._sco_closed(self.audio_bridge)
        if state == "disconnected":
            self._call_removed(path)

    def _call_removed(self, path: str) -> None:
        self._calls.pop(path, None)
        self._finish_incoming(path)
        if self.ai_owned and path == self._owned_path:
            self.end_ai_call()
            if self._on_hangup:
                self._on_hangup()

    def on_call_event(self, callback: Callable[[dict], None]) -> None:
        self._on_call_event = callback

    def _emit_call_event(self, status: str, path: str, **fields: Any) -> None:
        incoming = self._incoming.get(path)
        caller = incoming.number if incoming else self._last_dialed if path == self._owned_path else None
        event = {"type": "call", "status": status, "call_path": path, "caller": caller, **fields}
        logger.info("Bluetooth %s caller=%s%s", status, masked_number(caller),
                    " reason=" + str(fields["reason"]) if "reason" in fields else "")
        if self._on_call_event:
            self._on_call_event(event)

    def _finish_incoming(self, path: str, *, reason: str = "call_removed") -> None:
        incoming = self._incoming.get(path)
        if incoming is None:
            return
        outcome = "ai_answered" if incoming.ai_answered else "owner_answered" if incoming.owner_answered else "missed"
        self._emit_call_event("incoming_finished", path, outcome=outcome, reason=reason,
                              session_started=incoming.session_started)
        self._incoming.pop(path, None)
        if self._answer_candidate == path:
            self._answer_candidate = ""

    def _offer_incoming(self, path: str, incoming: IncomingCall, now: float) -> None:
        if (incoming.deadline is not None and now >= incoming.deadline and not incoming.offered
                and self._calls.get(path, {}).get("State") == "incoming"
                and not self.ai_owned and not self._answer_candidate and not self._claimed_inbound):
            incoming.offered = True
            self._answer_candidate = path
            self._emit_call_event("incoming_ai_requested", path)
            if self._on_ring:
                self._on_ring(incoming.number)

    @property
    def inbound_ready(self) -> bool:
        return bool(self._answer_candidate and self._calls.get(self._answer_candidate, {}).get("State") == "incoming")

    def claim_inbound(self) -> str:
        with self._owner_lock:
            if not self.inbound_ready:
                return ""
            path = self._answer_candidate
            self._incoming[path].session_started = True
            self._claimed_inbound = path
            self._answer_candidate = ""
            return path

    def skip_inbound(self, reason: str) -> None:
        with self._owner_lock:
            if self._answer_candidate:
                self._emit_call_event("incoming_skipped", self._answer_candidate, reason=reason)
                self._answer_candidate = ""

    def _new_audio(self) -> None:
        self._owner_released.clear()
        self._sco_confirm_at = None
        self._sco_resolution.set()
        self.owner_takeover_confirmed = False
        bridge = BluetoothAudioBridge(on_closed=lambda: self._sco_closed(bridge))
        self.audio_bridge = bridge
        self.active_since, self._audio_retry_at, self._audio_attempts = 0.0, 0.0, 0
        self._call_connected_event.clear()

    def _sco_closed(self, bridge: BluetoothAudioBridge) -> None:
        # Runs on the SCO worker: never join it or acquire the owner lock here.
        # Fence modem hangup/audio immediately, but let the listener distinguish
        # a route change from a remote hangup whose State signal arrives later.
        with self._audio_lock:
            if (bridge is not self.audio_bridge or not self.ai_owned or self._owner_released.is_set()
                    or not bridge.packet_count or self._calls.get(self._owned_path, {}).get("State") != "active"):
                return
            self._owner_released.set()
            self._sco_confirm_at = self._clock() + 1.0
            self._sco_resolution.clear()
            path = self._owned_path
        self._emit_call_event("sco_closed", path, reason="awaiting_call_state_confirmation")

    @property
    def audio_loss_pending(self) -> bool:
        return self._sco_confirm_at is not None

    def _resolve_audio_loss(self, *, took_over: bool = False) -> None:
        with self._audio_lock:
            self._sco_confirm_at = None
            self.owner_takeover_confirmed = took_over
            self._sco_resolution.set()

    def wait_audio_resolution(self) -> None:
        # The listener normally resolves this after ~1s. Bound cleanup even if
        # that listener has stopped or a D-Bus refresh is stuck; never guess a
        # takeover or hang up a phone call on this fallback.
        if not self._sco_resolution.wait(timeout=5):
            with self._owner_lock:
                if self.audio_loss_pending:
                    logger.warning("SCO call-state confirmation unavailable; AI detached without hangup")
                    self._resolve_audio_loss()
                    self.end_ai_call(preserve_call=True)

    def _refresh_cards(self) -> None:
        cards = self._call("/", "org.ofono.HandsfreeAudioManager", "GetCards")[0]
        with self._audio_lock:
            self._cards = {path for path, props in cards if props.get("RemoteAddress", "").upper() == self.address}

    def _accept_audio(self, card: str, fd: int, codec: int) -> bool:
        with self._audio_lock:
            if not self.ai_owned or self._owner_released.is_set() or card not in self._cards or codec != 1:
                os.close(fd)
                return False
            return self.audio_bridge.accept_fd(fd, codec)

    def dial(self, number: str) -> str:
        with self._owner_lock:
            if not self.is_connected():
                raise RuntimeError("手机未连接，请检查手机蓝牙与 HFP 连接")
            # Do not rely on the half-second cache when the owner is using the phone.
            calls = self._call(self._modem_path, "org.ofono.VoiceCallManager", "GetCalls")[0]
            if calls or self._calls or self.ai_owned:
                raise RuntimeError("手机正在通话，不能发起 AI 外呼")
            self._new_audio()
            self._last_dialed = number
            self._owned_path = ""
            self._refresh_cards()
            try:
                with self._audio_lock:
                    self.ai_owned = True
                # Even a timed-out Register may have succeeded on the daemon.
                self._registered = True
                self._call("/", "org.ofono.HandsfreeAudioManager", "Register", "oay", [self.AGENT_PATH, b"\x01"])
                self._owned_path = self._call(self._modem_path, "org.ofono.VoiceCallManager", "Dial",
                                              "ss", [number, ""])[0]
                self._calls.setdefault(self._owned_path, {"State": "dialing"})
            except BaseException:
                self.end_ai_call()
                raise
        return "OK"

    def answer(self) -> None:
        with self._owner_lock:
            path = self._claimed_inbound
            if not path:
                raise RuntimeError("No eligible Bluetooth incoming call")
            incoming = self._incoming.get(path)
            try:
                if not incoming or not incoming.session_started:
                    raise RuntimeError("No eligible Bluetooth incoming call")
                if not self.is_connected() or self.ai_owned:
                    raise RuntimeError("Bluetooth incoming call no longer available")
                props = self._call(path, "org.ofono.VoiceCall", "GetProperties")[0]
                if props.get("State") != "incoming":
                    self._call_changed(path, props)
                    raise RuntimeError("Incoming call already answered or ended")
                self._new_audio()
                self._last_dialed = incoming.number or ""
                self._refresh_cards()
                self._owned_path = path
                with self._audio_lock:
                    self.ai_owned = True
                self._registered = True
                self._call("/", "org.ofono.HandsfreeAudioManager", "Register", "oay", [self.AGENT_PATH, b"\x01"])
                props = self._call(path, "org.ofono.VoiceCall", "GetProperties")[0]
                if props.get("State") != "incoming":
                    raise RuntimeError("Incoming call changed during audio registration")
                self._call(path, "org.ofono.VoiceCall", "Answer")
                props = self._wait_for_answer_active(path)
                incoming.ai_answered = True
                self._call_changed(path, props)
                self._emit_call_event("incoming_ai_answered", path)
            except BaseException:
                self._emit_call_event("incoming_answer_failed", path)
                self.end_ai_call(preserve_call=True)
                raise
            finally:
                if self._answer_candidate == path:
                    self._answer_candidate = ""
                self._claimed_inbound = ""

    def _wait_for_answer_active(self, path: str) -> dict:
        # ATA's OK precedes +CIEV. Poll the same call while the listener's owner
        # lock is held; queued signals cannot advance our cache during answer().
        deadline = self._clock() + self.ANSWER_ACTIVE_TIMEOUT_SECONDS
        while not self._closed and not self._stop_event.is_set():
            props = self._call(path, "org.ofono.VoiceCall", "GetProperties")[0]
            if props.get("State") == "active":
                return props
            if props.get("State") == "disconnected":
                raise RuntimeError("Incoming call ended before answer completed")
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise RuntimeError("Timed out waiting for answered call to become active")
            self._wait(min(0.1, remaining))
        raise RuntimeError("Bluetooth line stopped while answering")

    def end_ai_call(self, *, preserve_call: bool = False) -> None:
        with self._owner_lock:
            if self.audio_loss_pending:
                self._resolve_audio_loss()
            with self._audio_lock:
                self.ai_owned = False
            if self._answer_candidate == self._claimed_inbound:
                self._answer_candidate = ""
            self._claimed_inbound = ""
            try:
                self.audio_bridge.stop()
            finally:
                self._call_connected_event.clear()
                if not preserve_call and not self._owner_released.is_set():
                    self._calls.pop(self._owned_path, None)
                self._owned_path = ""
                if self._registered:
                    try:
                        self._call("/", "org.ofono.HandsfreeAudioManager", "Unregister", "o", [self.AGENT_PATH])
                    except Exception:
                        logger.warning("oFono audio Unregister failed", exc_info=True)
                        # Releasing our bus owner also drops a daemon-side agent
                        # when the Unregister reply is lost. The listener reconnects.
                        self._transport_ready = False
                        self._transport.close()
                    finally:
                        self._registered = False

    def hangup(self) -> None:
        with self._owner_lock:
            if self.audio_loss_pending:
                return  # The SCO confirmation window must not disturb any physical call.
            try:
                if self.ai_owned and not self._owner_released.is_set():
                    if self._owned_path:
                        self._call(self._owned_path, "org.ofono.VoiceCall", "Hangup")
                    else:
                        logger.warning("Bluetooth owned path missing; no physical call hung up")
            finally:
                self.end_ai_call()

    def send_dtmf(self, digits: str) -> bool:
        if (not self.ai_owned or self._owner_released.is_set() or not self.is_call_connected()
                or not re.fullmatch(r"[0-9*#A-D]+", digits)):
            return False
        self._call(self._modem_path, "org.ofono.VoiceCallManager", "SendTones", "s", [digits])
        return True

    def is_connected(self) -> bool:
        return self._connection_online and not self._closed

    def refresh_sim_identity(self, *, notify: bool = True, expected_generation: int | None = None) -> None:
        if notify:
            self._emit_current_sim_identity()

    def send_command(self, command: str) -> str:
        raise RuntimeError("AT commands are unavailable on a Bluetooth phone line")

    def query_chld_capabilities(self) -> str:
        return "unsupported"

    def hold_toggle(self) -> bool:
        return False

    def merge_calls(self) -> bool:
        return False

    def dial_second(self, number: str) -> str:
        raise RuntimeError("Bluetooth Phase 1 does not support a second call")

    @property
    def phone_call_busy(self) -> bool:
        return bool(self._calls)

    def send_sms(self, number: str, text: str) -> bool:
        return False

    def list_calls(self) -> list[dict[str, object]]:
        return list(self._calls.values())

    def stop_listener(self) -> None:
        self._stop_event.set()
        if self._reader_thread and self._reader_thread is not threading.current_thread():
            self._reader_thread.join(timeout=12)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_listener()
        with self._owner_lock:
            if self.audio_loss_pending:
                self._resolve_audio_loss()
            self.hangup()
        if self._transport is not None:
            self._transport.close()
        self._emit_connection_state(False)
