"""音频桥纯逻辑单测。"""

from __future__ import annotations

import threading

import numpy as np
import pytest

import agentcall.audio_bridge as audio_bridge
import agentcall.coreaudio as coreaudio
from agentcall.audio_bridge import (
    NMEA_WRITE_SIZE,
    FfmpegAudioBridge,
    create_audio_bridge,
    resample_pcm,
)


@pytest.mark.parametrize("src_rate,dst_rate", [(8000, 24000), (24000, 8000)])
def test_resample_pcm_round_trip_preserves_length_and_waveform(src_rate, dst_rate):
    duration = 0.1
    sample_count = int(src_rate * duration)
    phase = np.arange(sample_count, dtype=np.float64) / src_rate
    original = (np.sin(2 * np.pi * 440 * phase) * 12000).astype(np.int16)

    converted = resample_pcm(original.tobytes(), src_rate, dst_rate)
    restored_bytes = resample_pcm(converted, dst_rate, src_rate)
    restored = np.frombuffer(restored_bytes, dtype=np.int16)

    assert len(converted) == int(sample_count * dst_rate / src_rate) * 2
    assert restored.size == original.size
    assert np.corrcoef(original.astype(np.float64), restored.astype(np.float64))[0, 1] > 0.98
    assert np.sqrt(np.mean(restored.astype(np.float64) ** 2)) == pytest.approx(
        np.sqrt(np.mean(original.astype(np.float64) ** 2)), rel=0.05
    )


def make_ffmpeg_bridge() -> FfmpegAudioBridge:
    bridge = FfmpegAudioBridge.__new__(FfmpegAudioBridge)
    bridge._tx_buffer = bytearray()
    bridge._tx_lock = threading.Lock()
    return bridge


def test_ffmpeg_uac_write_payload_keeps_silence_clock_when_empty():
    bridge = make_ffmpeg_bridge()
    silence = b"\x00" * NMEA_WRITE_SIZE

    assert bridge._next_write_payload(silence) == silence


def test_ffmpeg_uac_write_payload_pads_partial_agent_audio():
    bridge = make_ffmpeg_bridge()
    silence = b"\x00" * NMEA_WRITE_SIZE
    bridge.write_modem_chunks([b"\x01\x02\x03"])

    payload = bridge._next_write_payload(silence)

    assert len(payload) == NMEA_WRITE_SIZE
    assert payload[:3] == b"\x01\x02\x03"
    assert payload[3:] == b"\x00" * (NMEA_WRITE_SIZE - 3)
    assert bridge.pending_output_bytes() == 0


def test_ffmpeg_uac_write_payload_consumes_one_realtime_frame():
    bridge = make_ffmpeg_bridge()
    silence = b"\x00" * NMEA_WRITE_SIZE
    first_frame = b"\x11" * NMEA_WRITE_SIZE
    remainder = b"\x22" * 7
    bridge.write_modem_chunks([first_frame + remainder])

    payload = bridge._next_write_payload(silence)

    assert payload == first_frame
    assert bridge.pending_output_bytes() == len(remainder)


# ---- 平台约束：uac_ffmpeg 仅 macOS（Windows 路径待硬件验证）----


def test_ffmpeg_bridge_rejected_on_non_macos(monkeypatch):
    monkeypatch.setattr(audio_bridge.platforms, "IS_MACOS", False)

    with pytest.raises(RuntimeError, match="仅支持 macOS.*MODEM_AUDIO_MODE=uac"):
        FfmpegAudioBridge("Interface")


def test_create_audio_bridge_uac_ffmpeg_rejected_on_non_macos(monkeypatch):
    monkeypatch.setattr(audio_bridge.platforms, "IS_MACOS", False)

    with pytest.raises(RuntimeError, match="仅支持 macOS"):
        create_audio_bridge("uac_ffmpeg", "Interface", None, 921600)


def test_ffmpeg_bridge_constructs_on_macos(monkeypatch):
    """平台检查不误伤 macOS：设备探测打桩后应正常完成构造。"""
    monkeypatch.setattr(audio_bridge.platforms, "IS_MACOS", True)
    monkeypatch.setattr(
        FfmpegAudioBridge, "_find_avfoundation_input", staticmethod(lambda keyword: 1)
    )
    monkeypatch.setattr(coreaudio, "find_output_index", lambda keyword: 2)

    bridge = FfmpegAudioBridge("Interface")

    assert bridge.input_index == 1
    assert bridge.output_index == 2


def test_create_audio_bridge_invalid_mode_mentions_macos_constraint():
    with pytest.raises(ValueError, match="仅 macOS"):
        create_audio_bridge("bogus", "Interface", None, 921600)
