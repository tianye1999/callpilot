"""EG25 UAC 声卡自检。

用途：在实拨电话之前，独立验证 EG25 的 UAC 声卡是否能双向工作。
流程：
  1. 枚举所有音频设备，找出 "AC Interface"（或自定义关键字）的输入/输出端点；
  2. 依次尝试以 8000Hz、16000Hz 打开输入+输出流（与 ModemAudioBridge 的回退策略一致）；
  3. 录音 3 秒统计输入峰值（验证上行/麦克风通路）；
  4. 播放 1 秒 440Hz 测试音（验证下行/喇叭通路，通话另一端应能听到）。

用法：
  python scripts/uac_check.py                # 默认关键字 "AC Interface"
  python scripts/uac_check.py --keyword EG25 # 自定义设备名关键字
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np
import sounddevice as sd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BLOCK_MS = 20
CHANNELS = 1
DTYPE = "int16"


def find(keyword: str, kind: str):
    kw = keyword.lower()
    for idx, dev in enumerate(sd.query_devices()):
        name = str(dev.get("name", "")).lower()
        if kw not in name:
            continue
        if kind == "input" and dev.get("max_input_channels", 0) <= 0:
            continue
        if kind == "output" and dev.get("max_output_channels", 0) <= 0:
            continue
        return idx, dev["name"]
    return None, None


def try_rate(in_idx: int, out_idx: int, rate: int) -> bool:
    block = int(rate * BLOCK_MS / 1000)
    in_s = out_s = None
    try:
        in_s = sd.RawInputStream(
            samplerate=rate, blocksize=block, dtype=DTYPE,
            channels=CHANNELS, device=in_idx,
        )
        out_s = sd.RawOutputStream(
            samplerate=rate, blocksize=block, dtype=DTYPE,
            channels=CHANNELS, device=out_idx,
        )
        in_s.start()
        out_s.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[{rate}Hz] 开流失败: {exc}")
        for s in (in_s, out_s):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
        return False

    print(f"\n[{rate}Hz] 开流成功，录音 3 秒，请对着话筒/电话说话...")
    peak = 0
    for _ in range(int(3000 / BLOCK_MS)):
        data, _overflow = in_s.read(block)
        arr = np.frombuffer(bytes(data), dtype=np.int16)
        if arr.size:
            peak = max(peak, int(np.abs(arr).max()))
    verdict = "有声音输入 ✓" if peak > 500 else "几乎静音 ✗"
    print(f"[{rate}Hz] 输入峰值={peak}（>500 视为正常）-> {verdict}")

    print(f"[{rate}Hz] 播放 1 秒 440Hz 测试音（电话另一端应能听到）...")
    t = np.arange(int(rate * 1.0)) / rate
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes()
    step = block * 2
    for i in range(0, len(tone), step):
        out_s.write(tone[i:i + step])
    time.sleep(0.3)

    for s in (in_s, out_s):
        try:
            s.stop()
            s.close()
        except Exception:
            pass
    print(f"[{rate}Hz] 自检完成。")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="EG25 UAC 声卡自检")
    parser.add_argument("--keyword", default="AC Interface", help="设备名关键字")
    args = parser.parse_args()

    print("=== 所有音频设备 ===")
    for i, d in enumerate(sd.query_devices()):
        print(f"[{i:2}] in={d['max_input_channels']} out={d['max_output_channels']} {d['name']}")

    in_idx, in_name = find(args.keyword, "input")
    out_idx, out_name = find(args.keyword, "output")
    print(f"\n关键字: '{args.keyword}'")
    print(f"输入设备: [{in_idx}] {in_name}")
    print(f"输出设备: [{out_idx}] {out_name}")
    if in_idx is None or out_idx is None:
        print("\n!! 未找到 UAC 输入/输出设备。")
        print("   请确认 EG25 已物理重插 USB、UAC 已通过 AT+QCFG=\"USBCFG\" 启用并枚举。")
        sys.exit(1)

    for rate in (8000, 16000):
        if try_rate(in_idx, out_idx, rate):
            print(
                f"\n结论：UAC 在 {rate}Hz 工作正常。"
                "若输入峰值>500 且对端听到 440Hz，则双向链路打通。"
            )
            return
    print("\n结论：8000Hz 与 16000Hz 均无法打开声卡，请检查 UAC 枚举与驱动。")
    sys.exit(2)


if __name__ == "__main__":
    main()
