"""Nonblocking SCO pump regressions; no real fd, clock waits or hardware."""

from __future__ import annotations

import errno
from types import SimpleNamespace

import pytest

from agentcall import bluetooth_audio
from agentcall.audio_bridge import AgentPcm


@pytest.fixture
def run_pump(monkeypatch):
    def run(reads, *, writes=(), pcm=b"", times=None):
        bridge = bluetooth_audio.BluetoothAudioBridge()
        observed, attempted, closed, blocking = [], [], [], []
        bridge.set_output_observer(observed.append)
        bridge.write_modem_chunks([AgentPcm(pcm)])
        read_results, write_results = iter(reads), iter(writes)
        clock = [0.0]
        state = SimpleNamespace(read_count=0)

        def read(fd, size):
            assert (fd, size) == (77, 1024)
            if times is not None:
                clock[0] = times[state.read_count]
            state.read_count += 1
            result = next(read_results)
            if isinstance(result, BaseException):
                raise result
            return result

        def write(fd, payload):
            assert fd == 77
            attempted.append(payload)
            result = next(write_results, None)
            if isinstance(result, BaseException):
                raise result
            return len(payload) if result is None else result

        monkeypatch.setattr(bluetooth_audio, "os", SimpleNamespace(
            set_blocking=lambda fd, value: blocking.append((fd, value)),
            read=read, write=write, close=closed.append,
        ))
        monkeypatch.setattr(bluetooth_audio, "select", SimpleNamespace(select=lambda *_: ([77], [], [])))
        monkeypatch.setattr(bluetooth_audio, "authorize_sco", lambda _: None)
        monkeypatch.setattr(bluetooth_audio, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        bridge._pump(77)
        assert blocking == [(77, False)]
        assert closed == [77]
        return bridge, observed, attempted, state

    return run


def test_read_eagain_after_readiness_keeps_pump_alive(run_pump, caplog):
    bridge, observed, attempted, state = run_pump(
        [BlockingIOError(errno.EAGAIN, "fake read readiness race"), b"11", b"22", b""], pcm=b"abcdef",
    )
    assert attempted == [b"ab", b"cd"]
    assert state.read_count == 4
    assert bridge.read_modem_chunk() == b"1122"
    assert bridge.packet_count == 2
    assert bridge.pending_output_bytes() == 2
    assert bridge._read_eagain_count == 1
    assert bridge._write_eagain_count == 0
    assert observed == [b"ab", b"cd"]
    assert bridge.output_progress_bytes() == 4
    assert "SCO unavailable" not in caplog.text


@pytest.mark.parametrize("read_eagain", [False, True])
def test_write_eagain_consumes_packet_without_retry_or_false_playout(run_pump, caplog, read_eagain):
    reads = [b"11", b"222", b"33", b""]
    if read_eagain:
        reads.insert(0, BlockingIOError(errno.EAGAIN, "fake read readiness race"))
    bridge, observed, attempted, state = run_pump(
        reads,
        writes=[BlockingIOError(errno.EAGAIN, "fake write backpressure")], pcm=b"abcdef",
    )
    assert attempted == [b"ab", b"cde", b"f\0"]
    assert state.read_count == 4 + int(read_eagain)
    assert bridge.read_modem_chunk() == b"1122233"
    assert bridge.packet_count == 3
    assert bridge.pending_output_bytes() == 0
    assert bridge._read_eagain_count == int(read_eagain)
    assert bridge._write_eagain_count == 1
    assert observed == [b"cde", b"f"]  # A dropped packet is not a confirmed write.
    assert bridge.output_progress_bytes() == 4
    assert not bridge._output_spans
    assert "write_eagain=1" in caplog.text
    assert "SCO unavailable" not in caplog.text


def test_write_eagain_logs_are_rate_limited_with_cumulative_counts(run_pump, caplog):
    bridge, observed, attempted, _ = run_pump(
        [b"11", b"22", b"33", b"44", b"55", b""], pcm=b"abcdefghij",
        writes=[BlockingIOError(errno.EAGAIN, "fake backpressure") for _ in range(4)],
        times=[0.0, 1.0, 4.9, 5.0, 5.1, 5.2],
    )
    assert attempted == [b"ab", b"cd", b"ef", b"gh", b"ij"]
    assert bridge._write_eagain_count == 4
    assert bridge.pending_output_bytes() == 0
    assert observed == [b"ij"]
    logs = [record.getMessage() for record in caplog.records if "write_eagain=" in record.getMessage()]
    assert len(logs) == 2
    assert "write_eagain=1" in logs[0]
    assert "write_eagain=4" in logs[1]


@pytest.mark.parametrize("failure", ["read", "write", "short_write"])
def test_real_io_error_or_short_write_still_terminates_pump(run_pump, caplog, failure):
    read_failure = OSError(errno.ENOTCONN, "fake disconnected SCO")
    reads = [read_failure, b"22", b""] if failure == "read" else [b"11", b"22", b""]
    writes = [OSError(errno.EPIPE, "fake broken SCO")] if failure == "write" else [1]
    bridge, observed, attempted, state = run_pump(reads, writes=writes, pcm=b"abcd")
    assert state.read_count == 1
    assert attempted == ([] if failure == "read" else [b"ab"])
    assert bridge._read_eagain_count == bridge._write_eagain_count == 0
    assert observed == [] and bridge.output_progress_bytes() == 0
    assert "SCO unavailable" in caplog.text
