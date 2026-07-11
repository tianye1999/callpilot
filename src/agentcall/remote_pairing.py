"""Local paired-device registry for the fixed Remote Web Dialer entry."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_PAIRING_CODE_RE = re.compile(r"[23456789A-HJ-NP-Z]{8}")
_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_DEVICE_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,64}")


class InvalidPairingCodeError(ValueError):
    pass


class PairingCapacityError(ValueError):
    pass


@dataclass(frozen=True)
class PairingOffer:
    code: str
    pairing_url: str
    expires_at: float


@dataclass(frozen=True)
class PairedDevice:
    device_id: str
    display_name: str
    created_at: float
    last_used_at: float
    revoked_at: float | None = None


@dataclass(frozen=True)
class DeviceCredential:
    device: PairedDevice
    secret: str


@dataclass
class _StoredDevice:
    device_id: str
    display_name: str
    secret_hash: str
    created_at: float
    last_used_at: float
    revoked_at: float | None = None

    def public(self) -> PairedDevice:
        return PairedDevice(
            device_id=self.device_id,
            display_name=self.display_name,
            created_at=self.created_at,
            last_used_at=self.last_used_at,
            revoked_at=self.revoked_at,
        )


class RemotePairingStore:
    """Thread-safe local store; only secret hashes are persisted."""

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], float] = time.time,
        max_devices: int = 5,
    ) -> None:
        self.path = Path(path)
        self._now = now
        self._max_devices = min(5, max(1, max_devices))
        self._lock = threading.RLock()
        self._offers: dict[str, float] = {}
        self._devices = self._load()

    def create_pairing(self, public_url: str, *, ttl_seconds: int = 300) -> PairingOffer:
        base_url = _validated_public_url(public_url)
        ttl = min(900, max(30, int(ttl_seconds)))
        raw = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        expires_at = self._now() + ttl
        with self._lock:
            self._prune_offers()
            self._offers[_hash_secret(raw)] = expires_at
        return PairingOffer(
            code=code,
            pairing_url=f"{base_url}#pair={code}",
            expires_at=expires_at,
        )

    def pair(self, code: str, display_name: str) -> DeviceCredential:
        normalized_code = _normalize_pairing_code(code)
        name = _validate_device_name(display_name)
        code_hash = _hash_secret(normalized_code)
        now = self._now()
        with self._lock:
            self._prune_offers()
            expires_at = self._offers.pop(code_hash, None)
            if expires_at is None or expires_at < now:
                raise InvalidPairingCodeError("配对码无效或已过期")
            if sum(device.revoked_at is None for device in self._devices.values()) >= self._max_devices:
                raise PairingCapacityError("已达到配对设备上限")

            device_id = secrets.token_urlsafe(18)
            secret = secrets.token_urlsafe(32)
            stored = _StoredDevice(
                device_id=device_id,
                display_name=name,
                secret_hash=_hash_secret(secret),
                created_at=now,
                last_used_at=now,
            )
            self._devices[device_id] = stored
            self._persist()
            return DeviceCredential(device=stored.public(), secret=secret)

    def authenticate(self, device_id: str, secret: str) -> PairedDevice | None:
        if not _DEVICE_ID_RE.fullmatch(device_id) or len(secret) < 32:
            return None
        with self._lock:
            stored = self._devices.get(device_id)
            if (
                stored is None
                or stored.revoked_at is not None
                or not secrets.compare_digest(stored.secret_hash, _hash_secret(secret))
            ):
                return None
            stored.last_used_at = self._now()
            return stored.public()

    def list_devices(self) -> list[PairedDevice]:
        with self._lock:
            return [
                device.public()
                for device in sorted(
                    self._devices.values(), key=lambda item: item.created_at, reverse=True
                )
            ]

    def revoke(self, device_id: str) -> bool:
        if not _DEVICE_ID_RE.fullmatch(device_id):
            return False
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or device.revoked_at is not None:
                return False
            device.revoked_at = self._now()
            self._persist()
            return True

    def _prune_offers(self) -> None:
        now = self._now()
        self._offers = {
            code_hash: expires_at
            for code_hash, expires_at in self._offers.items()
            if expires_at >= now
        }

    def _load(self) -> dict[str, _StoredDevice]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("devices"), list):
                raise ValueError("unsupported pairing store")
            devices: dict[str, _StoredDevice] = {}
            for item in payload["devices"]:
                try:
                    stored = _StoredDevice(**item)
                    if not _DEVICE_ID_RE.fullmatch(stored.device_id):
                        raise ValueError("invalid device id")
                    devices[stored.device_id] = stored
                except (TypeError, ValueError):
                    logger.warning("跳过损坏的远程配对设备记录")
            return devices
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("读取远程配对设备失败，按空库处理: error_type=%s", type(exc).__name__)
            return {}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "devices": [asdict(device) for device in self._devices.values()],
        }
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_pairing_code(code: str) -> str:
    if not isinstance(code, str):
        raise InvalidPairingCodeError("配对码无效或已过期")
    normalized = code.replace("-", "").replace(" ", "").upper()
    if not _PAIRING_CODE_RE.fullmatch(normalized):
        raise InvalidPairingCodeError("配对码无效或已过期")
    return normalized


def _validate_device_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("设备名称格式不合法")
    normalized = name.strip()
    if not _DEVICE_NAME_RE.fullmatch(normalized):
        raise ValueError("设备名称需为 1-64 个可见字符")
    return normalized


def _validated_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("远程拨号地址必须是无内嵌凭证的 HTTPS URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


__all__ = [
    "DeviceCredential",
    "InvalidPairingCodeError",
    "PairedDevice",
    "PairingCapacityError",
    "PairingOffer",
    "RemotePairingStore",
]
