"""EC20 NMEA PCM 上行录音诊断。

接听来电后，纯粹录制 COM21 收到的对方语音（不向 PCM 口写任何数据，
排除回环干扰），把原始字节按多个采样率存成 wav，并做基础分析，
用来判断收到的到底是语音还是噪声、真实采样率是多少。

用法：
    python scripts/ec20_record_test.py --at-port COM24 --pcm-port COM21 --seconds 12
接通后请对着电话清楚地数数：一、二、三、四、五……
"""

from __future__ import annotations

import argparse
import audioop
import sys
import time
import wave
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


def save_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def analyze(pcm: bytes) -> None:
    if len(pcm) < 2:
        print("没有录到任何数据")
        return
    total_rms = audioop.rms(pcm, 2)
    peak = audioop.max(pcm, 2)
    # 分 100ms 帧（按 8kHz 算 = 1600 bytes）统计活动段
    frame = 1600
    rms_seq = []
    for i in range(0, len(pcm) - frame, frame):
        rms_seq.append(audioop.rms(pcm[i : i + frame], 2))
    active = [r for r in rms_seq if r > 300]
    print(
        f"分析: total_bytes={len(pcm)}, total_rms={total_rms}, peak={peak}, "
        f"frames={len(rms_seq)}, active_frames(>300)={len(active)}"
    )
    if rms_seq:
        sample = ",".join(str(r) for r in rms_seq[:40])
        print(f"前 40 帧 RMS: {sample}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="EC20 上行录音诊断")
    parser.add_argument("--at-port", default="COM24")
    parser.add_argument("--pcm-port", default="COM21")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--pcm-baud", type=int, default=921600)
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--rtscts", action="store_true")
    args = parser.parse_args()

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

        with serial.Serial(
            args.pcm_port,
            args.pcm_baud,
            timeout=0.02,
            rtscts=args.rtscts,
        ) as pcm:
            pcm.reset_input_buffer()
            print(f"开始录音 {args.seconds} 秒，请对着电话清楚地数数：一、二、三、四、五……")
            buf = bytearray()
            end_at = time.monotonic() + args.seconds
            while time.monotonic() < end_at:
                incoming = pcm.read(pcm.in_waiting or 1600)
                if incoming:
                    buf.extend(incoming)
                else:
                    time.sleep(0.01)
            pcm_bytes = bytes(buf)

        print(send_at(at, "ATH").strip())
        print(send_at(at, "AT+QPCMV=0").strip())

    raw_path = ROOT / "rec_raw.bin"
    raw_path.write_bytes(pcm_bytes)
    save_wav(ROOT / "rec_8k.wav", pcm_bytes, 8000)
    save_wav(ROOT / "rec_16k.wav", pcm_bytes, 16000)
    print(f"已保存: {raw_path.name}, rec_8k.wav, rec_16k.wav")
    analyze(pcm_bytes)
    print("请用电脑播放器分别播放 rec_8k.wav 和 rec_16k.wav，听哪个是你正常的数数声。")


if __name__ == "__main__":
    main()
