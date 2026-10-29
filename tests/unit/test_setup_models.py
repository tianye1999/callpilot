"""实体卡向导默认配置、余额校验和包内资产路径。"""

import urllib.error

import pytest

from agentcall import config, local_models


def test_physical_sim_defaults():
    assert config.get_spec("AGENT_PROVIDER").default == "local"
    assert config.get_spec("LOCAL_LLM_PROVIDER").default == "deepseek"
    assert config.get_spec("LOCAL_LLM_MODEL").default == "deepseek-flash"


def test_explicit_provider_and_model_are_preserved(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "qwen")
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)
    assert config.get_str("AGENT_PROVIDER") == "qwen"
    assert config.get_str("LOCAL_LLM_MODEL") == "qwen-plus"
    monkeypatch.setenv("LOCAL_LLM_MODEL", "custom-model")
    assert config.get_str("LOCAL_LLM_MODEL") == "custom-model"


@pytest.mark.parametrize("body,status", [
    (b'{"is_available":true}', "valid"),
    (b'{"is_available":false}', "balance"),
    (b'{}', "network"), (b'not json', "network"),
])
def test_deepseek_balance_validation(monkeypatch, body, status):
    def http(url, **kwargs):
        assert url == "https://api.deepseek.com/user/balance"
        assert kwargs.get("method", "GET") == "GET"
        assert kwargs["headers"] == {"Authorization": "Bearer sk-test"}
        return 200, body
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://api.deepseek.com/")
    monkeypatch.setattr(config, "_http_request_json", http)
    result = config.validate_provider_key_online("deepseek", "sk-test")
    assert result.status == status
    assert result.ok == (status == "valid")


@pytest.mark.parametrize("error,status", [
    (urllib.error.HTTPError("url", 401, "sk-test", {}, None), "invalid"),
    (urllib.error.URLError("sk-test"), "network"),
])
def test_deepseek_errors_never_echo_secret(monkeypatch, error, status):
    def http(*args, **kwargs):
        raise error
    monkeypatch.setattr(config, "_http_request_json", http)
    result = config.validate_provider_key_online("deepseek", "sk-test")
    assert result.status == status
    assert "sk-test" not in result.message


def test_deepseek_format_rejected_without_http(monkeypatch):
    monkeypatch.setattr(config, "_http_request_json", lambda *a, **k: pytest.fail("不应联网"))
    assert config.validate_provider_key_online("deepseek", "wrong").status == "format"


def populate(root, assets):
    for asset in assets:
        for rel in asset.required_files:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"model")


def test_models_dir_resolution_and_readonly_bundle(monkeypatch, tmp_path):
    data, bundle = tmp_path / "data", tmp_path / "bundle"
    monkeypatch.delenv("LOCAL_MODELS_DIR", raising=False)
    monkeypatch.setenv("LOCAL_TTS_MODEL", "vits-icefall-zh-aishell3")
    monkeypatch.setattr(config, "data_dir", lambda: data)
    monkeypatch.setattr(local_models, "bundled_models_dir", lambda: bundle)
    assert local_models.models_dir() == data / "models"
    populate(bundle, local_models.bundled_assets())
    assert local_models.models_dir() == bundle
    populate(data / "models", local_models.selected_assets())
    assert local_models.models_dir() == data / "models"
    monkeypatch.setenv("LOCAL_MODELS_DIR", str(tmp_path / "override"))
    assert local_models.models_dir() == tmp_path / "override"
    missing = local_models.ModelAsset("missing", "missing", "url", ("missing.onnx",), False)
    with pytest.raises(RuntimeError, match="只读"):
        local_models.ensure_asset(missing, bundle)


def test_bundle_manifest_only_required_files(tmp_path):
    populate(tmp_path, local_models.bundled_assets())
    (tmp_path / "unwanted.onnx").write_bytes(b"large")
    files = local_models.bundle_data_files(tmp_path)
    assert len(files) == 13
    assert not any("unwanted" in source for source, _ in files)
    assert all(target.startswith("models") for _, target in files)


def test_bundle_path_requires_frozen_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(local_models.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(local_models.sys, "frozen", False, raising=False)
    assert local_models.bundled_models_dir() is None
    monkeypatch.setattr(local_models.sys, "frozen", True)
    assert local_models.bundled_models_dir() == tmp_path / "models"
    with pytest.raises(RuntimeError, match="打包模型缺失"):
        local_models.bundle_data_files(tmp_path)
