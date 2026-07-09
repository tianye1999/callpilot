"""预调教号码任务库：JSON 加载、分层匹配与安全回退。"""

from __future__ import annotations

import json

from agentcall import number_profiles


def write_profiles(path, profiles) -> None:
    path.write_text(json.dumps({"profiles": profiles}, ensure_ascii=False), encoding="utf-8")


def test_lookup_exact_number_and_task_first(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {"number": "10000", "scenario": "通用兜底", "opening": "办理业务"},
            {"number": "10000", "task": "查流量", "scenario": "精确查流量", "opening": "查流量"},
        ],
    )

    result = number_profiles.lookup_profile(" 10000 ", " 查流量 ", path=path)

    assert result is not None
    assert result["scenario"] == "精确查流量"
    assert result["opening"] == "查流量"
    assert result["source"] == "profile"
    assert result["number"] == "10000"
    assert result["task"] == "查流量"


def test_lookup_same_number_different_tasks(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {"number": "10000", "task": "查流量", "scenario": "流量策略"},
            {"number": "10000", "task": "查话费", "scenario": "话费策略"},
        ],
    )

    traffic = number_profiles.lookup_profile("10000", "查流量", path=path)
    balance = number_profiles.lookup_profile("10000", "查话费", path=path)

    assert traffic is not None
    assert balance is not None
    assert traffic["scenario"] == "流量策略"
    assert balance["scenario"] == "话费策略"


def test_lookup_number_wildcard_when_task_has_no_exact_match(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {"number": "10000", "task": "查流量", "scenario": "流量策略"},
            {"number": "10000", "scenario": "号码通用策略", "opening": "办理业务"},
        ],
    )

    result = number_profiles.lookup_profile("10000", "改套餐", path=path)

    assert result is not None
    assert result["scenario"] == "号码通用策略"
    assert result["opening"] == "办理业务"


def test_lookup_miss_and_bad_files_return_none(tmp_path):
    missing = tmp_path / "missing.json"
    assert number_profiles.lookup_profile("10000", "查流量", path=missing) is None

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not-json", encoding="utf-8")
    assert number_profiles.lookup_profile("10000", "查流量", path=bad_json) is None

    no_scenario = tmp_path / "no_scenario.json"
    write_profiles(no_scenario, [{"number": "10000", "task": "查流量", "opening": "查流量"}])
    assert number_profiles.lookup_profile("10000", "查流量", path=no_scenario) is None


def test_lookup_uses_configured_default_file(tmp_path, monkeypatch):
    path = tmp_path / "profiles.json"
    write_profiles(path, [{"number": "10000", "scenario": "配置路径策略"}])
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(path))

    result = number_profiles.lookup_profile("10000", "任意事项")

    assert result is not None
    assert result["scenario"] == "配置路径策略"
