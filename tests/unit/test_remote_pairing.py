"""Paired-device credentials for the fixed Remote Web Dialer entry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcall.remote_pairing import (
    InvalidPairingCodeError,
    PairingCapacityError,
    RemotePairingStore,
)


def _store(tmp_path: Path, clock: list[float], *, max_devices: int = 5) -> RemotePairingStore:
    return RemotePairingStore(
        tmp_path / "remote_devices.json",
        now=lambda: clock[0],
        max_devices=max_devices,
    )


def test_pairing_code_is_one_time_and_device_secret_is_only_returned_once(tmp_path: Path) -> None:
    clock = [1_000.0]
    store = _store(tmp_path, clock)

    offer = store.create_pairing("https://dial.example/")
    credential = store.pair(offer.code, "Tianye's iPhone")

    assert offer.pairing_url == f"https://dial.example/#pair={offer.code}"
    assert credential.device.display_name == "Tianye's iPhone"
    assert store.authenticate(credential.device.device_id, credential.secret) is not None
    with pytest.raises(InvalidPairingCodeError):
        store.pair(offer.code, "second phone")

    persisted = json.loads((tmp_path / "remote_devices.json").read_text(encoding="utf-8"))
    serialized = json.dumps(persisted)
    assert offer.code not in serialized
    assert credential.secret not in serialized
    assert "secret_hash" in serialized


def test_expired_pairing_code_and_wrong_device_secret_are_rejected(tmp_path: Path) -> None:
    clock = [1_000.0]
    store = _store(tmp_path, clock)
    offer = store.create_pairing("https://dial.example/", ttl_seconds=30)

    clock[0] = 1_031.0
    with pytest.raises(InvalidPairingCodeError):
        store.pair(offer.code, "phone")
    assert store.authenticate("missing", "wrong") is None


def test_revoked_device_is_denied_immediately_and_survives_reload(tmp_path: Path) -> None:
    clock = [1_000.0]
    path = tmp_path / "remote_devices.json"
    store = RemotePairingStore(path, now=lambda: clock[0])
    credential = store.pair(store.create_pairing("https://dial.example/").code, "phone")

    clock[0] = 2_000.0
    assert store.revoke(credential.device.device_id) is True
    assert store.authenticate(credential.device.device_id, credential.secret) is None

    reloaded = RemotePairingStore(path, now=lambda: clock[0])
    devices = reloaded.list_devices()
    assert len(devices) == 1
    assert devices[0].revoked_at == 2_000.0
    assert reloaded.authenticate(credential.device.device_id, credential.secret) is None


def test_pairing_enforces_active_device_cap_but_revoked_slot_can_be_reused(tmp_path: Path) -> None:
    clock = [1_000.0]
    store = _store(tmp_path, clock, max_devices=1)
    first = store.pair(store.create_pairing("https://dial.example/").code, "first")

    second_offer = store.create_pairing("https://dial.example/")
    with pytest.raises(PairingCapacityError):
        store.pair(second_offer.code, "second")

    assert store.revoke(first.device.device_id) is True
    second = store.pair(store.create_pairing("https://dial.example/").code, "second")
    assert second.device.display_name == "second"


def test_missing_or_corrupt_store_loads_as_empty_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "remote_devices.json"
    assert RemotePairingStore(path).list_devices() == []

    path.write_text("not-json", encoding="utf-8")
    assert RemotePairingStore(path).list_devices() == []


@pytest.mark.parametrize("name", ["", "x" * 65, "bad\nname"])
def test_invalid_device_names_are_rejected(tmp_path: Path, name: str) -> None:
    store = RemotePairingStore(tmp_path / "remote_devices.json")
    offer = store.create_pairing("https://dial.example/")
    with pytest.raises(ValueError):
        store.pair(offer.code, name)
