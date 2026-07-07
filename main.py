"""AgentCall 入口：EG25 来电自动接入千问/豆包 Agent。"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from agentcall.call_agent import CallAgentService


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="EG25 来电 AI Agent 服务")
    parser.add_argument("--port", default=os.getenv("MODEM_PORT", "COM3"))
    parser.add_argument("--baud", type=int, default=int(os.getenv("MODEM_BAUD", "115200")))
    parser.add_argument(
        "--audio-keyword",
        default=os.getenv("MODEM_AUDIO_KEYWORD", "EG25"),
        help="USB 声卡名称关键字",
    )
    parser.add_argument(
        "--audio-mode",
        choices=["uac", "nmea"],
        default=os.getenv("MODEM_AUDIO_MODE", "uac"),
        help="模组音频模式：uac=USB声卡，nmea=USB NMEA串口PCM",
    )
    parser.add_argument("--pcm-port", default=os.getenv("MODEM_PCM_PORT"))
    parser.add_argument(
        "--pcm-baud",
        type=int,
        default=int(os.getenv("MODEM_PCM_BAUD", "921600")),
    )
    parser.add_argument(
        "--tx-gain",
        type=float,
        default=float(os.getenv("MODEM_TX_GAIN", "1.0")),
        help="写回电话侧的 PCM 音量增益",
    )
    parser.add_argument(
        "--provider",
        choices=["qwen", "doubao"],
        default=os.getenv("AGENT_PROVIDER", "qwen"),
    )
    parser.add_argument("--list-audio", action="store_true", help="列出音频设备后退出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.list_audio:
        import sounddevice as sd

        print(sd.query_devices())
        return

    if args.provider == "qwen" and not os.getenv("DASHSCOPE_API_KEY"):
        print("错误: 使用千问需设置 DASHSCOPE_API_KEY", file=sys.stderr)
        sys.exit(1)

    service = CallAgentService(
        modem_port=args.port,
        audio_keyword=args.audio_keyword,
        provider=args.provider,
        baudrate=args.baud,
        audio_mode=args.audio_mode,
        pcm_port=args.pcm_port,
        pcm_baudrate=args.pcm_baud,
        tx_gain=args.tx_gain,
    )
    service.run()


if __name__ == "__main__":
    main()
