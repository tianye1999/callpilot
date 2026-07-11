"""AgentCall 一键入口：启动模组来电服务 + 网页仪表盘。

用法：
    python app.py
浏览器会自动打开 http://127.0.0.1:47100
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import webbrowser

from aiohttp import web
from dotenv import load_dotenv

from agentcall import config, number_profiles
from agentcall.call_agent import CallAgentService
from agentcall.events import EventHub
from agentcall.remote_pairing import RemotePairingStore
from agentcall.web.remote_gateway import build_remote_gateway
from agentcall.web.server import build_app


def _open_browser_later(url: str, delay: float = 1.0) -> threading.Timer | None:
    if config._is_frozen():
        return None
    timer = threading.Timer(delay, lambda: webbrowser.open(url))
    timer.start()
    return timer


def _restart_after_cleanup() -> None:
    """Restart manually-run services in place; let launchd restart its own job.

    Re-executing the frozen child underneath ``caffeinate`` can race a second
    launchd instance for the listening port.  A launchd-managed service instead
    exits only after cleanup has released its sockets; ``KeepAlive`` then starts
    one fresh job with the persisted environment file.
    """
    if sys.platform == "darwin":
        from agentcall.macos_launchd import APP_LABEL

        if os.environ.get("XPC_SERVICE_NAME") == APP_LABEL:
            logging.getLogger("app").info("清理完成，退出并交由 launchd 重新启动服务…")
            return
    argv = (
        [sys.executable, *sys.argv[1:]]
        if config._is_frozen()
        else [sys.executable, *sys.argv]
    )
    os.execv(sys.executable, argv)


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
    load_dotenv(config.env_file_path())
    _force_utf8()

    log_dir = config.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"
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
            logger.warning("配置未完成: %s", message)

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

    # local provider 模型预下载：~300MB 必须在通话前备好，否则放到 agent.start()
    # （通话已接通）里下载会超过外呼时限，通话空转到超时（2026-07-10 真机实证）。
    # 后台线程下载，不阻塞 Web 启动；下载中来电会因模型未就绪走既有 fatal 降级。
    if provider == "local":
        def _prefetch_local_models() -> None:
            try:
                from agentcall import local_models

                missing = local_models.missing_assets()
                if missing:
                    logger.info(
                        "local provider 首次启动，后台预下载语音模型（%s，约 300MB）…",
                        ", ".join(a.id for a in missing),
                    )
                    local_models.ensure_all()
                    logger.info("local 语音模型已就绪")
            except Exception as exc:  # noqa: BLE001
                logger.warning("local 语音模型预下载失败（首次通话时会重试）: %s", exc)

        threading.Thread(target=_prefetch_local_models, name="local-model-prefetch", daemon=True).start()

    # Qwen 连接预热：提前建好 TLS 连接，降低首通接听延迟。
    # start_prewarm_keepalive 由 W2 实现，未就绪时跳过即可，不阻塞启动。
    if provider == "qwen" and config.get_bool("QWEN_PREWARM") and not credential_errors:
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

    # 非 loopback 监听必须带访问令牌：Web API 能拨号/发短信，裸监听等于把
    # 电话交给整个网段。fail-fast 拒绝启动，比静默裸奔清晰。
    web_auth_token = config.get_str("WEB_AUTH_TOKEN").strip()
    if not config.is_loopback_host(host) and not web_auth_token:
        logger.error(
            "WEB_HOST=%s 暴露到非本机网络，但未设置 WEB_AUTH_TOKEN；"
            "请在 .env 里设置访问令牌（客户端用 Authorization: Bearer <token> "
            "或 ?token=<token>），或将 WEB_HOST 改回 127.0.0.1", host,
        )
        sys.exit(2)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    data_dir = config.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    number_profiles.ensure_seeded()
    store_path = data_dir / "messages.json"
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
    meta = config.runtime_meta(
        provider=provider,
        model=config.get_str(model_name_keys.get(provider, "AGENT_MODEL_NAME")),
        port=modem_port,
    )

    # 韧性启动：模组连接交给后台 supervisor 反复重试，Web 服务不因模组缺席而退出。
    service.start()

    remote_pairing_store = None
    if config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
        remote_pairing_store = RemotePairingStore(
            data_dir / "remote_devices.json",
            max_devices=config.get_int("REMOTE_MAX_PAIRED_DEVICES"),
        )

    dial_whitelist = config.get_str("DIAL_WHITELIST").strip()
    logger.info(
        "功能开关: 录音=%s(保留%s天) 摘要=%s 本地监听=%s 外呼白名单=%s",
        "开" if config.get_bool("RECORDING_ENABLED") else "关",
        config.get_int("RECORDING_RETENTION_DAYS"),
        "开" if config.get_bool("SUMMARY_ENABLED") else "关",
        "开" if service.monitor is not None else "关",
        dial_whitelist or "未设置(全部放行)",
    )

    # 需重启配置的自愈重启：/api/restart 置位该事件 → 停 loop → 清理后重启。
    restart_event = threading.Event()
    app = build_app(
        hub,
        service.modem,
        service=service,
        meta=meta,
        restart_event=restart_event,
        # loopback 下不启用令牌校验（行为不变）；非 loopback 上面已保证 token 非空。
        auth_token=web_auth_token if not config.is_loopback_host(host) else None,
        remote_pairing_store=remote_pairing_store,
    )
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host, port)
    loop.run_until_complete(site.start())

    remote_runner = None
    if remote_pairing_store is not None:
        remote_app = build_remote_gateway(
            service,
            remote_pairing_store,
            public_url=config.get_str("REMOTE_CONTROL_URL"),
        )
        remote_runner = web.AppRunner(remote_app, access_log=None)
        try:
            loop.run_until_complete(remote_runner.setup())
            remote_site = web.TCPSite(
                remote_runner,
                "127.0.0.1",
                config.get_int("REMOTE_GATEWAY_PORT"),
            )
            loop.run_until_complete(remote_site.start())
            logger.info(
                "远程拨号最小权限网关已启动: http://127.0.0.1:%s",
                config.get_int("REMOTE_GATEWAY_PORT"),
            )
        except OSError as exc:
            logger.error("远程拨号网关启动失败: %s", exc)
            loop.run_until_complete(remote_runner.cleanup())
            remote_runner = None

    url = f"http://{host}:{port}"
    logger.info("网页仪表盘已启动: %s", url)
    _open_browser_later(url)

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
        if remote_runner is not None:
            loop.run_until_complete(remote_runner.cleanup())
        loop.run_until_complete(runner.cleanup())
        loop.close()

    # 端口已随 runner.cleanup() 释放；手动运行原地 exec，launchd 运行则退出交由
    # KeepAlive 拉起，避免 frozen 子进程与 launchd 新实例争抢同一端口。
    if restart_event.is_set():
        logger.info("按请求重启服务以应用需重启的配置…")
        _restart_after_cleanup()


def _selftest() -> int:
    """--selftest：只做 import 自检，不起服务。用于打包后验证关键模块真进了 bundle
    （教训：v0.5.0 DMG 漏了 local provider，find 文件名查不出 pyc，靠这个才能确证）。"""
    import importlib

    modules = [
        "agentcall.agents.factory",
        "agentcall.agents.qwen_agent",
        "agentcall.agents.openai_agent",
        "agentcall.agents.local_agent",
        "agentcall.local_models",
        "agentcall.sms_email_forwarder",
        "agentcall.remote_dialer",
        "agentcall.remote_pairing",
        "agentcall.livekit_media",
        "agentcall.web.remote_gateway",
        "livekit.rtc",
        "livekit.api",
    ]
    optional = {"sherpa_onnx"}
    ok = True
    for name in modules + list(optional):
        try:
            importlib.import_module(name)
            print(f"OK  {name}")
        except Exception as exc:  # noqa: BLE001
            marker = "WARN" if name in optional else "FAIL"
            if name not in optional:
                ok = False
            print(f"{marker} {name}: {type(exc).__name__}: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    main()
