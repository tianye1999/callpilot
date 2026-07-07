"""AG35 PCM 数据口探测。

AG35 (OCPU 固件) 的 AT 口在 COM36，但 QPCMV NMEA 数据走哪个口未知。
本脚本在 COM36 启用 Voice over USB、接听来电后，同时监听候选口
(COM34/COM35)，统计各口收到的字节数与音频 RMS，确定真正的 PCM 数据口。

用法：python scripts/probe_ag35_pcm.py
接通后请对着电话持续说话或数数。
"""

from __future__ import annotations

import argparse
import audioop
import sys
import threading
import time

import serial


def send_at(port: serial.Serial, command: str, wait: float = 0.6) -> str:
    port.write(f"{command}\r".encode("ascii"))
    time.sleep(wait)
    deadline = time.time() + 3
    resp = ""
    while time.time() < deadline:
        chunk = port.read(port.in_waiting or 1)
        if chunk:
            resp += chunk.decode("ascii", errors="ignore")
        if "OK" in resp or "ERROR" in resp:
            break
        time.sleep(0.05)
    return resp.strip()


def wait_for_call(at_port: serial.Serial, timeout: int) -> str | None:
    deadline = time.time() + timeout
    seen: set[str] = set()
    while time.time() < deadline:
        resp = send_at(at_port, "AT+CLCC", wait=0.25)
        for line in resp.splitlines():
            line = line.strip()
            if not line.startswith("+CLCC:"):
                continue
            parts = line.removeprefix("+CLCC:").strip().split(",")
            if len(parts) < 3:
                continue
            cid, direction, status = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if direction == "1" and status == "4" and cid not in seen:
                seen.add(cid)
                return cid
        time.sleep(0.5)
    return None


def monitor_port(name: str, baud: int, seconds: float, results: dict) -> None:
    stats = {"bytes": 0, "max_rms": 0, "error": ""}
    results[name] = stats
    try:
        with serial.Serial(name, baud, timeout=0.05) as p:
            p.reset_input_buffer()
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                data = p.read(p.in_waiting or 640)
                if data:
                    stats["bytes"] += len(data)
                    if len(data) >= 2:
                        stats["max_rms"] = max(stats["max_rms"], audioop.rms(data, 2))
                else:
                    time.sleep(0.01)
    except Exception as e:  # noqa: BLE001
        stats["error"] = str(e)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AG35 PCM 数据口探测")
    parser.add_argument("--at-port", default="COM36")
    parser.add_argument("--candidates", default="COM34,COM35")
    parser.add_argument("--pcm-baud", type=int, default=921600)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]

    with serial.Serial(args.at_port, 115200, timeout=0.2, write_timeout=1) as at:
        print(send_at(at, "ATE0"))
        print(send_at(at, "AT+CMEE=2"))
        print(send_at(at, "AT+CLIP=1"))
        print(send_at(at, "AT+QAUDMOD=3"))
        print(send_at(at, 'AT+QGPSCFG="outport","none"'))
        print(send_at(at, "AT+QPCMV=0"))
        print("QPCMV=1,0 ->", send_at(at, "AT+QPCMV=1,0"))
        print("QPCMV? ->", send_at(at, "AT+QPCMV?"))
        print(f"等待来电 {args.timeout} 秒，请现在拨打...")

        cid = wait_for_call(at, args.timeout)
        if not cid:
            print("未检测到来电")
            return
        print(f"检测到来电 call_id={cid}，发送 ATA")
        print(send_at(at, "ATA", wait=1.2))

        print(f"接听成功，开始监听候选口 {candidates} 共 {args.seconds} 秒，请对电话持续说话...")
        results: dict = {}
        threads = [
            threading.Thread(
                target=monitor_port,
                args=(name, args.pcm_baud, args.seconds, results),
                daemon=True,
            )
            for name in candidates
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print(send_at(at, "ATH"))
        print(send_at(at, "AT+QPCMV=0"))

    print("==== 探测结果 ====")
    for name in candidates:
        s = results.get(name, {})
        print(f"{name}: bytes={s.get('bytes')}, max_rms={s.get('max_rms')}, error={s.get('error')}")
    print("bytes 明显非零、且 max_rms 跟随说话变化的口，就是 PCM 数据口。")


if __name__ == "__main__":
    main()
