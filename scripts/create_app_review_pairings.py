#!/usr/bin/env python3
"""Create one-time App Review pairing codes without exposing Edge credentials."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_PAIRING_CODE_RE = re.compile(r"^[23456789A-HJ-NP-Z]{4}-[23456789A-HJ-NP-Z]{4}$")

from agentcall import config  # noqa: E402
from agentcall.cloud_control import CloudControlApi  # noqa: E402
from agentcall.cloud_credentials import CloudCredentialStore  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create bounded, one-time pairing codes for App Store review.",
    )
    parser.add_argument("--count", type=int, default=3, help="number of codes (1-5)")
    parser.add_argument(
        "--ttl-hours",
        type=int,
        default=72,
        help="code lifetime in hours (1-168)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.count <= 5:
        _parser().error("--count must be between 1 and 5")
    if not 1 <= args.ttl_hours <= 168:
        _parser().error("--ttl-hours must be between 1 and 168")

    load_dotenv(ROOT / ".env", override=False)
    store = CloudCredentialStore()
    credential = store.load()
    if credential is None:
        raise SystemExit("No enrolled Edge credential is available in the system keychain.")

    api = CloudControlApi(
        config.get_str("REMOTE_CLOUD_URL"),
        sign=store.sign,
    )
    ttl_seconds = args.ttl_hours * 60 * 60
    print("Codes are shown once. Put them only in App Review Notes.", file=sys.stderr)
    for _ in range(args.count):
        pairing = api.create_pairing(
            credential,
            ttl_seconds=ttl_seconds,
            purpose="app_review",
        )
        code = str(pairing.get("code") or "")
        expires_at = pairing.get("expiresAt")
        if _PAIRING_CODE_RE.fullmatch(code) is None:
            raise RuntimeError("Cloud returned an invalid pairing code")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int | float):
            raise RuntimeError("Cloud returned an incomplete pairing response")
        expires = datetime.fromtimestamp(expires_at / 1000, UTC).isoformat()
        print(f"{code}\texpires={expires}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
