"""Optional Linux D-Bus adapter; no GLib or system Python bindings required.

Low-level messages keep fd ownership explicit: an `h` body value indexes
Message.unix_fds. All synchronous consumers run outside the asyncio bus thread.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Callable


class OfonoCallError(RuntimeError):
    """Keep daemon detail for classification without leaking it into logs."""

    def __init__(self, error_name: str, detail: str, *, operation: str = "") -> None:
        super().__init__(f"{operation}: {error_name}" if operation else error_name)
        self.error_name = error_name
        self.detail = detail


class OfonoDBus:
    def __init__(self, agent_path: str) -> None:
        self.agent_path = agent_path
        self._loop = asyncio.new_event_loop()
        self._thread: threading.Thread | None = None
        self._bus: Any = None
        self._owner = ""

    def start(self, signal: Callable, audio: Callable) -> None:
        if self._thread is not None:
            return
        if self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        # Imported only by a Linux BluetoothModem connection, never by app import.
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        self._Message = Message
        self._MessageType = MessageType
        self._signal, self._audio = signal, audio

        async def connect() -> None:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
            self._bus.add_message_handler(self._handle_message)
            for rule in (
                "type='signal',sender='org.ofono'",
                "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
                "member='NameOwnerChanged',arg0='org.ofono'",
            ):
                await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                 "AddMatch", "s", [rule])
            await self._resolve_owner()

        self._thread = threading.Thread(target=self._loop.run_forever, name="ofono-dbus", daemon=True)
        self._thread.start()
        pending = asyncio.run_coroutine_threadsafe(connect(), self._loop)
        try:
            pending.result(timeout=15)
        except BaseException:
            pending.cancel()
            self.close()
            raise

    async def _resolve_owner(self) -> None:
        reply = await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                 "GetNameOwner", "s", ["org.ofono"])
        self._owner = reply[0]

    @staticmethod
    def _unwrap(value: Any) -> Any:
        if hasattr(value, "signature") and hasattr(value, "value"):
            return OfonoDBus._unwrap(value.value)
        if isinstance(value, dict):
            return {k: OfonoDBus._unwrap(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [OfonoDBus._unwrap(v) for v in value]
        return value

    def _handle_message(self, message: Any) -> Any:
        Message, MessageType = self._Message, self._MessageType
        if message.message_type == MessageType.SIGNAL:
            if (message.sender == "org.freedesktop.DBus" and message.member == "NameOwnerChanged"
                    and message.body[0] == "org.ofono"):
                self._owner = message.body[2]
                self._signal("/", "org.ofono.Manager", "ServiceChanged", [])
            elif self._owner and message.sender == self._owner:
                self._signal(message.path, message.interface, message.member, self._unwrap(message.body))
            return None
        if (message.message_type != MessageType.METHOD_CALL or message.path != self.agent_path
                or message.interface != "org.ofono.HandsfreeAudioAgent"):
            return None
        fds = list(message.unix_fds)
        try:
            if not self._owner or message.sender != self._owner:
                return Message.new_error(message, "org.ofono.Error.NotAllowed", "Not oFono")
            if message.member == "Release" and message.signature == "":
                self._signal("/", message.interface, "Release", [])
                return Message.new_method_return(message)
            if message.member != "NewConnection" or message.signature != "ohy":
                return Message.new_error(message, "org.ofono.Error.InvalidArguments", "Expected ohy")
            card, index, codec = message.body
            if index < 0 or index >= len(fds):
                return Message.new_error(message, "org.ofono.Error.InvalidArguments", "Missing SCO fd")
            fd = fds.pop(index)
            if not self._audio(card, fd, codec):
                return Message.new_error(message, "org.ofono.Error.Rejected", "No AI audio owner")
            return Message.new_method_return(message)
        finally:
            for fd in fds:
                os.close(fd)

    async def _call(self, destination: str, path: str, interface: str, member: str,
                    signature: str = "", body: list | None = None) -> list:
        reply = await asyncio.wait_for(self._bus.call(self._Message(
            destination=destination, path=path, interface=interface, member=member,
            signature=signature, body=body or [],
        )), timeout=10)
        if reply.message_type == self._MessageType.ERROR:
            error_body = getattr(reply, "body", [])
            raise OfonoCallError(reply.error_name, str(error_body[0]) if error_body else "",
                                 operation=f"{interface}.{member}")
        return self._unwrap(reply.body)

    def call(self, destination: str, path: str, interface: str, member: str,
             signature: str = "", body: list | None = None) -> list:
        pending = asyncio.run_coroutine_threadsafe(
            self._call(destination, path, interface, member, signature, body), self._loop,
        )
        try:
            return pending.result(timeout=12)
        except BaseException:
            pending.cancel()
            raise

    def close(self) -> None:
        self._owner = ""
        if self._thread is not None:
            async def disconnect() -> None:
                if self._bus is not None:
                    self._bus.disconnect()
                    await self._bus.wait_for_disconnect()
                tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            try:
                asyncio.run_coroutine_threadsafe(disconnect(), self._loop).result(timeout=3)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=2)
                if not self._thread.is_alive():
                    self._loop.close()
                self._thread = None
        elif not self._loop.is_closed():
            self._loop.close()
