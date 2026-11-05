"""Operating-system credential storage for the hosted CallPilot control plane."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agentcall import config, platforms

logger = logging.getLogger(__name__)

_SERVICE = "ai.bondings.callpilot.cloud"
_ACCOUNT = "edge-credential"
_KEY_ACCOUNT = "edge-device-key"
_CREDENTIAL_RE = re.compile(r"^(edge_[A-Za-z0-9_-]{12,80})\.([A-Za-z0-9_-]{32,256})$")


class _FileBackend:
    def __init__(self) -> None:
        self.path = config.data_dir() / "cloud-credentials.json"

    def _read(self) -> dict[str, str]:
        try:
            contents = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(contents, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in contents.items()
        ):
            raise ValueError("Invalid cloud credential file")
        return contents

    def _write(self, contents: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".cloud-credentials-", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                json.dump(contents, temporary)
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def get_password(self, service: str, username: str) -> str | None:
        return self._read().get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        contents = self._read()
        contents[username] = password
        self._write(contents)

    def delete_password(self, service: str, username: str) -> None:
        contents = self._read()
        if username in contents:
            del contents[username]
            self._write(contents)


def _backend():
    if platforms.IS_LINUX:
        return _FileBackend()
    import keyring

    return keyring


@dataclass(frozen=True)
class EdgeCredential:
    edge_id: str
    value: str


class CloudCredentialStore:
    """Store cloud secrets in a private Linux file or the OS credential manager."""

    def load(self) -> EdgeCredential | None:
        try:
            value = _backend().get_password(_SERVICE, _ACCOUNT)
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
            _backend().set_password(_SERVICE, _ACCOUNT, credential.value)
        except Exception as exc:
            raise RuntimeError("无法把云端设备凭证写入系统钥匙串") from exc
        return credential

    def clear(self) -> None:
        try:
            backend = _backend()
            if platforms.IS_LINUX:
                backend.delete_password(_SERVICE, _ACCOUNT)
            else:
                from keyring.errors import PasswordDeleteError

                try:
                    backend.delete_password(_SERVICE, _ACCOUNT)
                except PasswordDeleteError:
                    pass
        except Exception as exc:
            logger.warning(
                "删除云端设备凭证失败: error_type=%s", type(exc).__name__
            )

    def load_or_create_public_key(self) -> str:
        """Return a stable Ed25519 public key while keeping its seed private."""

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )

            private_key = _load_or_create_private_key(_backend(), Ed25519PrivateKey)
            return _encode_key(private_key.public_key().public_bytes_raw())
        except Exception as exc:
            raise RuntimeError("无法创建或读取云端设备密钥") from exc

    def sign(self, message: bytes) -> str:
        if not message or len(message) > 4096:
            raise ValueError("设备签名消息格式不合法")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )

            private_key = _load_or_create_private_key(_backend(), Ed25519PrivateKey)
            return _encode_key(private_key.sign(message))
        except Exception as exc:
            raise RuntimeError("无法使用云端设备密钥签名") from exc


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


def _load_or_create_private_key(keyring_module, key_type):
    encoded = keyring_module.get_password(_SERVICE, _KEY_ACCOUNT)
    if encoded:
        return key_type.from_private_bytes(_decode_key(encoded))
    private_key = key_type.generate()
    keyring_module.set_password(
        _SERVICE, _KEY_ACCOUNT, _encode_key(private_key.private_bytes_raw())
    )
    return private_key


__all__ = ["CloudCredentialStore", "EdgeCredential", "parse_edge_credential"]
