"""Deterministic 16 kB/s PCM source; no devices, AT commands or sleep assertions."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest
import serial
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall import config
from agentcall.audio_bridge import SerialPcmAudioBridge
from agentcall.call_agent import CallSession


class TtyPcmSource:
    """4095B tty FIFO, advanced in acknowledged 20ms/320B virtual-time ticks."""

    def __init__(self):
        self.is_open = True
        self.buffer = bytearray()
        self.cv = threading.Condition()
        self.read_bytes = 0
        self.ack_bytes = 0
        self.lost_bytes = 0
        self.readers = set()
        self.active_reads = 0
        self.max_readers = 0
        self.read_entered = threading.Event()
        self.release_read = threading.Event()
        self.release_read.set()
        self.block_reads = False
        self.read_blocked = threading.Event()
        self.fail = False

    def reset_input_buffer(self):
        self.buffer.clear()

    def reset_output_buffer(self):
        pass

    @property
    def in_waiting(self):
        with self.cv:
            return len(self.buffer)

    def feed(self, data):
        with self.cv:
            accepted = min(len(data), 4095 - len(self.buffer))
            self.buffer.extend(data[:accepted])
            self.lost_bytes += len(data) - accepted
            self.cv.notify_all()

    def wait_drained(self, count):
        # A new read acknowledges that the previous read has been buffered by the bridge.
        with self.cv:
            assert self.cv.wait_for(lambda: self.ack_bytes >= count, timeout=5)

    def read(self, count):
        with self.cv:
            self.active_reads += 1
            self.max_readers = max(self.max_readers, self.active_reads)
            self.readers.add(threading.current_thread().name)
            self.ack_bytes = self.read_bytes
            block = self.block_reads
            self.cv.notify_all()
        try:
            self.read_entered.set()
            if block:
                self.read_blocked.set()
                assert self.release_read.wait(timeout=5)
            with self.cv:
                self.cv.wait_for(lambda: self.buffer or not self.is_open or self.fail, timeout=0.02)
                if self.fail:
                    raise serial.SerialException("fake PCM disconnect")
                result = bytes(self.buffer[:count])
                del self.buffer[:len(result)]
                self.read_bytes += len(result)
                return result
        finally:
            with self.cv:
                self.active_reads -= 1

    def write(self, data):
        return len(data)

    def close(self):
        with self.cv:
            self.is_open = False
            self.cv.notify_all()


def start_bridge(monkeypatch, enabled):
    monkeypatch.setenv("MODEM_PCM_UPLINK_PREBUFFER", str(enabled).lower())
    source = TtyPcmSource()
    bridge = SerialPcmAudioBridge("fake-pcm")
    monkeypatch.setattr(bridge, "_open_serial", lambda: source)
    bridge.start()
    return bridge, source


def feed_ticks(source, ticks, *, acknowledged):
    # Low-amplitude distinct samples cannot spuriously trigger alignment correction.
    data = bytes(value for index in range(ticks * 160) for value in (index % 128, 0))
    for offset in range(0, len(data), 320):
        source.feed(data[offset:offset + 320])
        if acknowledged:
            source.wait_drained(offset + 320)
    return data


def read_all(bridge, size):
    result = bytearray()
    for _ in range(size // 640 + 3):
        if len(result) >= size:
            break
        result.extend(bridge.read_modem_chunk())
    assert len(result) == size
    return bytes(result)


def test_prebuffer_config_defaults_false_on_all_platforms(monkeypatch):
    monkeypatch.delenv("MODEM_PCM_UPLINK_PREBUFFER", raising=False)
    spec = config.get_spec("MODEM_PCM_UPLINK_PREBUFFER")
    assert spec.kind == "bool" and spec.default == "false"
    assert config.get_bool(spec.key) is False
    root = Path(__file__).resolve().parents[2]
    for path in (root / ".env.example", root / "scripts/linux/.env.example"):
        assert "MODEM_PCM_UPLINK_PREBUFFER=false" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("enabled, lost", [(False, 19905), (True, 0)])
def test_first_read_after_1500ms_loss_comparison(monkeypatch, caplog, enabled, lost):
    bridge, source = start_bridge(monkeypatch, enabled)
    caplog.set_level(logging.INFO)
    try:
        data = feed_ticks(source, 75, acknowledged=enabled)  # 1.5s * 16000B/s = 24000B
        if not enabled:
            assert source.readers == set()  # No drain thread or serial reads when disabled.
            assert bridge._prebuffer_thread is None
        # Disabled tty contains 4095B: the last odd byte remains in the existing carry.
        result = bridge.read_modem_chunk() if enabled else read_all(bridge, 4094)
        assert len(result) == (24000 if enabled else 4094)
        assert result == data[:len(result)]
        assert source.lost_bytes == lost
        assert source.max_readers == 1
        if enabled:
            assert "[timing] 上行预缓冲: bytes=24000(≈1500ms) dropped=0" in caplog.text
            assert not bridge._prebuffer_thread.is_alive()
        else:
            assert bridge._rx_carry == data[4094:4095]
            assert "上行预缓冲" not in caplog.text
    finally:
        bridge.stop()


def test_overflow_discards_oldest_whole_samples(monkeypatch, caplog):
    bridge, source = start_bridge(monkeypatch, True)
    caplog.set_level(logging.INFO)
    try:
        data = feed_ticks(source, 200, acknowledged=True)  # 4s -> retain most recent 3s
        assert len(bridge._prebuffer) == 48000
        assert bridge._prebuffer_dropped == 16000
        assert read_all(bridge, 48000) == data[16000:]
        source.feed(b"\x01\x00\x02\x00")
        assert bridge.read_modem_chunk() == b"\x01\x00\x02\x00"
        assert source.max_readers == 1
        assert "bytes=48000(≈3000ms) dropped=16000" in caplog.text
    finally:
        bridge.stop()


@pytest.mark.parametrize("operation", ["first_read", "stop"])
def test_handoff_and_stop_join_inflight_reader(monkeypatch, operation):
    bridge, source = start_bridge(monkeypatch, True)
    data = feed_ticks(source, 1, acknowledged=True)
    with source.cv:
        source.release_read.clear()
        source.block_reads = True
    assert source.read_blocked.wait(timeout=5)
    results = []

    def act():
        results.append(bridge.read_modem_chunk() if operation == "first_read" else bridge.stop())

    caller = threading.Thread(target=act, name="test-main-reader")
    try:
        caller.start()
        assert bridge._prebuffer_stop.wait(timeout=5)
        assert source.readers == {"pcm-uplink-prebuffer"}
        assert source.is_open
        source.release_read.set()
        caller.join(timeout=5)
        assert not caller.is_alive()
        assert not bridge._prebuffer_thread.is_alive()
        assert source.max_readers == 1
        if operation == "first_read":
            assert results == [data]
        else:
            assert results == [None] and not source.is_open
            assert bridge.read_modem_chunk() == b""
    finally:
        source.release_read.set()
        caller.join(timeout=5)
        bridge.stop()


def test_stop_after_prebuffer_serial_error_is_clean(monkeypatch):
    bridge, source = start_bridge(monkeypatch, True)
    with source.cv:
        source.fail = True
        source.cv.notify_all()
    bridge._prebuffer_thread.join(timeout=5)
    assert not bridge._prebuffer_thread.is_alive()
    bridge.stop()
    bridge.stop()
    assert not source.is_open
    # Reusing a stopped bridge with the flag off must not rethrow the old error.
    monkeypatch.setenv("MODEM_PCM_UPLINK_PREBUFFER", "false")
    source.fail = False
    source.is_open = True
    bridge.start()
    try:
        source.feed(b"\x01\x00")
        assert bridge.read_modem_chunk() == b"\x01\x00"
    finally:
        bridge.stop()


def test_paced_16000_bytes_per_second_source(monkeypatch):
    bridge, source = start_bridge(monkeypatch, True)
    # Waiting schedules input, never decides correctness; each tick is acknowledged.
    # No sub-tick elapsed-time assertions (not portable to Windows monotonic clocks).
    tick = max(0.02, time.get_clock_info("monotonic").resolution)
    timer = threading.Event()
    payload = b"\x01\x00" * 160
    try:
        deadline = time.monotonic()
        for count in range(1, 76):
            deadline += tick
            timer.wait(max(0.0, deadline - time.monotonic()))
            source.feed(payload)
            source.wait_drained(count * len(payload))
        assert bridge.read_modem_chunk() == payload * 75
        assert source.lost_bytes == 0 and source.max_readers == 1
    finally:
        bridge.stop()


def test_prebuffer_carry_crosses_handoff_and_logs_once(monkeypatch, caplog):
    bridge, source = start_bridge(monkeypatch, True)
    caplog.set_level(logging.INFO)
    try:
        source.feed(b"\x01\x00\x02")
        source.wait_drained(3)
        assert bridge.read_modem_chunk() == b"\x01\x00"
        assert bridge._rx_carry == b"\x02"
        source.feed(b"\x00\x03\x00")
        assert bridge.read_modem_chunk() == b"\x02\x00\x03\x00"
        assert not bridge._rx_carry
        assert caplog.text.count("[timing] 上行预缓冲:") == 1
        assert bridge._inwaiting_n > 0  # Drain and direct reads use the same stats path.
    finally:
        bridge.stop()


@pytest.mark.parametrize("offset", [False, True])
def test_prebuffer_uses_existing_alignment_self_healing(monkeypatch, offset):
    import numpy as np

    rng = np.random.default_rng(7)
    samples = np.cumsum(rng.normal(0, 300, 16000))
    samples = np.clip(samples - np.convolve(samples, np.ones(50) / 50, mode="same"), -8000, 8000)
    good = (samples * 3).astype(np.int16).tobytes()
    payload = (b"\x00" if offset else b"") + good
    # Reference is the pre-existing direct-read alignment/carry path.
    reference = SerialPcmAudioBridge("unused")
    reference_source = TtyPcmSource()
    reference._ser = reference_source
    expected = bytearray()
    for start in range(0, len(payload), 640):
        reference_source.feed(payload[start:start + 640])
        expected.extend(reference.read_modem_chunk())

    bridge, source = start_bridge(monkeypatch, True)
    try:
        for start in range(0, len(payload), 640):
            source.feed(payload[start:start + 640])
            source.wait_drained(min(len(payload), start + 640))
        assert bridge.read_modem_chunk() == expected
        assert bridge._align_locked and reference._align_locked
        assert bridge._rx_carry == reference._rx_carry
    finally:
        bridge.stop()


@pytest.mark.parametrize("parallel", [False, True])
def test_real_startup_drains_while_agent_connects(monkeypatch, parallel):
    monkeypatch.setenv("MODEM_PCM_UPLINK_PREBUFFER", "true")
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel).lower())
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    monkeypatch.setenv("INBOUND_TRIAGE_MODE", "off")
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "false")
    modem = FakeModem()
    modem.POST_ANSWER_SETTLE_SECONDS = 0
    session = CallSession(modem, "unused", "openai", "nmea", "unused", 115200, 1.0)
    session._outbound_number = "10086"
    session._prompt_gen_opening_mode = "wait"
    session._set_active(True)
    bridge = SerialPcmAudioBridge("fake-pcm")
    source = TtyPcmSource()
    agent = FakeAgent()
    agent.input_rate = 8000
    started = threading.Event()
    expected = []
    original_start = bridge.start

    def start():
        original_start()
        started.set()

    async def connect(callback):
        assert await asyncio.to_thread(started.wait, 5)
        expected.append(await asyncio.to_thread(feed_ticks, source, 75, acknowledged=True))
        await FakeAgent.start(agent, callback)

    async def send(pcm):
        agent.received_audio.append(pcm)
        session._set_active(False)

    async def connected(*args, **kwargs):
        return True

    monkeypatch.setattr(bridge, "_open_serial", lambda: source)
    monkeypatch.setattr(bridge, "start", start)
    monkeypatch.setattr(agent, "start", connect)
    monkeypatch.setattr(agent, "send_audio", send)
    monkeypatch.setattr(session, "_connect_outbound", connected)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _provider: agent)
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kwargs: bridge)

    async def run():
        await asyncio.wait_for(session._handle_call(), timeout=10)

    try:
        asyncio.run(run())
        assert agent.said == []
        assert agent.received_audio == expected
        assert len(expected[0]) == 24000
        assert source.max_readers == 1 and source.lost_bytes == 0
        assert not bridge._prebuffer_thread.is_alive() and not bridge._writer_thread.is_alive()
    finally:
        session._set_active(False)
        bridge.stop()


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("phase", ["connect", "opening"])
def test_startup_failure_stops_prebuffer_before_first_read(monkeypatch, parallel, phase):
    monkeypatch.setenv("MODEM_PCM_UPLINK_PREBUFFER", "true")
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel).lower())
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    modem = FakeModem()
    modem.POST_ANSWER_SETTLE_SECONDS = 0
    session = CallSession(modem, "unused", "openai", "nmea", "unused", 115200, 1.0)
    session._set_active(True)
    bridge = SerialPcmAudioBridge("fake-pcm")
    source = TtyPcmSource()
    agent = FakeAgent()
    started = threading.Event()
    original_start = bridge.start

    def start():
        original_start()
        started.set()

    async def fail(*args):
        assert await asyncio.to_thread(started.wait, 5)
        raise RuntimeError("fake provider failure")

    monkeypatch.setattr(bridge, "_open_serial", lambda: source)
    monkeypatch.setattr(bridge, "start", start)
    monkeypatch.setattr(agent, "start" if phase == "connect" else "say", fail)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _provider: agent)
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kwargs: bridge)

    async def run():
        with pytest.raises(RuntimeError, match="fake provider failure"):
            await asyncio.wait_for(session._handle_call(), timeout=10)

    try:
        asyncio.run(run())
        assert agent.stopped and not source.is_open
        assert not bridge._prebuffer_thread.is_alive()
        assert not bridge._writer_thread.is_alive()
    finally:
        session._set_active(False)
        bridge.stop()


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("barge_in", [False, True])
@pytest.mark.parametrize("opening", ["say", "wait"])
def test_replayed_audio_cannot_interrupt_opening_but_wait_profile_hears_it(
    monkeypatch, parallel, barge_in, opening,
):
    from agentcall.audio_bridge import PrebufferedPcm

    monkeypatch.setenv("MODEM_PCM_UPLINK_PREBUFFER", "true")
    monkeypatch.setenv("INBOUND_TRIAGE_MODE", "off")
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "false")
    monkeypatch.setenv("AGENT_UPLINK_GAIN", "1")
    session = CallSession(FakeModem(), "unused", "openai", "nmea", "unused", 115200, 1.0)
    session._outbound_number = "10086"
    session._prompt_gen_opening_mode = opening
    session._parallel_agent_connect = parallel
    session._barge_in = barge_in
    session._set_active(True)
    historical = b"\x00\x10" * 160
    live = b"\x00\x20" * 160
    bridge = FakeAudioBridge()
    chunks = iter([PrebufferedPcm(historical), live, b""])
    recorded = []

    def read():
        data = next(chunks)
        if not data:
            session._set_active(False)
        return data

    monkeypatch.setattr(bridge, "read_modem_chunk", read)
    agent = FakeAgent()
    agent.input_rate = 8000

    async def send(pcm):
        agent.received_audio.append(pcm)
        if pcm == historical:
            session._handle_peer_barge_in(bridge)

    monkeypatch.setattr(agent, "send_audio", send)
    record = type("Record", (), {"write_uplink": lambda _self, pcm: recorded.append(pcm),
                                 "log_event": lambda *args, **kw: None})()
    asyncio.run(session._run_agent_loop(agent, bridge, record, []))
    assert recorded == [historical, live]
    assert agent.received_audio == ([historical, live] if opening == "wait" else [live])
    assert session._barge_in_echo_strikes == 0
