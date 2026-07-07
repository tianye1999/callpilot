"""枚举 CoreAudio 设备表（序号 + AudioDeviceID + 名字）。

ffmpeg 的 audiotoolbox 输出用 -audio_device_index 按 kAudioHardwarePropertyDevices
数组序号选设备，本脚本输出该序号，供播放侧选择 EC20 的 "AS Interface"。
"""

from __future__ import annotations

import ctypes
import struct


def fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

kAudioObjectSystemObject = 1
kAudioHardwarePropertyDevices = fourcc("dev#")
kAudioDevicePropertyDeviceName = fourcc("name")
kAudioObjectPropertyScopeGlobal = fourcc("glob")
kAudioDevicePropertyScopeOutput = fourcc("outp")
kAudioDevicePropertyStreams = fourcc("stm#")


class PropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


def get_property(obj_id: int, selector: int, scope: int, buf: ctypes.Array) -> int:
    addr = PropertyAddress(selector, scope, 0)
    size = ctypes.c_uint32(ctypes.sizeof(buf))
    status = ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(obj_id), ctypes.byref(addr), 0, None,
        ctypes.byref(size), buf,
    )
    if status != 0:
        raise OSError(f"AudioObjectGetPropertyData failed: {status}")
    return size.value


def list_devices() -> list[tuple[int, int, str, int]]:
    """返回 [(数组序号, AudioDeviceID, 名字, 输出流数)]。"""
    ids_buf = (ctypes.c_uint32 * 64)()
    size = get_property(
        kAudioObjectSystemObject, kAudioHardwarePropertyDevices,
        kAudioObjectPropertyScopeGlobal, ids_buf,
    )
    count = size // 4
    devices = []
    for idx in range(count):
        dev_id = ids_buf[idx]
        name_buf = ctypes.create_string_buffer(256)
        try:
            get_property(dev_id, kAudioDevicePropertyDeviceName,
                         kAudioObjectPropertyScopeGlobal, name_buf)
            name = name_buf.value.decode("utf-8", "replace")
        except OSError:
            name = "?"
        streams_buf = (ctypes.c_uint32 * 32)()
        try:
            out_size = get_property(dev_id, kAudioDevicePropertyStreams,
                                    kAudioDevicePropertyScopeOutput, streams_buf)
            out_streams = out_size // 4
        except OSError:
            out_streams = 0
        devices.append((idx, dev_id, name, out_streams))
    return devices


def find_output_index(keyword: str) -> int | None:
    """返回名字含 keyword 且有输出流的设备的数组序号（ffmpeg audiotoolbox 用）。"""
    for idx, _dev_id, name, out_streams in list_devices():
        if keyword.lower() in name.lower() and out_streams > 0:
            return idx
    return None


if __name__ == "__main__":
    for idx, dev_id, name, outs in list_devices():
        print(f"[{idx}] id={dev_id} out_streams={outs} {name}")
