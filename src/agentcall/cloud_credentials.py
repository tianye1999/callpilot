"""Operating-system credential storage for the hosted CallPilot control plane."""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SERVICE = "ai.bondings.callpilot.cloud"
_ACCOUNT = "edge-credential"
_KEY_ACCOUNT = "edge-device-key"
_CREDENTIAL_RE = re.compile(r"^(edge_[A-Za-z0-9_-]{12,80})\.([A-Za-z0-9_-]{32,256})$")


@dataclass(frozen=True)
class EdgeCredential:
    edge_id: str
    value: str


class CloudCredentialStore:
    """Store the Edge bearer in Keychain/Credential Manager through keyring."""

    def load(self) -> EdgeCredential | None:
        try:
            import keyring

            value = keyring.get_password(_SERVICE, _ACCOUNT)
        except Exception as exc:  # keyring backends intentionally vary by OS
            logger.warning(
                "读取云端设备凭证失败: error_type=%s", type(exc).__name__
            )
            return None
        return parse_edge_credential(value) if value else None

    def save(self, value: str) -> EdgeCredential:
        credential = parse_edge_credential(value)
        if credential is None:
            raise ValueError("云端返回的设备凭证格式不合法")
        try:
            import keyring

            keyring.set_password(_SERVICE, _ACCOUNT, credential.value)
        except Exception as exc:
            raise RuntimeError("无法把云端设备凭证写入系统钥匙串") from exc
        return credential

    def clear(self) -> None:
        try:
            import keyring
            from keyring.errors import PasswordDeleteError

            try:
                keyring.delete_password(_SERVICE, _ACCOUNT)
            except PasswordDeleteError:
                pass
        except Exception as exc:
            logger.warning(
                "删除云端设备凭证失败: error_type=%s", type(exc).__name__
            )

    def load_or_create_public_key(self) -> str:
        """Return a stable Ed25519 public key while keeping its seed in Keychain."""

        try:
            import keyring
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )

            encoded = keyring.get_password(_SERVICE, _KEY_ACCOUNT)
            if encoded:
                seed = _decode_key(encoded)
                private_key = Ed25519PrivateKey.from_private_bytes(seed)
            else:
                private_key = Ed25519PrivateKey.generate()
                seed = private_key.private_bytes_raw()
                keyring.set_password(_SERVICE, _KEY_ACCOUNT, _encode_key(seed))
            return _encode_key(private_key.public_key().public_bytes_raw())
        except Exception as exc:
            raise RuntimeError("无法创建或读取云端设备密钥") from exc


def parse_edge_credential(value: str) -> EdgeCredential | None:
    match = _CREDENTIAL_RE.fullmatch(value or "")
    if match is None:
        return None
    return EdgeCredential(edge_id=match.group(1), value=value)


def _encode_key(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_key(value: str) -> bytes:
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    if len(raw) != 32:
        raise ValueError("invalid device key")
    return raw


__all__ = ["CloudCredentialStore", "EdgeCredential", "parse_edge_credential"]
