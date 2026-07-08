"""CoreAudio 设备枚举（macOS 专用，ctypes 直调，无第三方依赖）。

ffmpeg 的 audiotoolbox ``-audio_device_index`` 用的是 kAudioHardwarePropertyDevices
**全设备数组序号**（含只输入的设备），实测证实：给只输入设备的序号会
AudioQueueStart(-66637) 失败，给有输出的序号成功。故本模块返回全数组序号，
与 ffmpeg 对齐。（注意：这不是「输出设备中的序位」——曾误改成序位导致下行
落到只输入的 AC Interface、对端听不到。）
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
    """名字含 keyword 且有输出流的设备的全数组序号（供 ffmpeg audiotoolbox）。"""
    for idx, _dev_id, name, out_streams in list_devices():
        if keyword.lower() in name.lower() and out_streams > 0:
            return idx
    return None


def _resolve_default_output_id() -> int | None:
    """读系统默认输出设备的 AudioDeviceID（ctypes 直调 CoreAudio）。"""
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
        return dev.value
    except OSError:
        return None


def default_output_index() -> int | None:
    """系统默认输出设备的全数组序号（供 ffmpeg audiotoolbox）。查不到返回 None。"""
    default_id = _resolve_default_output_id()
    if default_id is None:
        return None
    for idx, dev_id, _name, out_streams in list_devices():
        if dev_id == default_id and out_streams > 0:
            return idx
    return None
