"""CoreAudio 设备枚举（macOS 专用，ctypes 直调，无第三方依赖）。

ffmpeg 的 audiotoolbox 输出按 kAudioHardwarePropertyDevices 数组序号选设备
（-audio_device_index），本模块提供该序号的查找。
"""

from __future__ import annotations

import ctypes
import struct


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


_KA_SYSTEM_OBJECT = 1
_KA_DEVICES = _fourcc("dev#")
_KA_DEVICE_NAME = _fourcc("name")
_KA_SCOPE_GLOBAL = _fourcc("glob")
_KA_SCOPE_OUTPUT = _fourcc("outp")
_KA_STREAMS = _fourcc("stm#")


class _PropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


def _get_property(ca, obj_id: int, selector: int, scope: int, buf) -> int:
    addr = _PropertyAddress(selector, scope, 0)
    size = ctypes.c_uint32(ctypes.sizeof(buf))
    status = ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(obj_id), ctypes.byref(addr), 0, None, ctypes.byref(size), buf
    )
    if status != 0:
        raise OSError(f"AudioObjectGetPropertyData failed: {status}")
    return size.value


def list_devices() -> list[tuple[int, int, str, int]]:
    """返回 [(数组序号, AudioDeviceID, 名字, 输出流数)]。"""
    ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    ids_buf = (ctypes.c_uint32 * 64)()
    size = _get_property(ca, _KA_SYSTEM_OBJECT, _KA_DEVICES, _KA_SCOPE_GLOBAL, ids_buf)
    devices = []
    for idx in range(size // 4):
        dev_id = ids_buf[idx]
        name_buf = ctypes.create_string_buffer(256)
        try:
            _get_property(ca, dev_id, _KA_DEVICE_NAME, _KA_SCOPE_GLOBAL, name_buf)
            name = name_buf.value.decode("utf-8", "replace")
        except OSError:
            name = "?"
        streams_buf = (ctypes.c_uint32 * 32)()
        try:
            out_size = _get_property(ca, dev_id, _KA_STREAMS, _KA_SCOPE_OUTPUT, streams_buf)
            out_streams = out_size // 4
        except OSError:
            out_streams = 0
        devices.append((idx, dev_id, name, out_streams))
    return devices


_KA_DEFAULT_OUTPUT = _fourcc("dOut")  # kAudioHardwarePropertyDefaultOutputDevice


def find_output_index(keyword: str) -> int | None:
    """名字含 keyword 且有输出流的设备的数组序号（供 ffmpeg audiotoolbox）。"""
    for idx, _dev_id, name, out_streams in list_devices():
        if keyword.lower() in name.lower() and out_streams > 0:
            return idx
    return None


def default_output_index() -> int | None:
    """系统默认输出设备的数组序号（供 ffmpeg audiotoolbox）。

    比按名字匹配更稳健、可移植：跟随用户在系统里选的输出（内置扬声器/耳机/
    外接音箱），不依赖机型或语言特定的设备名。查不到返回 None。
    """
    try:
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        addr = _PropertyAddress(_KA_DEFAULT_OUTPUT, _KA_SCOPE_GLOBAL, 0)
        dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        status = ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(_KA_SYSTEM_OBJECT), ctypes.byref(addr), 0, None,
            ctypes.byref(size), ctypes.byref(dev),
        )
        if status != 0:
            return None
        default_id = dev.value
    except OSError:
        return None
    for idx, dev_id, _name, out_streams in list_devices():
        if dev_id == default_id and out_streams > 0:
            return idx
    return None
