"""先读后写实验：仅假串口，不等待真实 USB 或通话。"""

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from agentcall.audio_bridge import create_audio_bridge


class PcmSerial:
    is_open = True
    in_waiting = 0

    def __init__(self):
        self.downlink = threading.Event()
        self.written = threading.Event()
        self.read_at = None
        self.write_at = None

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def read(self, count):
        if self.downlink.wait(0.01):
            self.downlink.clear()
            self.read_at = time.monotonic()
            return b"\x01\x00" * 160
        return b""

    def write(self, data):
        if self.write_at is None:
            self.write_at = time.monotonic()
        self.written.set()
        return len(data)

    def close(self):
        self.is_open = False


def make_bridge(monkeypatch, vendor="simcom", hold="1000", enabled_at=None):
    monkeypatch.setenv("MODEM_PCM_UPLINK_PREBUFFER", "false")
    monkeypatch.delenv("MODEM_PCM_UPLINK_HOLD_MS", raising=False)
    if hold is not None:
        monkeypatch.setenv("MODEM_PCM_UPLINK_HOLD_MS", hold)
    modem = SimpleNamespace(vendor=vendor, last_voice_enable={"ok": True,
                            "enabled_mono": time.monotonic() if enabled_at is None else enabled_at})
    audio = create_audio_bridge("nmea", "fake", "fake-pcm", 115200, modem=modem)
    ser = PcmSerial()
    monkeypatch.setattr(audio, "_open_serial", lambda: ser)
    return audio, ser


def test_hold_first_downlink_releases_uplink_and_preserves_pcm(monkeypatch, caplog):
    audio, ser = make_bridge(monkeypatch)
    with caplog.at_level(logging.INFO):
        audio.start()
        try:
            assert not ser.written.wait(0.04)
            ser.downlink.set()
            assert ser.written.wait(0.5)
            assert ser.write_at >= ser.read_at
            assert audio.read_modem_chunk() == b"\x01\x00" * 160
        finally:
            audio.stop()
    assert "首帧下行到达耗时" in caplog.text
    assert "开始上行时刻" in caplog.text
    assert "reason=first_downlink" in caplog.text


def test_hold_deadline_is_measured_from_cpcmreg_ok(monkeypatch, caplog):
    audio, ser = make_bridge(monkeypatch, hold="10000", enabled_at=time.monotonic() - 11)
    with caplog.at_level(logging.INFO):
        audio.start()
        try:
            assert ser.written.wait(0.3)
        finally:
            audio.stop()
    assert "reason=deadline" in caplog.text
    assert "首帧下行到达耗时" not in caplog.text


@pytest.mark.parametrize("vendor,hold", [("simcom", None), ("simcom", "0"), ("quectel", "10000")])
def test_hold_default_and_quectel_keep_immediate_writer(monkeypatch, caplog, vendor, hold):
    audio, ser = make_bridge(monkeypatch, vendor=vendor, hold=hold)
    with caplog.at_level(logging.INFO):
        audio.start()
        try:
            assert ser.written.wait(0.3)
            assert audio._reader_thread.is_alive()
            assert not audio.uplink_prebuffer_enabled
        finally:
            audio.stop()
    assert "PCM_UPLINK_HOLD" not in caplog.text
    assert "NMEA PCM 音频流已启动" in caplog.text


def test_hold_stop_interrupts_wait_without_writing(monkeypatch):
    audio, ser = make_bridge(monkeypatch, hold="10000")
    audio.start()
    audio.stop()
    assert not ser.written.is_set()
    assert not audio._writer_thread.is_alive()
    assert not audio._reader_thread.is_alive()


@pytest.mark.parametrize("hold,mode,marked", [(None, "nmea", False), ("0", "nmea", False),
                                           ("500", "nmea", True), ("500", "uac", False)])
def test_cpcmreg_hold_timestamp_is_gated_and_commands_unchanged(monkeypatch, caplog, hold, mode, marked):
    from agentcall.modem import Sim7600Modem

    monkeypatch.delenv("MODEM_PCM_UPLINK_HOLD_MS", raising=False)
    monkeypatch.setenv("MODEM_PCM_PIN_8K", "false")
    if hold is not None:
        monkeypatch.setenv("MODEM_PCM_UPLINK_HOLD_MS", hold)
    modem = Sim7600Modem("fake-at")
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")
    with caplog.at_level(logging.INFO):
        modem.initialize_for_voice(mode)
    assert sent == ["AT+CPCMREG=1"]
    info = dict(modem.last_voice_enable)
    assert ("enabled_mono" in info) is marked
    if marked:
        assert info.pop("enabled_mono") <= time.monotonic()
    assert info == {"ok": True, "attempts": 1, "degraded": False}
    assert [r.message for r in caplog.records][-1] == "PCM-over-USB 语音通道已启用 (AT+CPCMREG=1，第 1 次)"
