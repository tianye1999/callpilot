"""批量生成 Qwen Realtime 音色试听 WAV。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
import wave
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcall.agents.qwen_agent import QwenVoiceAgent  # noqa: E402
from agentcall.audio_bridge import resample_pcm  # noqa: E402


DEFAULT_VOICES = ["Alek", "Andre", "Raymond", "Dylan"]
DEFAULT_TEXT = (
    "你好，我是红茶语音助手。现在正在试听 {voice} 音色。"
    "这段声音会用于电话里的自然对话，请听听清晰度、亲和力和节奏感。"
)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")


def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


async def synthesize_voice(
    *,
    voice: str,
    text_template: str,
    output_dir: Path,
    model: str,
    display_name: str,
    max_seconds: float,
    idle_seconds: float,
) -> tuple[Path, Path, str]:
    audio_parts: list[bytes] = []
    transcript = ""
    last_audio_at = 0.0

    agent = QwenVoiceAgent(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        model=model,
        model_display_name=display_name,
        voice=voice,
        realtime_url=os.getenv("DASHSCOPE_REALTIME_URL"),
    )

    def on_audio(pcm: bytes) -> None:
        nonlocal last_audio_at
        if pcm:
            audio_parts.append(pcm)
            last_audio_at = time.monotonic()

    def on_transcript(role: str, text: str) -> None:
        nonlocal transcript
        if role == "agent" and text:
            transcript = text

    agent.set_transcript_handler(on_transcript)
    await agent.start(on_audio)
    await agent.say(
        "请严格只朗读下面这句话，不要解释，不要扩写："
        + text_template.format(voice=voice)
    )

    deadline = time.monotonic() + max_seconds
    try:
        while time.monotonic() < deadline:
            if audio_parts and last_audio_at and time.monotonic() - last_audio_at >= idle_seconds:
                break
            await asyncio.sleep(0.1)
    finally:
        await agent.stop()

    if not audio_parts:
        raise RuntimeError(f"{voice} 没有收到音频输出")

    pcm_24k = b"".join(audio_parts)
    pcm_8k = resample_pcm(pcm_24k, agent.output_rate, 8000)
    stem = _safe_name(voice)
    full_path = output_dir / f"{stem}_24k.wav"
    phone_path = output_dir / f"{stem}_phone_8k.wav"
    _write_wav(full_path, pcm_24k, agent.output_rate)
    _write_wav(phone_path, pcm_8k, 8000)
    return full_path, phone_path, transcript


async def main_async(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).expanduser().resolve()
    for voice in args.voices:
        full_path, phone_path, transcript = await synthesize_voice(
            voice=voice,
            text_template=args.text,
            output_dir=output_dir,
            model=args.model,
            display_name=args.display_name,
            max_seconds=args.max_seconds,
            idle_seconds=args.idle_seconds,
        )
        print(f"{voice}:")
        print(f"  full:  {full_path}")
        print(f"  phone: {phone_path}")
        if transcript:
            print(f"  text:  {transcript}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="生成 Qwen Realtime 音色试听文件")
    parser.add_argument("voices", nargs="*", default=DEFAULT_VOICES)
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "voice_samples"))
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument(
        "--model",
        default=os.getenv("QWEN_REALTIME_MODEL", "qwen3.5-omni-flash-realtime"),
    )
    parser.add_argument(
        "--display-name",
        default=os.getenv("AGENT_MODEL_NAME", "通义千问 Qwen3.5-Omni"),
    )
    parser.add_argument("--max-seconds", type=float, default=25.0)
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
