"""UAC 冒烟测试：启用 QPCMV=1,2 后验证 PortAudio 能否打开 EC20 UAC 输入/输出流。"""

from __future__ import annotations

import time

import serial


def at(ser: serial.Serial, cmd: str, wait: float = 0.5) -> str:
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    time.sleep(wait)
    resp = ser.read(4096).decode("ascii", "ignore").strip()
    print(f">>> {cmd} -> {resp!r}", flush=True)
    return resp


def main() -> None:
    ser = serial.Serial("/tmp/ec20-at", 115200, timeout=1)
    at(ser, "AT+QPCMV=0")
    at(ser, "AT+QPCMV=1,2")
    at(ser, "AT+QPCMV?")
    ser.close()
    time.sleep(2)

    import sounddevice as sd

    print("PortAudio 初始化: OK（未挂起）", flush=True)
    devs = sd.query_devices()
    uac = [(i, d["name"]) for i, d in enumerate(devs) if "Interface" in d["name"]]
    print("UAC 设备:", uac, flush=True)
    in_idx = next((i for i, d in enumerate(devs) if "AC Interface" in d["name"]), None)
    out_idx = next((i for i, d in enumerate(devs) if "AS Interface" in d["name"]), None)
    if in_idx is None or out_idx is None:
        raise SystemExit("未找到 UAC 输入/输出设备")

    si = sd.RawInputStream(samplerate=8000, blocksize=160, dtype="int16", channels=1, device=in_idx)
    si.start()
    data, _ = si.read(160)
    si.stop()
    si.close()
    print(f"UAC 输入流: OK（读到 {len(bytes(data))} bytes）", flush=True)

    so = sd.RawOutputStream(samplerate=8000, blocksize=160, dtype="int16", channels=1, device=out_idx)
    so.start()
    so.write(b"\x00" * 320)
    so.stop()
    so.close()
    print("UAC 输出流: OK", flush=True)
    print("=== UAC 路线可行 ===", flush=True)


if __name__ == "__main__":
    main()
