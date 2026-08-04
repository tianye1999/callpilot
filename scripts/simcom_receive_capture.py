"""SIM7600 USB audio 纯接收抓音诊断。

只向 AT 接口发控制命令，从 audio interface 4 的 bulk IN 读取 PCM；绝不向
audio OUT 写数据，因此不会被当前已知的首包写超时提前打断。仅用于独占 USB
设备的人工诊断，运行前必须停止 CallPilot app/bridge/tray。

本机中国电信 SIM 的安全用法：
    .venv/bin/python scripts/simcom_receive_capture.py --number 10000 --count 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
import wave
from pathlib import Path

import numpy as np
import usb.core
import usb.util

ROOT = Path(__file__).resolve().parents[1]
AT_INTERFACE = 2
AT_OUT = 0x03
AT_IN = 0x84
AUDIO_INTERFACE = 4
AUDIO_IN = 0x88
SCAN_INTERFACES = {
    0: 0x81,
    1: 0x82,
    3: 0x86,
    4: 0x88,
    5: 0x8A,
}


def _response_text(raw: bytes) -> str:
    return " | ".join(
        line.strip()
        for line in raw.decode("ascii", errors="ignore").splitlines()
        if line.strip()
    )


def send_at(
    dev: usb.core.Device,
    command: str,
    *,
    timeout: float = 1.5,
) -> str:
    """发送一条 AT，读到最终 OK/ERROR 或超时。"""
    while True:
        try:
            dev.read(AT_IN, 512, timeout=30)
        except usb.core.USBTimeoutError:
            break
    dev.write(AT_OUT, f"{command}\r".encode("ascii"), timeout=1000)
    raw = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw.extend(dev.read(AT_IN, 512, timeout=200))
        except usb.core.USBTimeoutError:
            continue
        upper = bytes(raw).upper()
        if b"\r\nOK\r\n" in upper or b"ERROR" in upper:
            break
    return _response_text(bytes(raw))


def wait_for_connected(dev: usb.core.Device, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = send_at(dev, "AT+CLCC", timeout=0.8)
        for line in response.split(" | "):
            if line.startswith("+CLCC:"):
                fields = line.removeprefix("+CLCC:").strip().split(",")
                if len(fields) >= 3 and fields[2].strip() == "0":
                    return True
        time.sleep(0.5)
    return False


def enable_usb_audio(dev: usb.core.Device, timeout: float = 12.0) -> int:
    bandwidth = send_at(dev, "AT+CPCMBANDWIDTH=1,1")
    print(f"  CPCMBANDWIDTH: {bandwidth}")
    deadline = time.monotonic() + timeout
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        start = send_at(dev, "AT+CPCMREG=1")
        if "OK" in start.upper():
            state = send_at(dev, "AT+CPCMREG?")
            if "+CPCMREG: 1" in state:
                print(f"  CPCMREG: mode=1（第 {attempts} 次）")
                return attempts
            print(f"  CPCMREG 写 OK 但读回异常: {state}")
        time.sleep(0.5)
    raise RuntimeError(f"CPCMREG 在 {attempts} 次尝试后仍未读回 mode=1")


def capture_pcm(dev: usb.core.Device, seconds: float) -> tuple[bytes, float]:
    chunks: list[bytes] = []
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        try:
            chunks.append(bytes(dev.read(AUDIO_IN, 4096, timeout=250)))
        except usb.core.USBTimeoutError:
            continue
    return b"".join(chunks), time.monotonic() - started


def save_wav(path: Path, pcm: bytes, rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    even_pcm = pcm[: len(pcm) // 2 * 2]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(even_pcm)


def analyze(pcm: bytes, elapsed: float, rate: int = 8000) -> dict[str, float | int]:
    even_pcm = pcm[: len(pcm) // 2 * 2]
    samples = np.frombuffer(even_pcm, dtype="<i2").astype(np.float64)
    if samples.size == 0:
        return {
            "bytes": 0,
            "bytes_per_second": 0.0,
            "rms": 0.0,
            "peak": 0,
            "active_frames": 0,
            "frames": 0,
            "band_ratio_median": 0.0,
        }

    frame_samples = rate // 10
    frame_count = samples.size // frame_samples
    frames = samples[: frame_count * frame_samples].reshape(-1, frame_samples)
    frame_rms = np.sqrt(np.mean(frames * frames, axis=1)) if frame_count else np.array([])
    ratios: list[float] = []
    fft_size = 8192
    for offset in range(0, samples.size - fft_size + 1, fft_size):
        segment = samples[offset : offset + fft_size]
        spectrum = np.abs(np.fft.rfft(segment * np.hanning(fft_size))) ** 2
        freqs = np.fft.rfftfreq(fft_size, 1 / rate)
        low = spectrum[(freqs >= 300) & (freqs <= 1000)].sum()
        high = spectrum[(freqs >= 3000) & (freqs <= 4000)].sum()
        if high > 0:
            ratios.append(float(low / high))
    return {
        "bytes": len(even_pcm),
        "bytes_per_second": len(even_pcm) / elapsed if elapsed else 0.0,
        "rms": float(np.sqrt(np.mean(samples * samples))),
        "peak": int(np.max(np.abs(samples))),
        "active_frames": int(np.sum(frame_rms > 300)),
        "frames": int(frame_count),
        "band_ratio_median": float(np.median(ratios)) if ratios else 0.0,
    }


def print_metrics(metrics: dict[str, float | int]) -> None:
    print(
        "  capture: "
        f"{metrics['bytes']} bytes, {metrics['bytes_per_second']:.1f} B/s, "
        f"RMS={metrics['rms']:.1f}, peak={metrics['peak']}, "
        f"active={metrics['active_frames']}/{metrics['frames']}, "
        f"band_ratio={metrics['band_ratio_median']:.2f}"
    )


def set_control_lines(dev: usb.core.Device, interface: int) -> None:
    dev.ctrl_transfer(0x21, 0x22, 0x03, interface, None, timeout=1000)


def main() -> None:
    parser = argparse.ArgumentParser(description="SIM7600 USB audio 纯接收抓音")
    parser.add_argument("--number", default="10000")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument(
        "--reset-usb",
        action="store_true",
        help="独占设备后先复位 USB，清除 macOS 上可能残留的僵死端点",
    )
    parser.add_argument(
        "--scan-interfaces",
        action="store_true",
        help="每个候选 interface 依次抓取 --seconds 秒，定位当前音频端口",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "receive-captures",
    )
    args = parser.parse_args()
    if args.number != "10000":
        raise SystemExit("安全限制：此诊断只允许拨本机中国电信免费客服 10000")
    if args.count < 1 or args.count > 5:
        raise SystemExit("--count 必须在 1..5")

    dev = usb.core.find(idVendor=0x1E0E, idProduct=0x9001)
    if dev is None:
        raise SystemExit("未找到 SIMCom 1e0e:9001")
    if args.reset_usb:
        print("reset USB device...")
        dev.reset()
        time.sleep(1.5)
    try:
        dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()

    claimed: list[int] = []
    try:
        # 与已成功读到 16kB/s 的原始探针保持同一时序：先只打开 AT，等
        # CPCMREG 确认 mode=1 后再 claim audio interface。部分 SIM7600 固件
        # 似乎会观察 host 打开 audio port 的边沿，过早 claim 会得到 0 字节。
        usb.util.claim_interface(dev, AT_INTERFACE)
        claimed.append(AT_INTERFACE)
        set_control_lines(dev, AT_INTERFACE)

        print(f"ATE0: {send_at(dev, 'ATE0')}")
        for run in range(1, args.count + 1):
            print(f"\n=== run {run}/{args.count} ===")
            print(f"  pre-clean: {send_at(dev, 'AT+CHUP')}")
            time.sleep(0.8)
            clean_state = send_at(dev, "AT+CLCC")
            print(f"  CLCC before dial: {clean_state}")
            print(f"  dial: {send_at(dev, f'ATD{args.number};', timeout=3.0)}")
            if not wait_for_connected(dev):
                raise RuntimeError("25 秒内未接通")
            print("  connected")
            enable_usb_audio(dev)

            candidates = SCAN_INTERFACES if args.scan_interfaces else {4: AUDIO_IN}
            for interface, in_endpoint in candidates.items():
                usb.util.claim_interface(dev, interface)
                claimed.append(interface)
                try:
                    set_control_lines(dev, interface)
                except usb.core.USBError as exc:
                    # interface 0/1/3/5 未必接受 CDC SET_CONTROL_LINE_STATE；
                    # 全接口定位时这不应阻止读取其 bulk IN。
                    if not args.scan_interfaces:
                        raise
                    print(f"  interface {interface} DTR/RTS 不支持: {exc}")
                time.sleep(0.1)
                try:
                    if in_endpoint == AUDIO_IN:
                        pcm, elapsed = capture_pcm(dev, args.seconds)
                    else:
                        chunks: list[bytes] = []
                        started = time.monotonic()
                        deadline = started + args.seconds
                        while time.monotonic() < deadline:
                            try:
                                chunks.append(bytes(dev.read(in_endpoint, 4096, timeout=250)))
                            except usb.core.USBTimeoutError:
                                continue
                        pcm = b"".join(chunks)
                        elapsed = time.monotonic() - started
                finally:
                    usb.util.release_interface(dev, interface)
                    claimed.remove(interface)
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                output = (
                    args.output_dir
                    / f"simcom-10000-{stamp}-run{run}-if{interface}.wav"
                )
                save_wav(output, pcm)
                print(f"  interface {interface}:")
                print_metrics(analyze(pcm, elapsed))
                print(f"  saved: {output}")

            print(f"  hangup: {send_at(dev, 'AT+CHUP')}")
            print(f"  audio stop: {send_at(dev, 'AT+CPCMREG=0,1')}")
            time.sleep(1.0)
            print(f"  CLCC after hangup: {send_at(dev, 'AT+CLCC')}")
    finally:
        try:
            send_at(dev, "AT+CHUP")
            send_at(dev, "AT+CPCMREG=0,1")
        except usb.core.USBError:
            pass
        for interface in reversed(claimed):
            try:
                usb.util.release_interface(dev, interface)
            except usb.core.USBError:
                pass
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
