#!/usr/bin/env python3
"""Run the LAN-only CallPilot browser dialer POC.

The normal AgentCall web app stays untouched.  This entrypoint starts a small
HTTPS server for a phone browser and bridges that browser audio directly to the
Dongle SIM call.
"""

from __future__ import annotations

import argparse
import http.server
import logging
import secrets
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from agentcall import config
from agentcall.lan_web_dialer import (
    LanDialerController,
    LanDialerSettings,
    build_lan_dialer_app,
)
from agentcall.modem import Eg25Modem


def _lan_ip() -> str:
    for interface in ("en0", "en1", "en2", "en3"):
        try:
            result = subprocess.run(
                ["ipconfig", "getifaddr", interface],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            continue
        candidate = result.stdout.strip()
        if candidate.startswith(("192.168.", "172.16.", "172.17.", "172.18.", "172.19.")):
            return candidate
        if candidate.startswith(("172.2", "172.3")):
            return candidate
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def _ensure_self_signed_cert(cert_dir: Path, lan_ip: str) -> tuple[Path, Path]:
    cert_dir.mkdir(parents=True, exist_ok=True)
    suffix = lan_ip.replace(".", "-").replace(":", "-")
    cert = cert_dir / f"callpilot-lan-{suffix}.crt"
    key = cert_dir / f"callpilot-lan-{suffix}.key"
    if cert.exists() and key.exists():
        key.chmod(0o600)
        return cert, key
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "30",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=CallPilot LAN Dialer",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
            "-addext",
            "extendedKeyUsage=serverAuth",
            "-addext",
            f"subjectAltName=IP:{lan_ip},DNS:localhost",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    return cert, key


class _CertHandler(http.server.BaseHTTPRequestHandler):
    cert_path: Path

    def do_GET(self) -> None:
        if self.path != "/callpilot-lan.crt":
            self.send_error(404)
            return
        body = self.cert_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-x509-ca-cert")
        self.send_header("Content-Disposition", 'attachment; filename="callpilot-lan.crt"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _start_cert_server(cert: Path, port: int) -> http.server.ThreadingHTTPServer | None:
    if port <= 0:
        return None
    _CertHandler.cert_path = cert
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _CertHandler)
    thread = threading.Thread(target=server.serve_forever, name="lan-cert-server", daemon=True)
    thread.start()
    return server


def _ssl_context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    return context


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the LAN-only CallPilot browser dialer POC.")
    parser.add_argument("--host", default="0.0.0.0", help="listen host; use 0.0.0.0 for LAN phones")
    parser.add_argument("--port", type=int, default=47443, help="HTTPS port")
    parser.add_argument(
        "--cert-port",
        type=int,
        default=47444,
        help="HTTP port for phone certificate download; 0 disables",
    )
    parser.add_argument("--token", default="", help="access token; defaults to a random per-run token")
    parser.add_argument("--cert", default="", help="TLS certificate path")
    parser.add_argument("--key", default="", help="TLS private key path")
    parser.add_argument(
        "--cert-dir",
        default=str(Path.home() / ".callpilot" / "lan-dialer"),
        help="where to create the temporary self-signed certificate when --cert/--key are omitted",
    )
    parser.add_argument("--connect-timeout", type=float, default=45.0, help="seconds to wait for B to answer")
    return parser


def main() -> int:
    load_dotenv(config.env_file_path())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parser().parse_args()
    lan_ip = _lan_ip()
    token = args.token or secrets.token_urlsafe(18)

    if bool(args.cert) != bool(args.key):
        print("--cert 和 --key 必须同时提供", file=sys.stderr)
        return 2
    if args.cert and args.key:
        cert, key = Path(args.cert).expanduser(), Path(args.key).expanduser()
    else:
        cert, key = _ensure_self_signed_cert(Path(args.cert_dir).expanduser(), lan_ip)

    settings = LanDialerSettings(
        audio_mode=config.get_str("MODEM_AUDIO_MODE"),
        audio_keyword=config.get_str("MODEM_AUDIO_KEYWORD"),
        pcm_port=config.get_str("MODEM_PCM_PORT") or None,
        pcm_baudrate=config.get_int("MODEM_PCM_BAUD"),
        tx_gain=config.get_float("MODEM_TX_GAIN"),
        connect_timeout_seconds=args.connect_timeout,
    )
    modem = Eg25Modem(config.get_str("MODEM_PORT"), config.get_int("MODEM_BAUD"))
    controller = LanDialerController(modem, settings=settings)
    app = build_lan_dialer_app(controller, token=token)

    cert_server = _start_cert_server(cert, args.cert_port)
    print(f"Certificate URL: http://{lan_ip}:{args.cert_port}/callpilot-lan.crt", flush=True)
    print(f"iPhone URL:      https://{lan_ip}:{args.port}/?token={token}", flush=True)
    print(f"TLS cert:        {cert}", flush=True)
    print("首次在手机上使用时，先用 Certificate URL 安装证书并设为完全信任。", flush=True)

    try:
        modem.connect()
        modem.initialize_for_voice(settings.audio_mode)
        modem.start_listener()
        web.run_app(
            app,
            host=args.host,
            port=args.port,
            ssl_context=_ssl_context(cert, key),
            access_log=None,
            print=None,
        )
    finally:
        if cert_server is not None:
            cert_server.shutdown()
        modem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
