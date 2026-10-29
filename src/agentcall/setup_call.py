"""一次性线路自测：不使用拨号队列、预设或自动重试，不改变日常通话策略。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

import numpy as np

from . import config
from .agents.factory import create_agent
from .agents.local_agent import LocalPipelineAgent
from .audio_bridge import AgentPcm, create_audio_bridge
from .bluetooth_modem import BluetoothModem
from .modem import SerialModem

if TYPE_CHECKING:
    from .call_agent import CallSession

logger = logging.getLogger(__name__)
MAX_SECONDS = 45.0
TEST_TASK = (
    "这是一次经用户授权的电话线路自测，不办理任何业务。你已经先听了对方的语音菜单。"
    "现在仅生成一句非常简短的线路测试说明。"
    "不要提问、不要继续对话、不要调用工具，系统会在这句话播放完成后挂断。"
)


@dataclass
class TestEvidence:
    __test__ = False
    uplink_bytes: int = 0
    voiced_samples: int = 0
    written_bytes: int = 0
    write_errors: int = 0
    transcript: bool = False

    def receive(self, pcm: bytes) -> bool:
        self.uplink_bytes += len(pcm)
        samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2").astype(np.float64)
        voiced = bool(samples.size and np.sqrt(np.mean(samples * samples)) >= 30)
        if voiced:
            self.voiced_samples += samples.size
        return voiced

    def result(self) -> dict:
        received = self.transcript or self.voiced_samples >= 2400
        sent = self.written_bytes > 0 and self.write_errors == 0 and self.uplink_bytes > 0
        return {
            "received": {"ok": received, "reason": "asr" if self.transcript else "energy" if received else "no_audio"},
            "sent": {"ok": sent, "reason": "write_error" if self.write_errors else
                     "no_uplink" if not self.uplink_bytes else "written" if sent else "no_write"},
        }


async def run_setup_call(session: CallSession) -> None:
    """45 秒从拨号开始计时；finally 先挂断，再收资源，不让 provider 收尾拖长通话。"""
    evidence = TestEvidence()
    record = None
    agent = None
    bridge = None
    reply = None
    status = "completed"
    expired = threading.Event()
    hangup_failed = threading.Event()
    generation_done = threading.Event()
    accepting_output = threading.Event()
    accepting_output.set()
    queued_bytes = 0
    release_report = (
        session.modem.begin_release_capture()
        if config.get_bool("MODEM_LOG_RELEASE_CAUSE") and isinstance(session.modem, SerialModem)
        and not isinstance(session.modem, BluetoothModem)
        else None
    )
    task = TEST_TASK + f"\n通话语言：{config.get_str('AGENT_LANGUAGE')}。"

    def deadline() -> None:
        # 不依赖 asyncio 取消：模型加载/设备读取阻塞时仍先尝试终止物理线路。
        expired.set()
        accepting_output.clear()
        session._set_active(False)
        try:
            session.modem.hangup()
        except Exception:  # noqa: BLE001
            hangup_failed.set()
            logger.warning("向导自测硬截止挂断失败")

    dial_deadline = time.monotonic() + MAX_SECONDS
    watchdog = threading.Timer(MAX_SECONDS, deadline)
    watchdog.daemon = True
    try:
        if session.call_logger is None:
            raise RuntimeError("自测需要录音存储")
        record = session.call_logger.begin_call("outbound", session._outbound_number, recording_enabled=True)
        session._record = record
        session.setup_test_result = {"state": "running", "number": session._outbound_number}
        watchdog.interval = max(0, dial_deadline - time.monotonic())
        watchdog.start()
        async with asyncio.timeout(max(0, dial_deadline - time.monotonic())):
            if expired.is_set() or not session.is_active:
                return
            await session._threaded_modem_call(partial(
                session.modem.dial, retry=False, deadline=dial_deadline,
            ), session._outbound_number)
            session._publish({"type": "call", "status": "dialing", "caller": session._outbound_number})
            if not await session._wait_connected(timeout=MAX_SECONDS):
                status = "not_connected"
                return
            session._publish({"type": "call", "status": "answered", "caller": session._outbound_number})
            record.log_event("answered")
            await session._threaded_modem_call(session.modem.initialize_for_voice, session.audio_mode)
            if not session.is_active:
                return
            bridge = create_audio_bridge(
                modem=session.modem, mode=session.audio_mode, device_keyword=session.audio_keyword,
                pcm_port=session.pcm_port, pcm_baudrate=session.pcm_baudrate, tx_gain=session.tx_gain,
            )

            def written(pcm: bytes) -> None:
                evidence.written_bytes += len(pcm)
                record.write_downlink(pcm)

            bridge.set_output_observer(written)
            if hasattr(bridge, "set_ready_check"):
                bridge.set_ready_check(session.modem.pcm_ready)
            await session._threaded_modem_call(bridge.start)
            if not session.is_active:
                return
            # 先采集菜单，至少收到 300ms 有声并等到 1s 停顿；不会主动抢开场。
            last_voice = time.monotonic()
            while session.is_active:
                pcm = bridge.read_modem_chunk()
                if pcm:
                    record.write_uplink(pcm)
                    if evidence.receive(pcm):
                        last_voice = time.monotonic()
                if evidence.voiced_samples >= 2400 and time.monotonic() - last_voice >= 1.0:
                    break
                await asyncio.sleep(0.01)
            if not session.is_active:
                return
            agent = create_agent(config.get_str("AGENT_PROVIDER"))
            agent.single_utterance = True
            agent.set_session_instructions(task)
            agent.set_output_complete_handler(generation_done.set)
            # 不向模型继续喂菜单，也不开放工具，只有一次显式 say 生成请求。
            def output(pcm: bytes) -> None:
                nonlocal queued_bytes
                if not accepting_output.is_set() or not session.is_active:
                    return
                converted = bridge.agent_to_modem(pcm, agent.output_rate)
                if hasattr(bridge, "amplify_for_modem"):
                    converted = bridge.amplify_for_modem(converted)
                queued_bytes += len(converted)
                bridge.write_modem_chunks([AgentPcm(converted)])

            def transcript(role: str, text: str) -> None:
                if role == "user" and text.strip():
                    evidence.transcript = True
                record.log_event("transcript", role=role, text=text)

            agent.set_transcript_handler(transcript)
            await agent.start(output)
            if not session.is_active:
                return
            reply = asyncio.create_task(agent.say(task))
            while session.is_active and not agent.fatal:
                pcm = bridge.read_modem_chunk()
                if pcm:
                    record.write_uplink(pcm)
                    evidence.receive(pcm)
                if reply.done():
                    reply.result()
                    if (generation_done.is_set() and bridge.pending_output_bytes() == 0
                            and evidence.written_bytes >= queued_bytes):
                        if agent.output_failed or queued_bytes == 0:
                            status = "failed"
                        break
                await asyncio.sleep(0.01)
    except TimeoutError:
        status = "timeout"
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        logger.warning("向导自测失败: %s", type(exc).__name__)
    finally:
        # 不走普通收尾裁判/短信/摘要，也不改变全局录音配置。
        accepting_output.clear()
        session._set_active(False)
        watchdog.cancel()
        try:
            await session._threaded_modem_call(session.modem.hangup)
        except Exception:  # noqa: BLE001
            status = "hangup_failed"
            logger.warning("向导自测挂断失败", exc_info=True)
        if watchdog.ident is not None:
            await asyncio.to_thread(watchdog.join)
        if expired.is_set():
            status = "hangup_failed" if hangup_failed.is_set() else "timeout"
        if reply is not None:
            reply.cancel()
            await asyncio.gather(reply, return_exceptions=True)
        if agent is not None:
            try:
                if isinstance(agent, LocalPipelineAgent):
                    await agent.stop()
                else:
                    await asyncio.wait_for(agent.stop(), timeout=5)
            except Exception:  # noqa: BLE001
                logger.warning("向导自测模型清理失败")
        if bridge is not None:
            try:
                await session._threaded_modem_call(bridge.stop)
            except Exception:  # noqa: BLE001
                status = "failed"
                logger.warning("向导自测音频桥清理失败")
            evidence.write_errors = getattr(bridge, "output_write_errors", 0)
        result = {"state": "done", "status": status, "number": session._outbound_number, **evidence.result()}
        if release_report is not None:
            causes = await asyncio.to_thread(session.modem.complete_release_capture, release_report)
            if record is not None:
                for cause in causes:
                    record.log_event("release_cause", **cause)
        if record is not None:
            try:
                record.log_event("setup_test", **result)
                record.finish(status)
                # 记录器会吞掉磁盘异常；没有落盘时不能给出无效回放入口。
                if (record.path / "meta.json").is_file() and (record.path / "uplink.wav").is_file():
                    result["call_id"] = record.id
                else:
                    result["recording_error"] = True
            except Exception:  # noqa: BLE001
                result["recording_error"] = True
                logger.warning("向导自测录音保存失败")
        session.setup_test_result = result
        session._publish({"type": "call", "status": "ended", "caller": session._outbound_number})
