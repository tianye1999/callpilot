"""Platform-specific cloud credential storage regression coverage."""

import base64
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agentcall import cloud_credentials, platforms
from agentcall.cloud_credentials import CloudCredentialStore, EdgeCredential

RAW = "edge_abcdefghijkl." + "s" * 40


@pytest.fixture
def linux_store(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "IS_LINUX", True)
    monkeypatch.setenv("AGENTCALL_DATA_DIR", str(tmp_path / "nested" / "data"))
    # Linux must not need even an importable keyring module.
    monkeypatch.setitem(sys.modules, "keyring", None)
    return CloudCredentialStore(), tmp_path / "nested" / "data" / "cloud-credentials.json"


def test_linux_save_load_clear_preserves_private_key(linux_store):
    store, path = linux_store
    assert store.load() is None
    store.clear()
    public_key = store.load_or_create_public_key()
    assert store.save(RAW) == EdgeCredential("edge_abcdefghijkl", RAW)
    assert CloudCredentialStore().load() == EdgeCredential("edge_abcdefghijkl", RAW)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) == {"edge-credential", "edge-device-key"}
    assert saved["edge-credential"] == RAW
    assert CloudCredentialStore().load_or_create_public_key() == public_key
    Ed25519PublicKey.from_public_bytes(cloud_credentials._decode_key(public_key)).verify(
        base64.urlsafe_b64decode(store.sign(b"proof") + "=="), b"proof"
    )
    store.clear()
    store.clear()
    assert store.load() is None
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "edge-device-key": saved["edge-device-key"]
    }
    assert CloudCredentialStore().load_or_create_public_key() == public_key


def test_linux_atomic_write_uses_private_temp_file(linux_store, monkeypatch):
    store, path = linux_store
    replace = os.replace
    dump = json.dump
    replacements = []

    def checked_dump(contents, stream):
        assert stream.tell() == 0
        if os.name == "posix":
            assert stat.S_IMODE(os.fstat(stream.fileno()).st_mode) == 0o600
        return dump(contents, stream)

    def checked_replace(src, dst):
        source = Path(src)
        assert source.parent == path.parent
        assert source != path
        assert json.loads(source.read_text(encoding="utf-8"))["edge-credential"] == RAW
        if os.name == "posix":
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
        replacements.append(source)
        replace(src, dst)

    monkeypatch.setattr(cloud_credentials.os, "replace", checked_replace)
    monkeypatch.setattr(cloud_credentials.json, "dump", checked_dump)
    store.save(RAW)
    store.save(RAW)
    assert len(replacements) == 2
    assert list(path.parent.iterdir()) == [path]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_linux_failed_replace_keeps_original_and_removes_temp(linux_store, monkeypatch):
    store, path = linux_store
    store.save(RAW)
    original = path.read_bytes()

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(cloud_credentials.os, "replace", fail_replace)
    with pytest.raises(RuntimeError):
        store.save("edge_abcdefghijkl." + "t" * 40)
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("contents", ["not json", "[]", '{"edge-credential": 1}'])
def test_linux_invalid_file_logs_no_contents(linux_store, caplog, contents):
    store, path = linux_store
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    assert store.load() is None
    assert "error_type=" in caplog.text
    assert contents not in caplog.text


def test_non_linux_uses_keyring_module(monkeypatch, tmp_path):
    import keyring.errors

    monkeypatch.setattr(platforms, "IS_LINUX", False)
    monkeypatch.setenv("AGENTCALL_DATA_DIR", str(tmp_path))
    values = {}
    fake = ModuleType("keyring")
    fake.errors = keyring.errors
    fake.get_password = lambda service, account: values.get((service, account))
    fake.set_password = lambda service, account, value: values.__setitem__((service, account), value)
    fake.delete_password = lambda service, account: values.pop((service, account))
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = CloudCredentialStore()
    public_key = store.load_or_create_public_key()
    store.save(RAW)
    assert store.load() == EdgeCredential("edge_abcdefghijkl", RAW)
    store.clear()
    assert store.load() is None
    assert store.load_or_create_public_key() == public_key
    assert set(values) == {("ai.bondings.callpilot.cloud", "edge-device-key")}
    assert not list(tmp_path.iterdir())
