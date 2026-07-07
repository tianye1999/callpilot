"""EC20 NMEA PCM 下行测试音。

用来排查电话里听不到 Agent 的问题：绕过千问，直接向 NMEA PCM 口
写入 8kHz/16bit/mono 的 1kHz 正弦波。
"""

from __future__ import annotations

import argparse
import math
import audioop
import sys
import threading
import time
from pathlib import Path

import serial
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def send_at(port: serial.Serial, command: str, wait: float = 0.8) -> str:
    port.write(f"{command}\r".encode("ascii"))
    time.sleep(wait)
    deadline = time.time() + 3
    response = ""
    while time.time() < deadline:
        chunk = port.read(port.in_waiting or 1)
        if chunk:
            response += chunk.decode("ascii", errors="ignore")
        if "OK" in response or "ERROR" in response:
            break
        time.sleep(0.05)
    return response


def make_tone_frame(
    sample_rate: int = 8000,
    frequency: int = 1000,
    duration_ms: int = 100,
    amplitude: int = 14000,
) -> bytes:
    samples = int(sample_rate * duration_ms / 1000)
    frame = bytearray()
    for i in range(samples):
        value = int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
        frame.extend(value.to_bytes(2, byteorder="little", signed=True))
    return bytes(frame)


def make_square_frame(
    sample_rate: int = 8000,
    frequency: int = 1000,
    duration_ms: int = 100,
    amplitude: int = 22000,
) -> bytes:
    samples = int(sample_rate * duration_ms / 1000)
    half_period = max(1, sample_rate // frequency // 2)
    frame = bytearray()
    for i in range(samples):
        value = amplitude if (i // half_period) % 2 == 0 else -amplitude
        frame.extend(value.to_bytes(2, byteorder="little", signed=True))
    return bytes(frame)


def wait_for_call(at_port: serial.Serial, timeout: int) -> str | None:
    deadline = time.time() + timeout
    seen_ids: set[str] = set()
    while time.time() < deadline:
        response = send_at(at_port, "AT+CLCC", wait=0.25)
        for line in response.splitlines():
            line = line.strip()
            if not line.startswith("+CLCC:"):
                continue
            parts = line.removeprefix("+CLCC:").strip().split(",")
            if len(parts) < 3:
                continue
            call_id = parts[0].strip()
            direction = parts[1].strip()
            status = parts[2].strip()
            if direction == "1" and status == "4" and call_id not in seen_ids:
                seen_ids.add(call_id)
                return call_id
        time.sleep(0.5)
    return None


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="EC20 电话下行 1kHz 测试音")
    parser.add_argument("--at-port", default="COM24")
    parser.add_argument("--pcm-port", default="COM21")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--pcm-baud", type=int, default=921600)
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--wave", choices=["sine", "square"], default="square")
    parser.add_argument(
        "--wait-input",
        action="store_true",
        help="接通后先等 PCM 口读到对方语音再开始写测试音",
    )
    parser.add_argument(
        "--rtscts",
        action="store_true",
        help="打开串口 RTS/CTS 流控",
    )
    args = parser.parse_args()

    tone_frame = make_square_frame() if args.wave == "square" else make_tone_frame()

    with serial.Serial(args.at_port, args.baud, timeout=0.2, write_timeout=1) as at:
        print(send_at(at, "ATE0").strip())
        print(send_at(at, "AT+CMEE=2").strip())
        print(send_at(at, "AT+CLIP=1").strip())
        print(send_at(at, "AT+QAUDMOD=3").strip())
        print(send_at(at, 'AT+QGPSCFG="outport","none"').strip())
        print(send_at(at, "AT+QPCMV=0").strip())
        print(send_at(at, "AT+QPCMV=1,0").strip())
        print(send_at(at, "AT+QPCMV?").strip())
        print(f"等待来电 {args.timeout} 秒，请现在拨打...")

        call_id = wait_for_call(at, args.timeout)
        if not call_id:
            print("未检测到来电")
            return

        print(f"检测到来电 call_id={call_id}，发送 ATA")
        print(send_at(at, "ATA", wait=1.2).strip())

        stop_reader = threading.Event()

        def at_urc_reader() -> None:
            while not stop_reader.is_set():
                chunk = at.read(at.in_waiting or 1)
                if chunk:
                    text = chunk.decode("ascii", errors="ignore").strip()
                    if text:
                        print(f"AT_URC: {text}")
                time.sleep(0.02)

        reader_thread = threading.Thread(target=at_urc_reader, daemon=True)
        reader_thread.start()

        with serial.Serial(
            args.pcm_port,
            args.pcm_baud,
            timeout=0.02,
            write_timeout=0.2,
            rtscts=args.rtscts,
        ) as pcm:
            pcm.reset_input_buffer()
            pcm.reset_output_buffer()
            if args.wait_input:
                print("等待 PCM 口出现对方语音数据...")
                wait_deadline = time.monotonic() + 10
                waited_bytes = 0
                while time.monotonic() < wait_deadline:
                    incoming = pcm.read(pcm.in_waiting or 640)
                    waited_bytes += len(incoming)
                    if waited_bytes >= 640:
                        break
                print(f"开始写入前已读到 PCM bytes={waited_bytes}")

            print(f"开始向 {args.pcm_port} 发送 1kHz 测试音 {args.seconds} 秒")
            end_at = time.monotonic() + args.seconds
            next_write = time.monotonic()
            frames = 0
            read_bytes = 0
            max_rms = 0
            while time.monotonic() < end_at:
                incoming = pcm.read(pcm.in_waiting or 640)
                if incoming:
                    read_bytes += len(incoming)
                    if len(incoming) >= 2:
                        max_rms = max(max_rms, audioop.rms(incoming, 2))

                now = time.monotonic()
                if now < next_write:
                    time.sleep(min(0.01, next_write - now))
                    continue
                pcm.write(tone_frame)
                frames += 1
                next_write += 0.1
            print(
                "测试音发送完成 "
                f"frames={frames}, bytes={frames * len(tone_frame)}, "
                f"read_bytes={read_bytes}, max_rms={max_rms}"
            )

        stop_reader.set()
        reader_thread.join(timeout=1)
        print(send_at(at, "ATH").strip())
        print(send_at(at, "AT+QPCMV=0").strip())


if __name__ == "__main__":
    main()
