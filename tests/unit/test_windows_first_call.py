"""Offline regressions for the first Windows call; no hardware access."""

import asyncio
import struct
import threading
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from agentcall import platforms
from agentcall.audio_bridge import apply_pcm_gain


def test_soft_knee_preserves_quiet_samples_and_has_no_rails():
    quiet = np.arange(-12000, 12001, dtype=np.int16)
    assert apply_pcm_gain(quiet.tobytes(), 2) == (quiet * 2).tobytes()
    loud = np.arange(12000, 32768, dtype=np.int16)
    out = np.frombuffer(apply_pcm_gain(loud.tobytes(), 2), dtype=np.int16)
    assert np.all(np.diff(out.astype(np.int32)) >= 0)
    assert 24000 < out[-1] < 32767
    assert len(np.unique(out[out > 30000])) > 100
    rails = np.frombuffer(apply_pcm_gain(struct.pack('<hh', -32768, 32767), 16), dtype=np.int16)
    assert -32767 < rails[0] < -30000 < 30000 < rails[1] < 32767


@pytest.mark.parametrize('windows', [False, True])
def test_timer_request_balanced_even_on_failure(monkeypatch, windows):
    from agentcall.windows_audio_timing import audio_timer_resolution

    winmm = SimpleNamespace(timeBeginPeriod=Mock(return_value=0), timeEndPeriod=Mock(return_value=0))
    monkeypatch.setattr(platforms, 'IS_WINDOWS', windows)
    monkeypatch.setattr('agentcall.windows_audio_timing._winmm', lambda: winmm)
    with pytest.raises(RuntimeError), audio_timer_resolution():
        raise RuntimeError('worker failure')
    assert winmm.timeBeginPeriod.call_count == int(windows)
    assert winmm.timeEndPeriod.call_count == int(windows)


@pytest.mark.parametrize('skew', [-71, 71])
def test_clock_skew_diagnostic_from_unauthorized_date(monkeypatch, skew):
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from agentcall.cloud_control import _handshake_error

    monkeypatch.setattr('agentcall.cloud_control.time.time', lambda: 1800000000 + skew)
    error = InvalidStatus(Response(401, 'Unauthorized', Headers({'Date': formatdate(1800000000, usegmt=True)})))
    assert _handshake_error(error).startswith('clock_skew:')
    assert '71' in _handshake_error(error)
    error.response.status_code = 503
    assert _handshake_error(error) == 'InvalidStatus'


@pytest.mark.parametrize('windows', [False, True])
def test_windows_serial_wait_does_not_block_sender(monkeypatch, windows):
    from agentcall.remote_dialer import _read_remote_pcm

    monkeypatch.setattr(platforms, 'IS_WINDOWS', windows)
    loop_thread = threading.get_ident()
    release = threading.Event()
    entered = threading.Event()

    def read():
        assert (threading.get_ident() != loop_thread) == windows
        entered.set()
        if windows:
            assert release.wait(2), 'event loop did not run while serial read waited'
        return b'pcm'

    async def scenario():
        task = asyncio.create_task(_read_remote_pcm(SimpleNamespace(read_modem_chunk=read)))
        if windows:
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.001)
                assert entered.is_set()
                assert not task.done()
            finally:
                release.set()
        assert await task == b'pcm'

    asyncio.run(scenario())


def test_timer_failed_request_does_not_release(monkeypatch):
    from agentcall.windows_audio_timing import audio_timer_resolution

    winmm = SimpleNamespace(timeBeginPeriod=Mock(return_value=1), timeEndPeriod=Mock())
    monkeypatch.setattr(platforms, 'IS_WINDOWS', True)
    monkeypatch.setattr('agentcall.windows_audio_timing._winmm', lambda: winmm)
    with audio_timer_resolution():
        pass
    winmm.timeEndPeriod.assert_not_called()


def test_limiter_stats_are_windowed_and_reset(caplog):
    from agentcall.pcm_stats import PcmFlowStats

    now = [0.0]
    stats = PcmFlowStats('test_limiter', clock=lambda: now[0])
    pcm = apply_pcm_gain(struct.pack('<4h', 0, 6000, 12001, -16384), 2, stats)
    stats.add(pcm)
    now[0] = 5
    with caplog.at_level('INFO'):
        assert stats.maybe_log()
    assert 'limited_samples=2 limited_ratio=0.500000' in caplog.text
    caplog.clear()
    apply_pcm_gain(struct.pack('<h', 1), 2, stats)
    now[0] = 10
    with caplog.at_level('INFO'):
        assert stats.maybe_log()
    assert 'limited_samples=0 limited_ratio=0.000000' in caplog.text


@pytest.mark.parametrize('date', [None, 'garbage'])
def test_clock_diagnostic_missing_or_invalid_date(date):
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from agentcall.cloud_control import _handshake_error

    error = InvalidStatus(Response(401, 'Unauthorized', Headers({'Date': date} if date else {})))
    assert _handshake_error(error) == 'InvalidStatus'


def test_unity_gain_is_exact_passthrough_without_limiting():
    pcm = struct.pack('<4h', 0, 24001, 32767, -32768)
    assert apply_pcm_gain(pcm, 1.0) is pcm
