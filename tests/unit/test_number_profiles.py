"""预调教号码任务库：JSON 加载、分层匹配与安全回退。"""

from __future__ import annotations

import json

import pytest

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


def test_list_profiles_returns_labels_with_fallbacks_in_file_order(tmp_path, monkeypatch):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {"label": "公开客服 · 查流量", "number": "10000", "task": "查流量", "scenario": "流量策略"},
            {"number": "10010", "task": "查话费", "scenario": "话费策略"},
            {"number": "10086", "scenario": "通用策略"},
        ],
    )
    monkeypatch.setenv("AGENT_LANGUAGE", "zh")

    assert number_profiles.list_profiles(path=path) == [
        {"number": "10000", "task": "查流量", "label": "公开客服 · 查流量"},
        {"number": "10010", "task": "查话费", "label": "10010 · 查话费"},
        {"number": "10086", "task": "", "label": "10086 · 通用"},
    ]


def test_list_profiles_handles_missing_and_bad_files(tmp_path):
    assert number_profiles.list_profiles(path=tmp_path / "missing.json") == []

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not-json", encoding="utf-8")
    assert number_profiles.list_profiles(path=bad_json) == []


def test_list_profiles_uses_english_general_fallback(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(path, [{"number": "10086", "scenario": "通用策略"}])

    assert number_profiles.list_profiles(path=path, lang="en") == [
        {"number": "10086", "task": "", "label": "10086 · general"}
    ]


def test_bilingual_scenario_and_opening_by_call_lang(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {
                "number": "10000",
                "task": {"zh": "查流量", "en": "check data"},
                "scenario": {"zh": "中文场景", "en": "English scenario"},
                "opening": {"zh": "你好查流量", "en": "hi check data"},
            }
        ],
    )

    zh = number_profiles.lookup_profile("10000", "查流量", lang="zh", path=path)
    en = number_profiles.lookup_profile("10000", "check data", lang="en", path=path)

    assert zh is not None and zh["scenario"] == "中文场景" and zh["opening"] == "你好查流量"
    assert en is not None and en["scenario"] == "English scenario" and en["opening"] == "hi check data"


def test_bilingual_task_matches_either_language(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [{"number": "10000", "task": {"zh": "查流量", "en": "check data"}, "scenario": "策略"}],
    )

    assert number_profiles.lookup_profile("10000", "查流量", path=path) is not None
    assert number_profiles.lookup_profile("10000", "check data", path=path) is not None


def test_scenario_falls_back_to_other_language(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(path, [{"number": "10000", "task": "查流量", "scenario": {"zh": "只有中文"}}])

    result = number_profiles.lookup_profile("10000", "查流量", lang="en", path=path)

    assert result is not None
    assert result["scenario"] == "只有中文"


def test_list_profiles_label_and_task_by_ui_lang(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {
                "number": "10000",
                "task": {"zh": "查流量", "en": "check data"},
                "label": {"zh": "电信·查流量", "en": "Telecom · Data"},
                "scenario": "策略",
            }
        ],
    )

    zh = number_profiles.list_profiles(path=path, lang="zh")
    en = number_profiles.list_profiles(path=path, lang="en")

    assert zh == [{"number": "10000", "task": "查流量", "label": "电信·查流量"}]
    assert en == [{"number": "10000", "task": "check data", "label": "Telecom · Data"}]


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"number": "10000", "task": {"zh": 123}, "scenario": "策略"},
        {"number": "10000", "task": ["查流量"], "scenario": "策略"},
        {"number": "10000", "task": "查流量", "scenario": {"zh": {"nested": "x"}}},
        {"number": "10000", "scenario": 42},
        {"number": "10000", "task": "查流量", "scenario": "策略", "opening": 99},
    ],
)
def test_malformed_fields_never_raise(tmp_path, bad_entry):
    path = tmp_path / "number_profiles.json"
    write_profiles(path, [bad_entry])

    result = number_profiles.lookup_profile("10000", "查流量", path=path)
    assert result is None or isinstance(result, dict)
    assert isinstance(number_profiles.list_profiles(path=path), list)


def test_empty_target_task_does_not_match_task_bearing_entry(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(path, [{"number": "10000", "task": "查流量", "scenario": "策略"}])

    assert number_profiles.lookup_profile("10000", "", path=path) is None
    assert number_profiles.lookup_profile("10000", None, path=path) is None
