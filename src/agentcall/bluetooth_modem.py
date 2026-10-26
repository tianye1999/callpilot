"""oFono HFP phone line, Phase 1: outbound AI ownership only."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from queue import Empty, Queue
from typing import Any

from . import config
from .bluetooth_audio import BluetoothAudioBridge
from .modem import SerialModem
from .sim_identity import UNKNOWN_SIM, identify_hfp, with_service_number_override

logger = logging.getLogger(__name__)


class BluetoothModem(SerialModem):
    """Reuse the modem callback contract, never the serial/AT transport.

    Signals are queued off the D-Bus loop: callbacks may synchronously call the
    modem. The listener owns refresh/reconnect; CallSession owns Register/Dial.
    """

    supports_sms = False
    auto_answer = False
    line_kind = "bluetooth"
    POST_ANSWER_SETTLE_SECONDS = 0.0
    AGENT_PATH = "/org/callpilot/HandsfreeAudioAgent"
    HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"

    def __init__(self, address: str, *, transport: Any = None) -> None:
        super().__init__("")
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address):
            raise ValueError("请在本地 .env 配置 BLUETOOTH_PHONE_ADDRESS（手机蓝牙地址）")
        self.address = address.upper()
        self._transport = transport
        self._transport_ready = False
        self._audio_lock = threading.RLock()
        self._owner_lock = threading.RLock()
        self._events: Queue[tuple] = Queue()
        self._stop_event = threading.Event()
        self._modem_path = ""
        self._calls: dict[str, dict] = {}
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
                self.poll_once(time.monotonic())
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
        with self._owner_lock:
            if (self.ai_owned and self.active_since and now >= max(self.active_since + 4, self._audio_retry_at)
                    and not self.audio_bridge.receiving_audio(now) and self._audio_attempts < 3):
                self._audio_retry_at = now + 5
                self._audio_attempts += 1
                self._refresh_cards()
                for card in self._cards:
                    try:
                        self._call(card, "org.ofono.HandsfreeAudioCard", "Connect")
                    except Exception:
                        logger.warning("SCO Connect not ready; bounded retry pending", exc_info=True)

    def _reconnect_phone(self) -> None:
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
        previous = self._calls.get(path, {})
        state = props.get("State", previous.get("State"))
        self._calls[path] = {**previous, **props}
        if state in {"incoming", "waiting"} and previous.get("State") != state and self._on_ring:
            self._on_ring(self._calls[path].get("LineIdentification"))
        if self.ai_owned and path == self._owned_path:
            if state == "active" and not self.active_since:
                self.active_since = time.monotonic()
                self._call_connected_event.set()
                if self._on_call_connected:
                    self._on_call_connected(self._last_dialed)
            elif state == "disconnected":
                self._call_removed(path)

    def _call_removed(self, path: str) -> None:
        self._calls.pop(path, None)
        if self.ai_owned and path == self._owned_path:
            self.end_ai_call()
            if self._on_hangup:
                self._on_hangup()

    def _refresh_cards(self) -> None:
        cards = self._call("/", "org.ofono.HandsfreeAudioManager", "GetCards")[0]
        with self._audio_lock:
            self._cards = {path for path, props in cards if props.get("RemoteAddress", "").upper() == self.address}

    def _accept_audio(self, card: str, fd: int, codec: int) -> bool:
        with self._audio_lock:
            if not self.ai_owned or card not in self._cards or codec != 1:
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
            self._call_connected_event.clear()
            self._last_dialed = number
            self._owned_path = ""
            self.active_since, self._audio_retry_at, self._audio_attempts = 0.0, 0.0, 0
            self.audio_bridge = BluetoothAudioBridge()
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
        raise RuntimeError("Bluetooth Phase 1 does not answer incoming calls")

    def end_ai_call(self) -> None:
        with self._owner_lock:
            with self._audio_lock:
                self.ai_owned = False
            try:
                self.audio_bridge.stop()
            finally:
                self._call_connected_event.clear()
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
            try:
                if self.ai_owned and self._owned_path and self._modem_path:
                    self._call(self._modem_path, "org.ofono.VoiceCallManager", "HangupAll")
            finally:
                self.end_ai_call()

    def send_dtmf(self, digits: str) -> bool:
        if not self.ai_owned or not self.is_call_connected() or not re.fullmatch(r"[0-9*#A-D]+", digits):
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
        self.hangup()
        if self._transport is not None:
            self._transport.close()
        self._emit_connection_state(False)
