"""EC20 NMEA PCM 下行播放诊断（ec20_record_test 的镜像）。

接听来电后：
1. 通话中查询 AT+QPCMV? 确认 PCM 通道真实状态；
2. 把一段 8kHz wav 按 100ms/1600B 节奏写入 NMEA 口（下行=对方应听到）；
3. 同时录制上行并保存 wav（顺带验证 Mac 上行）。

用法：
    python scripts/ec20_playback_test.py --wav /path/to/8k.wav
接通后请听手机里有没有播放声音，并对着手机说话。
"""

from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parents[1]
FRAME_BYTES = 1600  # 100ms @ 8kHz int16 mono
FRAME_INTERVAL = 0.1


def send_at(ser: serial.Serial, cmd: str, wait: float = 0.6) -> str:
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    time.sleep(wait)
    resp = ser.read(4096).decode("ascii", errors="ignore")
    print(f">>> {cmd}  ->  {resp.strip().replace(chr(13), '')!r}")
    return resp


def wait_for_call(ser: serial.Serial, timeout: float) -> bool:
    print(f"等待来电 {timeout:.0f} 秒，请现在拨打…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = send_at(ser, "AT+CLCC", wait=0.3)
        for line in resp.splitlines():
            parts = line.strip().removeprefix("+CLCC:").strip().split(",")
            if line.strip().startswith("+CLCC:") and len(parts) >= 3:
                if parts[1] == "1" and parts[2] == "4":
                    return True
        time.sleep(0.5)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="EC20 下行 PCM 播放诊断")
    parser.add_argument("--at-port", default="/tmp/ec20-at")
    parser.add_argument("--pcm-port", default="/tmp/ec20-nmea")
    parser.add_argument("--wav", default="/Users/tianye/temp/AA/qwen_greeting_8k.wav")
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()

    with wave.open(args.wav, "rb") as w:
        assert w.getframerate() == 8000 and w.getnchannels() == 1, "需要 8kHz mono wav"
        pcm = w.readframes(w.getnframes())
    print(f"已加载 wav: {len(pcm)} bytes ({len(pcm)/16000:.1f}s)")

    at = serial.Serial(args.at_port, 115200, timeout=0.2, write_timeout=2)
    send_at(at, "ATE0")
    send_at(at, "AT+QAUDMOD=3")
    send_at(at, 'AT+QGPSCFG="outport","none"')
    send_at(at, "AT+QPCMV=0")
    send_at(at, "AT+QPCMV=1,0")
    send_at(at, "AT+QPCMV?")

    if not wait_for_call(at, args.timeout):
        print("未检测到来电，退出")
        return
    print("检测到来电，接听")
    send_at(at, "ATA", wait=1.2)
    time.sleep(0.5)
    send_at(at, "AT+QPCMV?")  # 通话中的真实状态（关键证据）

    pcm_ser = serial.Serial(args.pcm_port, 115200, timeout=0.02, write_timeout=1)
    pcm_ser.reset_input_buffer()

    uplink = bytearray()
    written = 0
    write_errors = 0
    end_at = time.monotonic() + args.seconds
    next_write = time.monotonic()
    offset = 0
    print("开始下行播放 + 上行录音，请注意听手机…")
    while time.monotonic() < end_at:
        now = time.monotonic()
        if now >= next_write and offset < len(pcm):
            frame = pcm[offset:offset + FRAME_BYTES]
            try:
                pcm_ser.write(frame)
                written += len(frame)
                offset += len(frame)
            except serial.SerialTimeoutException:
                write_errors += 1
                print(f"[{now - (end_at - args.seconds):.1f}s] 下行写超时 #{write_errors}（模组不消费）")
                if write_errors >= 3:
                    print("连续写超时，停止下行（保护 USB 不被写崩）")
                    offset = len(pcm)
            next_write += FRAME_INTERVAL
        chunk = pcm_ser.read(pcm_ser.in_waiting or 1)
        if chunk:
            uplink.extend(chunk)

    print(f"下行已写 {written} bytes（{written/16000:.1f}s 音频），写超时 {write_errors} 次")
    print(f"上行收到 {len(uplink)} bytes（{len(uplink)/16000:.1f}s 音频）")

    send_at(at, "ATH")
    send_at(at, "AT+QPCMV=0")
    pcm_ser.close()
    at.close()

    if uplink:
        out = ROOT / "data" / "uplink_test.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(bytes(uplink))
        print(f"上行录音已保存: {out}")


if __name__ == "__main__":
    main()
