"""Exercise real parallel startup and duplex gating without devices or network."""

from __future__ import annotations

import asyncio
import threading
import time
from array import array

import pytest
from fakes import FakeAgent, FakeModem

from agentcall.audio_bridge import SerialPcmAudioBridge
from agentcall.call_agent import CallSession


@pytest.mark.parametrize("overlap", [False, True], ids=["separate", "overlapping"])
@pytest.mark.parametrize("partial_padding", [False, True], ids=["full-frames", "padded-tail"])
@pytest.mark.parametrize("streaming_opening", [False, True], ids=["opening-returned", "opening-streaming"])
def test_parallel_serial_playout_resumes_uplink_after_idle_filler(
    monkeypatch, overlap, partial_padding, streaming_opening,
):
    for key, value in {
        "PARALLEL_AGENT_CONNECT": "true",
        "BARGE_IN_ENABLED": "false",
        "HALF_DUPLEX_HANGOVER_SECONDS": "0.03",
        "INBOUND_TRIAGE_MODE": "off",
        "INBOUND_TAKEOVER_ENABLED": "false",
        "INBOUND_MAX_SECONDS": "60",
        "AGENT_MAX_TURN_SECONDS": "10",
        "AGENT_UPLINK_GAIN": "1",
        "MONITOR_UPLINK_GAIN": "1",
        "MODEM_ANSWER_GREETING": "",
    }.items():
        monkeypatch.setenv(key, value)

    async def run():
        modem = FakeModem()
        modem.POST_ANSWER_SETTLE_SECONDS = 0
        session = CallSession(modem, "unused", "openai", "nmea", "unused", 115200, 1.0)
        session._set_active(True)
        monkeypatch.setattr(session, "_start_dtmf_judge", lambda *args, **kwargs: None)
        bridge = SerialPcmAudioBridge("unused", write_size=64, write_interval=0.004)
        first_pcm = b"\x00\x10" * 768
        second_pcm = b"\x00\x20" * (3 * (128 + (5 if partial_padding else 0)))
        agent = FakeAgent(reply_pcm=first_pcm)
        expected = b"".join(bridge.agent_to_modem(pcm, agent.output_rate) for pcm in (first_pcm, second_pcm))
        first_size = len(bridge.agent_to_modem(first_pcm, agent.output_rate))
        silence = bytes(bridge._write_size)
        uplink = array("h", [(index % 128) * 64 - 4096 for index in range(320)]).tobytes()
        observed = bytearray()
        writes = []
        forwarded = []
        state_lock = threading.Lock()
        first_agent_write = threading.Event()
        release_agent_write = threading.Event()
        first_loop_read = asyncio.Event()
        release_opening = asyncio.Event()
        opening_pending = False
        opening_at_first_read = []
        if not overlap:
            release_agent_write.set()

        class MemorySerial:
            is_open = True

            def reset_input_buffer(self):
                pass

            def reset_output_buffer(self):
                pass

            def close(self):
                self.is_open = False

            @property
            def in_waiting(self):
                return 0

            def read(self, _count):
                if not first_loop_read.is_set():
                    opening_at_first_read.append(opening_pending)
                    first_loop_read.set()
                return uplink

            def write(self, pcm):
                if any(pcm) and not first_agent_write.is_set():
                    first_agent_write.set()
                    if not release_agent_write.wait(timeout=2):
                        raise TimeoutError("Test did not release the fake serial writer")
                with state_lock:
                    writes.append(bytes(pcm))
                return len(pcm)

        port = MemorySerial()
        monkeypatch.setattr(bridge, "_open_serial", lambda: port)
        original_set_observer = bridge.set_output_observer

        def set_observer(callback):
            def observe(pcm):
                callback(pcm)
                with state_lock:
                    observed.extend(pcm)

            original_set_observer(observe)

        monkeypatch.setattr(bridge, "set_output_observer", set_observer)

        async def say(_text):
            nonlocal opening_pending
            opening_pending = True
            agent._on_audio_out(first_pcm)
            if streaming_opening:
                await asyncio.wait_for(release_opening.wait(), timeout=2)
            opening_pending = False

        async def send_audio(_pcm):
            forwarded.append(time.monotonic())

        monkeypatch.setattr(agent, "say", say)
        monkeypatch.setattr(agent, "send_audio", send_audio)
        monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _provider: agent)
        monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kwargs: bridge)
        call_task = asyncio.create_task(session._handle_call())

        def observed_bytes():
            with state_lock:
                return bytes(observed)

        def filler_count():
            with state_lock:
                return writes.count(silence)

        async def wait_until(predicate):
            deadline = time.monotonic() + 2
            while not predicate():
                if call_task.done():
                    call_task.result()
                    pytest.fail("Call ended before the expected media boundary")
                assert time.monotonic() < deadline, "Timed out waiting for the expected media boundary"
                await asyncio.sleep(0.001)

        try:
            await wait_until(first_loop_read.is_set)
            assert opening_at_first_read == [streaming_opening]
            if overlap:
                await wait_until(first_agent_write.is_set)
                assert bridge.pending_output_bytes() > 0
            else:
                await wait_until(lambda: len(observed_bytes()) >= first_size)
                assert bridge.pending_output_bytes() == 0
            assert opening_pending is streaming_opening
            agent._on_audio_out(second_pcm)
            release_agent_write.set()
            release_opening.set()

            await wait_until(lambda: len(observed_bytes()) >= len(expected))
            assert observed_bytes() == expected
            await wait_until(lambda: bridge.pending_output_bytes() == 0 and session._outgoing_audio.empty())
            last_play_at = session._last_downlink_play_at
            initial_filler = filler_count()
            await wait_until(lambda: any(stamp > last_play_at for stamp in forwarded))
            first_resume = next(stamp for stamp in forwarded if stamp > last_play_at)
            assert session._hangover_seconds <= first_resume - last_play_at <= session._hangover_seconds + 0.5
            await wait_until(lambda: filler_count() >= initial_filler + 5)
            assert session._last_downlink_play_at == last_play_at
            assert observed_bytes() == expected
            assert not bridge._output_spans
            with state_lock:
                audio_writes = [pcm for pcm in writes if pcm != silence]
            padding = (-len(expected)) % bridge._write_size
            assert bool(padding) is partial_padding
            if padding:
                assert audio_writes[-1] == expected[-(bridge._write_size - padding):] + bytes(padding)
        finally:
            release_agent_write.set()
            release_opening.set()
            session._set_active(False)
            try:
                await asyncio.wait_for(call_task, timeout=2)
            finally:
                bridge.stop()
                if bridge._writer_thread is not None:
                    assert not bridge._writer_thread.is_alive()

    asyncio.run(run())
