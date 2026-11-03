"""Opt-in SIM7600 recovery; all waits and serial work stay off callers.

SIMCom SIM7500_SIM7600 AT Command Manual V3.00: CFUN section 3.2.1,
CEER section 5.2.8, CEREG section 4.2.16. CIREG is not documented there;
LTE registration is NOT proof of IMS registration or voice availability.
https://www.waveshare.net/w/upload/a/af/SIM7500_SIM7600_Series_AT_Command_Manual_V3.00.pdf
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from collections.abc import Callable

from . import config
from .modem import Sim7600Modem, parse_clcc_lines, parse_csq
from .sim_identity import parse_eps_registration

logger = logging.getLogger(__name__)


class Sim7600CallReset:
    _TIMEOUT_SECONDS = 90.0
    _CONNECT_TIMEOUT_SECONDS = 30.0
    _POLL_SECONDS = 0.25
    _NETWORK_WAIT_SECONDS = 30.0
    _NETWORK_COOLDOWN_SECONDS = 600.0

    def __init__(
        self, modem: Sim7600Modem, audio_mode: str, pcm_port: str | None,
        on_state: Callable[[bool], None],
    ) -> None:
        self.modem = modem
        self.audio_mode = audio_mode
        self.ports = [modem.port] + ([pcm_port] if pcm_port else [])
        self._on_state = on_state
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._connector: threading.Thread | None = None
        self._connect_result: bool | None = None
        self._coordinator_done = False
        self._finished = False
        self._started = 0.0
        self._deadline = 0.0
        self._network_mode = False
        self._last_network_reset = float("-inf")
        self._pending_network: threading.Thread | None = None
        modem.on_post_call_reset(self.request)
        modem._on_network_failure = self.request_network_recovery

    def request_network_recovery(self) -> None:
        if not config.get_bool("MODEM_NETWORK_RECOVERY"):
            return
        with self._lock:
            if self._pending_network is not None and self._pending_network.is_alive():
                return
            if time.monotonic() - self._last_network_reset < self._NETWORK_COOLDOWN_SECONDS:
                self._log_phase("network_cooldown")
                return
            self._pending_network = threading.Thread(
                target=self._wait_network_idle, name="sim7600-network-idle", daemon=True,
            )
            self._pending_network.start()

    def _wait_network_idle(self) -> None:
        deadline = time.monotonic() + self._NETWORK_WAIT_SECONDS
        self._log_phase("network_pending reason=no_service_available")
        while not self._stop.is_set() and not self.modem._closed and time.monotonic() < deadline:
            # A post-call reset already owns recovery; never stack two resets.
            if self.modem.recovery_in_progress:
                self._log_phase("network_skipped_existing_recovery")
                return
            runner = self.modem.run_idle_hardware
            try:
                if runner is not None and runner(self._claim_network_reset):
                    return
            except Exception as exc:  # noqa: BLE001
                logger.warning("SIM7600 network recovery claim failed: %s", type(exc).__name__)
                return
            self._stop.wait(self._POLL_SECONDS)
        self._log_phase("network_idle_timeout")

    def _claim_network_reset(self) -> None:
        with self.modem._serial_lock:
            if self._stop.is_set() or self.modem._closed or not self.modem.is_connected():
                return
            calls = self.modem._write_command("AT+CLCC")
            if "OK" not in calls or parse_clcc_lines(calls) or self.modem.is_call_connected():
                self._log_phase("network_skipped_line_busy")
                return
            self.modem._reset_in_progress = True
            self.modem._maintenance_generation += 1
            with self.modem._sim_refresh_lock:
                self.modem._sim_refresh_generation += 1
                self.modem._sim_refresh_requested_generation = None
            self._last_network_reset = time.monotonic()
        self.request(network=True)

    def request(self, *, network: bool = False) -> None:
        # The modem has already claimed reset ownership atomically with hangup.
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._finished:
                return
            self._started = time.monotonic()
            self._network_mode = network
            self._deadline = self._started + self._TIMEOUT_SECONDS
            self._connector = None
            self._connect_result = None
            self._coordinator_done = False
            self._finished = False
            self._thread = threading.Thread(target=self._run, name="sim7600-call-reset", daemon=True)
        self.modem._emit_connection_state(False)
        self._on_state(False)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def _log_phase(self, phase: str) -> None:
        logger.info(
            "SIM7600 recovery mode=%s phase=%s elapsed_seconds=%.3f",
            "network" if self._network_mode else "post_call", phase,
            time.monotonic() - self._started if self._started else 0,
        )

    def _ports_ready(self) -> bool:
        return all(os.path.exists(port) for port in self.ports)

    def _ports_gone(self) -> bool:
        return all(not os.path.exists(port) for port in self.ports)

    def _wait_for_ports(self, present: bool) -> None:
        samples = 0
        while not self._stop.is_set() and self._remaining() > 0:
            matches = self._ports_ready() if present else self._ports_gone()
            samples = samples + 1 if matches else 0
            if samples >= (2 if present else 1):
                return  # Two consecutive presence samples avoid opening transient links.
            self._stop.wait(min(self._POLL_SECONDS, self._remaining()))
        raise TimeoutError("端口消失/恢复等待超时或服务停止")

    def _run(self) -> None:
        success = False
        try:
            if self._stop.is_set() or self.modem._closed:
                return
            if len(self.ports) != 2 or not all(port.startswith("/") for port in self.ports):
                raise ValueError("自动重启需要稳定的 AT/PCM 设备路径")
            if not self.modem._reconnect_complete.wait(timeout=self._remaining()):
                raise TimeoutError("已有串口重连尚未退出")
            with self.modem.reset_transport_access():
                if self._network_mode and self._soft_network_reset():
                    success = True
                    return
                if self._stop.is_set() or self.modem._closed:
                    raise RuntimeError("service stopping")
                self._log_phase("reset_sent")
                # Never use _send: its automatic retry could reboot the new device twice.
                try:
                    command = "AT+CFUN=1,1" if self._network_mode else "AT+CRESET"
                    response = self.modem._write_command(command)
                    if "OK" not in response:
                        logger.warning("%s 未返回 OK，仍等待重枚举确认，不重发", command)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("模组重启响应不确定 (%s)，仍等待重枚举，不重发", type(exc).__name__)
                finally:
                    self._close_transport()
            self._wait_for_ports(False)
            self._log_phase("ports_gone")
            self._wait_for_ports(True)
            self._log_phase("ports_back")
            while not self._stop.is_set() and self._remaining() > 0:
                if self._connect_bounded():
                    success = True
                    return
                self._stop.wait(min(self._POLL_SECONDS, self._remaining()))
            raise TimeoutError("模组未在恢复窗口内重新可用")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "SIM7600 #121 自动重启未恢复，交回看门狗: %s; elapsed_seconds=%.3f",
                exc, time.monotonic() - self._started,
            )
        finally:
            with self._lock:
                self._coordinator_done = True
                pending = self._connector is not None and self._connect_result is None
            if pending:
                logger.error("SIM7600 重连 worker 仍未退出，保留串口占用；看门狗不得重复重连")
            else:
                self._finish(success)

    def _soft_network_reset(self) -> bool:
        self._log_phase("network_cfun_off_on")
        try:
            for command in ("AT+CFUN=0", "AT+CFUN=1"):
                if self._stop.is_set():
                    raise RuntimeError("service stopping")
                if "OK" not in self.modem._write_command(command):
                    raise RuntimeError("CFUN rejected")
            deadline = min(self._deadline, time.monotonic() + self._NETWORK_WAIT_SECONDS)
            while not self._stop.is_set() and time.monotonic() < deadline:
                if self._network_ready():
                    self.modem._write_command("AT+CREG=1")
                    self.modem._write_command("AT+CEREG=1")
                    self.modem.refresh_sim_identity()
                    self._log_phase("network_soft_ready")
                    return True
                self._stop.wait(min(self._POLL_SECONDS, max(0, deadline - time.monotonic())))
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIM7600 network soft recovery failed: %s", type(exc).__name__)
        if self._stop.is_set():
            raise RuntimeError("service stopping")
        self._log_phase("network_escalate_cfun_reboot")
        return False

    def _connect_bounded(self) -> bool:
        with self._lock:
            if self._connector is None:
                self._connector = threading.Thread(
                    target=self._connect, name="sim7600-reset-reconnect", daemon=True,
                )
                self._connector.start()
            worker = self._connector
        worker.join(timeout=min(self._CONNECT_TIMEOUT_SECONDS, self._remaining()))
        if worker.is_alive():
            return False  # Join timeout is not cancellation; retain this exact worker.
        with self._lock:
            result = self._connect_result
            self._connector = None
            self._connect_result = None
        return bool(result)

    def _connect(self) -> None:
        success = False
        try:
            with self.modem.reset_transport_access():
                self.modem.connect()  # Normal AT/SMS initialization and SIM identity refresh.
                if self._stop.is_set() or self.modem._closed or not self._remaining():
                    return
                self.modem.initialize_for_voice(self.audio_mode)
                while not self._stop.is_set() and not self.modem._closed and self._remaining() > 0:
                    if self._network_ready():
                        self.modem.refresh_sim_identity()
                        if self._stop.is_set() or self.modem._closed or not self._remaining():
                            return
                        self.modem.start_listener()
                        success = True
                        return
                    self._stop.wait(min(self._POLL_SECONDS, self._remaining()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIM7600 重连尝试失败: error_type=%s", type(exc).__name__)
        finally:
            if not success:
                self._close_transport()
            with self._lock:
                self._connect_result = success
                orphaned = self._coordinator_done
            if orphaned:
                self._finish(success)

    def _network_ready(self) -> bool:
        if "OK" not in self.modem._write_command("AT"):
            return False
        if "+CPIN: READY" not in self.modem._write_command("AT+CPIN?"):
            return False
        eps = self.modem._write_command("AT+CEREG?")
        if self._network_mode:
            csq, _level = parse_csq(self.modem._write_command("AT+CSQ"))
            return parse_eps_registration(eps)[0] and csq is not None and csq != 99
        if self._registered(eps, "+CEREG:"):
            return True
        cs = self.modem._write_command("AT+CREG?")
        return self._registered(cs, "+CREG:")

    @staticmethod
    def _registered(response: str, prefix: str) -> bool:
        for line in reversed(response.splitlines()):
            if not line.strip().startswith(prefix):
                continue
            fields = next(csv.reader([line.strip().removeprefix(prefix)], skipinitialspace=True))
            # Queries return <n>,<stat>; a short unsolicited response is just <stat>.
            status = fields[1] if len(fields) > 1 else fields[0] if fields else ""
            return status.strip() in {"1", "5"}
        return False

    def _finish(self, success: bool) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        success = success and not self._stop.is_set() and not self.modem._closed
        if not success:
            self._log_phase("failed")
            self._close_transport()
        self.modem._reset_in_progress = False
        if success:
            self.modem._emit_connection_state(True)
            self._log_phase("ready")
        self._on_state(success)

    def _close_transport(self) -> None:
        try:
            self.modem.close_reset_transport()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIM7600 释放串口异常: error_type=%s", type(exc).__name__)
