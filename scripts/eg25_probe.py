"""EG25 设备自检：扫描串口、对每个 Quectel 口探测 AT、查询 SIM/信号/网络/音频。"""

from __future__ import annotations

import sys
import time

import serial
import serial.tools.list_ports as list_ports

EG25_VIDPID = "2C7C:0125"


def find_quectel_ports() -> list[tuple[str, str]]:
    found = []
    for p in list_ports.comports():
        if EG25_VIDPID in (p.hwid or ""):
            found.append((p.device, p.description or ""))
    return found


def query(ser: serial.Serial, cmd: str, timeout: float = 2.0) -> str:
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    deadline = time.time() + timeout
    chunks: list[str] = []
    while time.time() < deadline:
        raw = ser.read(ser.in_waiting or 1)
        if raw:
            chunks.append(raw.decode("ascii", "ignore"))
            joined = "".join(chunks)
            if "OK" in joined or "ERROR" in joined:
                break
    return "".join(chunks).strip()


def probe_port(device: str, desc: str) -> bool:
    print(f"\n========== {device} | {desc} ==========")
    try:
        ser = serial.Serial(device, 115200, timeout=0.2, write_timeout=2)
    except serial.SerialException as exc:
        print(f"  [打开失败] {exc}")
        return False
    try:
        ser.dtr = True
        ser.rts = True
        time.sleep(0.2)
        at = query(ser, "AT")
        if "OK" not in at:
            print(f"  [无 AT 响应] 收到: {at!r}")
            return False
        print("  [OK] AT 通信正常，开始查询状态：")
        for cmd, label in [
            ("ATI", "型号/固件"),
            ("AT+CPIN?", "SIM 卡"),
            ("AT+CSQ", "信号强度"),
            ("AT+CEREG?", "LTE 注册"),
            ("AT+COPS?", "运营商"),
            ('AT+QCFG="USBCFG"', "USB/UAC 配置"),
        ]:
            resp = query(ser, cmd)
            resp = resp.replace("\r\n", " | ").replace("OK", "").strip(" |")
            print(f"    {label:10s}: {resp}")
        return True
    finally:
        ser.close()


def main() -> int:
    if len(sys.argv) > 1:
        ports = [(sys.argv[1], "手动指定")]
    else:
        ports = find_quectel_ports()
    if not ports:
        print("[FAIL] 未发现 Quectel/EG25 串口")
        return 1
    print(f"发现 {len(ports)} 个 Quectel 串口: {[p[0] for p in ports]}")
    any_ok = False
    for device, desc in ports:
        any_ok |= probe_port(device, desc)
    print("\n" + ("=" * 40))
    print("[结论] 至少一个口 AT 可用" if any_ok else "[结论] 所有口均无 AT 响应")
    return 0 if any_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
