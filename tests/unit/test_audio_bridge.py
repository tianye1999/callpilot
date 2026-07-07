"""音频桥纯逻辑单测。"""

from __future__ import annotations

import threading

from agentcall.audio_bridge import FfmpegAudioBridge, NMEA_WRITE_SIZE


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
