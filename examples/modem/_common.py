"""examples/modem 共用助手：按 .env/平台默认构造一个 Eg25Modem。

所有示例都走配置注册表（`agentcall.config`）而非硬编码：端口、波特率等取自
`.env` 或平台默认值，与主程序完全一致——改 `.env` 即改示例行为。
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

from agentcall import config
from agentcall.modem import Eg25Modem


def setup_logging() -> None:
    """统一日志格式，方便看到 modem 内部的 AT 交互日志。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def make_modem() -> Eg25Modem:
    """构造一个未连接的 Eg25Modem（端口/波特率来自 MODEM_PORT / MODEM_BAUD）。

    调用方自行 `connect()`；需要监听来电/短信的示例再 `start_listener()`；
    用完 `close()`。
    """
    load_dotenv()
    return Eg25Modem(
        config.get_str("MODEM_PORT"),
        baudrate=config.get_int("MODEM_BAUD"),
    )
