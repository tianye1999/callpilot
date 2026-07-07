"""本地麦克风/扬声器测试千问 Realtime 对话。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import sounddevice as sd
from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents.factory import create_agent  # noqa: E402


async def run_voice_test(
    input_device: int | None,
    output_device: int | None,
    seconds: int,
    greet: bool = False,
) -> None:
    agent = create_agent("qwen")
    input_block_size = int(agent.input_rate * 0.02)
    output_block_size = int(agent.output_rate * 0.02)

    output_stream = sd.RawOutputStream(
        samplerate=agent.output_rate,
        blocksize=output_block_size,
        dtype="int16",
        channels=1,
        device=output_device,
    )
    input_stream = sd.RawInputStream(
        samplerate=agent.input_rate,
        blocksize=input_block_size,
        dtype="int16",
        channels=1,
        device=input_device,
    )

    def play_agent_audio(pcm: bytes) -> None:
        if pcm:
            output_stream.write(pcm)

    output_stream.start()
    input_stream.start()
    await agent.start(play_agent_audio)

    print("已连接千问 Realtime。请直接对着麦克风说话，停顿后模型会语音回复。")
    print("你说的=[上行·用户]，Agent 说的=[下行·Agent]。按 Ctrl+C 可提前结束。")

    if greet:
        await agent.say("请用中文简短自我介绍，并说明你的底层模型名称。")

    loop = asyncio.get_running_loop()
    end_at = loop.time() + seconds if seconds > 0 else None

    try:
        while end_at is None or loop.time() < end_at:
            pcm, overflowed = input_stream.read(input_block_size)
            if overflowed:
                logging.warning("麦克风输入发生 overflow")
            await agent.send_audio(bytes(pcm))
            await asyncio.sleep(0.005)
    finally:
        await agent.stop()
        input_stream.stop()
        output_stream.stop()
        input_stream.close()
        output_stream.close()


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="测试千问 Realtime 语音对话")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    parser.add_argument("--input-device", type=int, default=None, help="麦克风设备 index")
    parser.add_argument("--output-device", type=int, default=None, help="扬声器设备 index")
    parser.add_argument("--seconds", type=int, default=120, help="测试时长，0 表示一直运行")
    parser.add_argument(
        "--greet",
        action="store_true",
        help="接通后让 Agent 先主动自我介绍(便于验证下行文字打印)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.list_devices:
        print(sd.query_devices())
        return

    try:
        asyncio.run(
            run_voice_test(
                args.input_device, args.output_device, args.seconds, args.greet
            )
        )
    except KeyboardInterrupt:
        print("\n已结束测试")


if __name__ == "__main__":
    main()
