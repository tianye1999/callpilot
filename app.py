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

from agentcall import config
from agentcall.call_agent import CallAgentService
from agentcall.events import EventHub
from agentcall.web.server import build_app


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

    provider = config.get_str("AGENT_PROVIDER")
    credential_errors = config.validate_provider_credentials(provider)
    if credential_errors:
        for message in credential_errors:
            print(f"错误: {message}", file=sys.stderr)
        sys.exit(1)

    # 启动期 fail-fast：uac_ffmpeg 仅 macOS 可用。不在这里拦，守卫要到
    # 通话建桥时才抛，对端每通都是「接通即挂」，远比启动报错难排查。
    from agentcall import platforms

    if config.get_str("MODEM_AUDIO_MODE").lower() == "uac_ffmpeg" and not platforms.IS_MACOS:
        print(
            "错误: MODEM_AUDIO_MODE=uac_ffmpeg 仅支持 macOS，"
            "本平台请改用 MODEM_AUDIO_MODE=uac",
            file=sys.stderr,
        )
        sys.exit(1)

    # Qwen 连接预热：提前建好 TLS 连接，降低首通接听延迟。
    # start_prewarm_keepalive 由 W2 实现，未就绪时跳过即可，不阻塞启动。
    if provider == "qwen" and config.get_bool("QWEN_PREWARM"):
        try:
            from agentcall.agents.qwen_agent import start_prewarm_keepalive

            prewarm_thread = start_prewarm_keepalive()
            logger.info("Qwen 连接预热已启动")
        except Exception as exc:  # noqa: BLE001
            prewarm_thread = None
            logger.warning("Qwen 连接预热启动失败，已跳过: %s", exc)
    else:
        prewarm_thread = None

    # 统一从 config 注册表读（默认值单一来源），避免与注册默认漂移。
    host = config.get_str("WEB_HOST")
    port = config.get_int("WEB_PORT")
    modem_port = config.get_str("MODEM_PORT")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    store_path = os.path.join(os.path.dirname(__file__), "data", "messages.json")
    hub = EventHub(loop, store_path=store_path)

    service = CallAgentService(
        modem_port=modem_port,
        audio_keyword=config.get_str("MODEM_AUDIO_KEYWORD"),
        provider=provider,
        baudrate=config.get_int("MODEM_BAUD"),
        audio_mode=config.get_str("MODEM_AUDIO_MODE"),
        pcm_port=config.get_str("MODEM_PCM_PORT") or None,
        pcm_baudrate=config.get_int("MODEM_PCM_BAUD"),
        tx_gain=config.get_float("MODEM_TX_GAIN"),
        hub=hub,
    )

    # provider -> 模型显示名的注册表 key（未知 provider 回落 qwen 显示名）。
    model_name_keys = {
        "qwen": "AGENT_MODEL_NAME",
        "doubao": "AGENT_MODEL_NAME_DOUBAO",
        "openai": "AGENT_MODEL_NAME_OPENAI",
    }
    meta = {
        "provider": provider,
        "model": config.get_str(model_name_keys.get(provider, "AGENT_MODEL_NAME")),
        "port": modem_port,
    }

    # 韧性启动：模组连接交给后台 supervisor 反复重试，Web 服务不因模组缺席而退出。
    service.start()

    dial_whitelist = config.get_str("DIAL_WHITELIST").strip()
    logger.info(
        "功能开关: 录音=%s(保留%s天) 摘要=%s 本地监听=%s 外呼白名单=%s",
        "开" if config.get_bool("RECORDING_ENABLED") else "关",
        config.get_int("RECORDING_RETENTION_DAYS"),
        "开" if config.get_bool("SUMMARY_ENABLED") else "关",
        "开" if service.monitor is not None else "关",
        dial_whitelist or "未设置(全部放行)",
    )

    # 需重启配置的自愈重启：/api/restart 置位该事件 → 停 loop → 清理后 os.execv。
    restart_event = threading.Event()
    app = build_app(
        hub, service.modem, service=service, meta=meta, restart_event=restart_event
    )
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
        if prewarm_thread is not None:
            prewarm_thread.stop_event.set()
        service.stop_service()
        if service.monitor is not None:
            service.monitor.stop()
        if service.uplink_monitor is not None:
            service.uplink_monitor.stop()
        loop.run_until_complete(runner.cleanup())
        loop.close()

    # 端口已随 runner.cleanup() 释放；此时原地重启（重读 .env）可干净重新绑定。
    if restart_event.is_set():
        logger.info("按请求重启服务以应用需重启的配置…")
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()
