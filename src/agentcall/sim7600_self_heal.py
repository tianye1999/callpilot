"""Windows / 无桥平台 SIM7600 服务内自愈。

模组劣化（AT+CPCMREG=1 需多次重试）在通话结束后触发。macOS 由 USB 桥消费
哨兵文件执行组合切换；Windows 没有桥，也没有 POSIX /tmp，改由服务在**线路
空闲**时自行执行：关 PCM 口 -> AT+CFUN=1,1 -> 释放串口 -> 等 AT/PCM COM 口
重枚举（自动探测，90s）-> 重连 -> 等 CREG/CEREG 注册 -> 记录结果。

CFUN 选择：SIMCom 手册里 ``AT+CFUN=<fun>[,<rst>]`` 的 ``rst=1`` 表示先复位
再进入指定功能级。因此 ``AT+CFUN=1,1`` 等价于「先 CFUN=0 再 CFUN=1」，但单条
命令原子、不会出现复位前网络侧反注册的中间态；真机手动恢复也用该命令
（COM5 发 AT+CFUN=1,1 后约 14s COM 重现）。串口重连 + initialize_for_voice +
start_listener 成功即视为 transport 已恢复；网络注册（CREG/CEREG）在后台继续
等待（上限 180s），注册超时只告警，不关串口也不判整体失败。若后续真机发现
CFUN=1,1 不足以清掉 CSQ=99/CEREG 0,4 这类网络级劣化，再升级为 AT+CRESET
（参考 sim7600_reset 的 CRESET 路径），不在本路径默认开启。
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from collections.abc import Callable

from .modem import Sim7600Modem

logger = logging.getLogger(__name__)


class Sim7600SelfHeal:
    """空闲时执行 CFUN 软复位的服务内自愈；通话中绝不执行。"""

    _PORT_WAIT_SECONDS = 90.0
    _NETWORK_WAIT_SECONDS = 180.0
    _COOLDOWN_SECONDS = 600.0
    _IDLE_WAIT_SECONDS = 30.0
    _POLL_SECONDS = 0.5

    def __init__(
        self,
        modem: Sim7600Modem,
        audio_mode: str,
        pcm_port: str | None,
        detect_ports: Callable[[], tuple[str | None, str | None]],
        close_pcm: Callable[[], None],
        on_result: Callable[[dict], None],
    ) -> None:
        self.modem = modem
        self.audio_mode = audio_mode
        self.pcm_port = pcm_port
        self._detect_ports = detect_ports
        self._close_pcm = close_pcm
        self._on_result = on_result
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_attempt = float("-inf")
        self._active = False
        self._started = 0.0

    @property
    def in_progress(self) -> bool:
        return self._active

    def request(self, reason: str) -> bool:
        """触发一次自愈；运行中/10 分钟节流内直接拒绝，不排队。"""
        with self._lock:
            if self._active or (self._thread is not None and self._thread.is_alive()):
                return False
            now = time.monotonic()
            if now - self._last_attempt < self._COOLDOWN_SECONDS:
                logger.info("SIM7600 自愈节流，跳过: reason=%s", reason)
                return False
            self._last_attempt = now
            self._active = True
            self._thread = threading.Thread(
                target=self._run, args=(reason,), name="sim7600-self-heal", daemon=True,
            )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _log_phase(self, result: dict, phase: str) -> None:
        result.setdefault("phases", []).append(phase)
        logger.info(
            "SIM7600 self_heal phase=%s elapsed_seconds=%.3f",
            phase, time.monotonic() - self._started if self._started else 0,
        )

    def _run(self, reason: str) -> None:
        result: dict = {"ok": False, "reason": reason, "phases": []}
        self._started = time.monotonic()
        try:
            if self._stop.is_set() or self.modem._closed:
                result["error"] = "service_stopping"
                return
            runner = self.modem.run_idle_hardware
            if runner is None:
                result["error"] = "no_idle_runner"
                return
            deadline = time.monotonic() + self._IDLE_WAIT_SECONDS
            while not self._stop.is_set() and not self.modem._closed and time.monotonic() < deadline:
                try:
                    claimed = bool(runner(lambda: self._heal(result)))
                except Exception as exc:  # noqa: BLE001
                    result["error"] = f"idle_claim_failed:{type(exc).__name__}"
                    return
                if claimed:
                    return
                self._stop.wait(self._POLL_SECONDS)
            result["error"] = "line_busy"
        except Exception as exc:  # noqa: BLE001
            result["error"] = type(exc).__name__
        finally:
            result["elapsed_seconds"] = round(time.monotonic() - self._started, 3)
            self._active = False
            logger.info("SIM7600 自愈结果: %s", result)
            try:
                self._on_result(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SIM7600 自愈结果回调异常: %s", type(exc).__name__)

    def _heal(self, result: dict) -> None:
        """完整自愈序列；异常在此兜底，绝不抛穿 idle runner。"""
        success = False
        try:
            self._log_phase(result, "close_pcm")
            self._close_pcm()
            self._log_phase(result, "claim")
            with self.modem.reset_transport_access():
                self.modem._reset_in_progress = True
                self.modem._maintenance_generation += 1
                with self.modem._sim_refresh_lock:
                    self.modem._sim_refresh_generation += 1
                    self.modem._sim_refresh_requested_generation = None
                self._log_phase(result, "cfun")
                response = self.modem._write_command("AT+CFUN=1,1")
                if "OK" not in response:
                    logger.warning("AT+CFUN=1,1 未返回 OK，仍等待重枚举，不重发")
                self._log_phase(result, "release_serial")
                self.modem.close_reset_transport()
            self._log_phase(result, "ports_gone")
            self._wait_for_ports(False)
            self._log_phase(result, "ports_back")
            at_port, pcm_port = self._wait_for_ports(True)
            if at_port:
                self.modem.port = at_port
            if pcm_port:
                self.pcm_port = pcm_port
            result["at_port"] = at_port
            result["pcm_port"] = pcm_port
            self._log_phase(result, "reconnect")
            with self.modem.reset_transport_access():
                if not self._reconnect_transport(result):
                    raise TimeoutError("模组未在自愈窗口内重新可用")
                success = True
                self._wait_for_registration(result)
            result["ok"] = True
            self._log_phase(result, "ready")
        except Exception as exc:  # noqa: BLE001
            result["error"] = type(exc).__name__
            logger.warning("SIM7600 自愈执行失败: error=%s %s", type(exc).__name__, exc)
        finally:
            self.modem._reset_in_progress = False
            if success:
                self.modem._emit_connection_state(True)
            else:
                self._close_transport()

    def _ports_gone(self) -> bool:
        at, _ = self._detect_ports()
        return at is None

    def _ports_ready(self) -> tuple[str | None, str | None] | None:
        at, pcm = self._detect_ports()
        if at is None:
            return None
        if self.pcm_port is not None and pcm is None:
            return None
        return at, pcm

    def _wait_for_ports(self, present: bool) -> tuple[str | None, str | None]:
        deadline = time.monotonic() + self._PORT_WAIT_SECONDS
        samples = 0
        while not self._stop.is_set() and time.monotonic() < deadline:
            if present:
                ready = self._ports_ready()
                if ready is not None:
                    samples += 1
                    if samples >= 2:
                        return ready
                else:
                    samples = 0
            else:
                samples = samples + 1 if self._ports_gone() else 0
                if samples >= 1:
                    return (None, None)
            self._stop.wait(self._POLL_SECONDS)
        raise TimeoutError("端口消失/恢复等待超时或服务停止")

    def _reconnect_transport(self, result: dict) -> bool:
        started = time.monotonic()
        success = False
        try:
            self.modem.connect()
            self.modem.initialize_for_voice(self.audio_mode)
            self.modem.start_listener()
            success = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIM7600 自愈串口重连失败: %s", type(exc).__name__)
            return False
        finally:
            result["transport_ok"] = success
            result["transport_elapsed_seconds"] = round(time.monotonic() - started, 3)
            if not success:
                self._close_transport()

    def _wait_for_registration(self, result: dict) -> None:
        started = time.monotonic()
        deadline = time.monotonic() + self._NETWORK_WAIT_SECONDS
        registered = False
        while not self._stop.is_set() and not self.modem._closed and time.monotonic() < deadline:
            try:
                if self._network_ready():
                    self.modem.refresh_sim_identity()
                    registered = True
                    break
            except Exception as exc:  # noqa: BLE001
                logger.warning("SIM7600 自愈注册探测失败: %s", type(exc).__name__)
            self._stop.wait(self._POLL_SECONDS)
        result["registered"] = registered
        result["registration_elapsed_seconds"] = round(time.monotonic() - started, 3)
        if registered:
            self._log_phase(result, "registered")
        else:
            logger.warning(
                "SIM7600 自愈 transport 已恢复，但 %.0fs 内未完成网络注册，保持串口打开",
                self._NETWORK_WAIT_SECONDS,
            )

    def _network_ready(self) -> bool:
        if "OK" not in self.modem._write_command("AT"):
            return False
        if "+CPIN: READY" not in self.modem._write_command("AT+CPIN?"):
            return False
        eps = self.modem._write_command("AT+CEREG?")
        if self._registered(eps, "+CEREG:"):
            return True
        creg = self.modem._write_command("AT+CREG?")
        return self._registered(creg, "+CREG:")

    @staticmethod
    def _registered(response: str, prefix: str) -> bool:
        for line in reversed(response.splitlines()):
            if not line.strip().startswith(prefix):
                continue
            fields = next(csv.reader([line.strip().removeprefix(prefix)], skipinitialspace=True))
            status = fields[1] if len(fields) > 1 else fields[0] if fields else ""
            return status.strip() in {"1", "5"}
        return False

    def _close_transport(self) -> None:
        try:
            self.modem.close_reset_transport()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIM7600 自愈释放串口异常: %s", type(exc).__name__)
