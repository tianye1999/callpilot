"""用 OpenAI 兼容接口测试千问文本对话。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def chat_once(message: str, model: str) -> str:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("DASHSCOPE_OPENAI_BASE_URL", "").rstrip("/")
    if not api_key or not base_url:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY 或 DASHSCOPE_OPENAI_BASE_URL")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你叫红茶语音助手，是电话 Agent 的文本调试助手，请用中文简短回答。",
            },
            {"role": "user", "content": message},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {error_body}") from exc

    return data["choices"][0]["message"]["content"]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="测试千问文本对话")
    parser.add_argument("message", nargs="*", help="要发送给模型的文本")
    parser.add_argument(
        "--model",
        default=os.getenv("QWEN_TEXT_MODEL", "qwen3.7-plus-2026-05-26"),
        help="文本模型名",
    )
    args = parser.parse_args()

    message = " ".join(args.message).strip() or "你好，你是什么模型？"
    try:
        reply = chat_once(message, args.model)
    except Exception as exc:  # noqa: BLE001
        print(f"请求失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print("模型回复:")
    print(reply)


if __name__ == "__main__":
    main()
