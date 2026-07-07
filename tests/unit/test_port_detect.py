"""port_detect 单测：Quectel AT 口扫描 + modem connect 的 auto 解析路径。"""

from __future__ import annotations

import pytest
import serial

from agentcall import platforms, port_detect
from agentcall.modem import Eg25Modem
from agentcall.port_detect import QUECTEL_VID, detect_at_port


class FakePortInfo:
    """serial.tools.list_ports 返回项的最小替身（只带被测代码用到的字段）。"""

    def __init__(self, device: str, description: str = "", vid: int | None = None):
        self.device = device
        self.description = description
        self.vid = vid


def _patch_comports(monkeypatch, ports: list[FakePortInfo]) -> None:
    monkeypatch.setattr(port_detect.list_ports, "comports", lambda: ports)


# ---- detect_at_port ----


def test_detect_prefers_at_description(monkeypatch):
    _patch_comports(monkeypatch, [
        FakePortInfo("COM3", "Quectel USB DM Port", QUECTEL_VID),
        FakePortInfo("COM4", "Quectel USB NMEA Port", QUECTEL_VID),
        FakePortInfo("COM5", "Quectel USB AT Port", QUECTEL_VID),
        FakePortInfo("COM6", "Quectel USB Modem", QUECTEL_VID),
    ])
    assert detect_at_port() == "COM5"


def test_detect_ignores_at_description_on_other_vendor(monkeypatch):
    """AT 字样出现在非 Quectel 设备上不算数（VID 过滤优先）。"""
    _patch_comports(monkeypatch, [
        FakePortInfo("COM2", "Some USB AT Port", 0x1234),
        FakePortInfo("COM7", "Quectel USB AT Port", QUECTEL_VID),
    ])
    assert detect_at_port() == "COM7"


def test_detect_word_boundary_avoids_data_false_positive(monkeypatch):
    """描述里 DATA 之类含 AT 子串的词不应误中，落到第 3 口惯例回退。"""
    _patch_comports(monkeypatch, [
        FakePortInfo("COM3", "Quectel USB DATA Port", QUECTEL_VID),
        FakePortInfo("COM4", "Quectel USB Port", QUECTEL_VID),
        FakePortInfo("COM5", "Quectel USB Port", QUECTEL_VID),
        FakePortInfo("COM6", "Quectel USB Port", QUECTEL_VID),
    ])
    assert detect_at_port() == "COM5"


def test_detect_falls_back_to_third_interface_with_natural_order(monkeypatch):
    """无 AT 描述时按第 3 口惯例回退；COM10 > COM9 需按数字序而非字典序。"""
    _patch_comports(monkeypatch, [
        FakePortInfo("COM10", "", QUECTEL_VID),
        FakePortInfo("COM9", "", QUECTEL_VID),
        FakePortInfo("COM8", "", QUECTEL_VID),
        FakePortInfo("COM11", "", QUECTEL_VID),
    ])
    # 数字序 COM8, COM9, COM10, COM11 → 第 3 个是 COM10
    assert detect_at_port() == "COM10"


def test_detect_returns_none_without_quectel_device(monkeypatch):
    _patch_comports(monkeypatch, [
        FakePortInfo("COM1", "Standard Serial Port", None),
        FakePortInfo("COM2", "USB-SERIAL CH340", 0x1A86),
    ])
    assert detect_at_port() is None
    _patch_comports(monkeypatch, [])
    assert detect_at_port() is None


def test_detect_returns_none_when_too_few_quectel_ports(monkeypatch):
    """有 Quectel 口但不足 3 个且无 AT 描述，无法套第 3 口惯例。"""
    _patch_comports(monkeypatch, [
        FakePortInfo("COM3", "Quectel USB DM Port", QUECTEL_VID),
        FakePortInfo("COM4", "Quectel USB NMEA Port", QUECTEL_VID),
    ])
    assert detect_at_port() is None


# ---- modem connect 的 auto 解析路径 ----


class FakeSerial:
    """假串口：记录构造参数，每次 write 后排一条 OK 供 _read_response 读取。"""

    instances: list["FakeSerial"] = []

    def __init__(self, port=None, baudrate=115200, timeout=0.2, write_timeout=2):
        self.port = port
        self.is_open = True
        self.writes: list[str] = []
        self._pending = b""
        FakeSerial.instances.append(self)

    @property
    def in_waiting(self) -> int:
        return len(self._pending)

    def write(self, data: bytes) -> int:
        self.writes.append(data.decode("ascii").strip())
        self._pending = b"\r\nOK\r\n"
        return len(data)

    def read(self, size: int = 1) -> bytes:
        out, self._pending = self._pending[:size], self._pending[size:]
        return out

    def reset_input_buffer(self) -> None:
        self._pending = b""

    def close(self) -> None:
        self.is_open = False


@pytest.fixture()
def fake_serial(monkeypatch):
    FakeSerial.instances = []
    monkeypatch.setattr("agentcall.modem.serial.Serial", FakeSerial)
    monkeypatch.setattr("agentcall.modem.time.sleep", lambda s: None)
    return FakeSerial


def test_connect_auto_resolves_port_each_attempt(fake_serial, monkeypatch):
    """port=auto 时每次 connect 都重新探测（Windows 重插后 COM 号会变）。"""
    detected = iter(["COM5", "COM9"])
    monkeypatch.setattr(
        "agentcall.modem.port_detect.detect_at_port", lambda: next(detected)
    )

    modem = Eg25Modem(port=platforms.AUTO_PORT)
    modem.connect()
    assert fake_serial.instances[0].port == "COM5"
    assert modem._active_port == "COM5"
    assert "AT" in fake_serial.instances[0].writes  # 初始化序列跑在解析出的口上

    modem.connect()  # 第二次连接（如重插后 supervisor 重试）
    assert fake_serial.instances[1].port == "COM9"
    assert modem._active_port == "COM9"


def test_connect_auto_raises_when_not_detected(fake_serial, monkeypatch):
    """探测不到设备时抛连接异常，交给 supervisor 退避重试。"""
    monkeypatch.setattr("agentcall.modem.port_detect.detect_at_port", lambda: None)

    modem = Eg25Modem(port=platforms.AUTO_PORT)
    with pytest.raises(serial.SerialException):
        modem.connect()
    assert fake_serial.instances == []  # 未探测到不应尝试开串口


def test_connect_fixed_port_skips_detection(fake_serial, monkeypatch):
    """显式端口不走探测（探测函数被调用即失败）。"""
    def boom() -> str | None:
        raise AssertionError("固定端口不应触发探测")

    monkeypatch.setattr("agentcall.modem.port_detect.detect_at_port", boom)

    modem = Eg25Modem(port="/dev/ttyUSB2")
    modem.connect()
    assert fake_serial.instances[0].port == "/dev/ttyUSB2"
    assert modem._active_port == "/dev/ttyUSB2"
