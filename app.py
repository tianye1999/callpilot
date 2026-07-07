"""AgentCall 一键入口：启动模组来电服务 + 网页仪表盘。

用法：
    python app.py
浏览器会自动打开 http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import webbrowser

from dotenv import load_dotenv

from aiohttp import web

from src.call_agent import CallAgentService
from src.events import EventHub
from src.web.server import build_app


def _force_utf8() -> None:
    """把标准输出改成 UTF-8，避免中文日志乱码（Windows GBK 控制台）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    load_dotenv()
    _force_utf8()

    log_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )
    logger = logging.getLogger("app")
    logger.info("日志文件: %s", log_file)

    provider = os.getenv("AGENT_PROVIDER", "qwen")
    if provider == "qwen" and not os.getenv("DASHSCOPE_API_KEY"):
        print("错误: 使用千问需设置 DASHSCOPE_API_KEY", file=sys.stderr)
        sys.exit(1)

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    modem_port = os.getenv("MODEM_PORT", "COM3")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    store_path = os.path.join(os.path.dirname(__file__), "data", "messages.json")
    hub = EventHub(loop, store_path=store_path)

    service = CallAgentService(
        modem_port=modem_port,
        audio_keyword=os.getenv("MODEM_AUDIO_KEYWORD", "EC20"),
        provider=provider,
        baudrate=int(os.getenv("MODEM_BAUD", "115200")),
        audio_mode=os.getenv("MODEM_AUDIO_MODE", "uac"),
        pcm_port=os.getenv("MODEM_PCM_PORT"),
        pcm_baudrate=int(os.getenv("MODEM_PCM_BAUD", "921600")),
        tx_gain=float(os.getenv("MODEM_TX_GAIN", "1.0")),
        hub=hub,
    )

    meta = {
        "provider": provider,
        "model": (
            os.getenv("AGENT_MODEL_NAME", "通义千问 Qwen3-Omni")
            if provider == "qwen"
            else os.getenv("AGENT_MODEL_NAME_DOUBAO", "豆包实时语音大模型")
        ),
        "port": modem_port,
    }

    try:
        service.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("模组启动失败: %s", exc)
        sys.exit(1)

    app = build_app(hub, service.modem, service=service, meta=meta)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host, port)
    loop.run_until_complete(site.start())

    url = f"http://{host}:{port}"
    logger.info("网页仪表盘已启动: %s", url)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("收到退出信号，正在关闭…")
    finally:
        service.session.stop()
        service.modem.close()
        loop.run_until_complete(runner.cleanup())
        loop.close()


if __name__ == "__main__":
    main()
