"""预调教号码任务库：JSON 加载、分层匹配与安全回退。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from agentcall import number_profiles


def write_profiles(path, profiles) -> None:
    path.write_text(json.dumps({"profiles": profiles}, ensure_ascii=False), encoding="utf-8")


def test_bundled_seed_has_public_hotline_profiles_without_private_data():
    seed = Path(__file__).resolve().parents[2] / "data" / "number_profiles.example.json"
    raw = seed.read_text(encoding="utf-8")
    data = json.loads(raw)
    profiles = data["profiles"]

    assert data.get("_comment")
    assert [item["number"] for item in profiles] == [
        "10000",
        "10000",
        "10086",
        "10010",
        "95588",
        "95533",
        "95555",
        "95566",
        "12315",
        "12345",
    ]
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", raw)
    allowed_numbers = {
        "10000",
        "10086",
        "10010",
        "95588",
        "95533",
        "95555",
        "95566",
        "12315",
        "12345",
    }
    assert all(item["number"] in allowed_numbers for item in profiles)
    assert len({item["id"] for item in profiles}) == len(profiles)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["id"]) for item in profiles)

    for item in profiles:
        for field in ("label", "task", "scenario", "opening"):
            value = item[field]
            assert set(value) == {"zh", "en"}
            assert value["zh"].strip()
            assert value["en"].strip()


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


def test_ensure_seeded_copies_seed_when_target_missing(tmp_path):
    seed = tmp_path / "seed" / "number_profiles.example.json"
    seed.parent.mkdir()
    seed.write_text('{"profiles":[{"number":"10000","scenario":"seed"}]}', encoding="utf-8")
    target = tmp_path / "data" / "number_profiles.json"

    assert number_profiles.ensure_seeded(target=target, seed=seed)
    assert target.read_text(encoding="utf-8") == seed.read_text(encoding="utf-8")


def test_ensure_seeded_keeps_existing_target(tmp_path):
    seed = tmp_path / "number_profiles.example.json"
    seed.write_text('{"profiles":[{"number":"10000","scenario":"seed"}]}', encoding="utf-8")
    target = tmp_path / "number_profiles.json"
    target.write_text('{"profiles":[{"number":"10086","scenario":"existing"}]}', encoding="utf-8")

    assert not number_profiles.ensure_seeded(target=target, seed=seed)
    assert "10086" in target.read_text(encoding="utf-8")


def test_ensure_seeded_warns_and_does_not_raise_when_seed_missing(tmp_path, caplog):
    target = tmp_path / "number_profiles.json"

    with caplog.at_level(logging.WARNING):
        assert not number_profiles.ensure_seeded(target=target, seed=tmp_path / "missing.json")

    assert not target.exists()
    assert "号码任务库种子文件不存在" in caplog.text


def test_management_crud_persists_ids_and_disabled_profiles_do_not_match(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {
                "number": "10000",
                "task": {"zh": "查流量", "en": "check data"},
                "label": {"zh": "电信·查流量", "en": "Telecom · Data"},
                "scenario": {"zh": "中文策略", "en": "English strategy"},
                "opening": {"zh": "查流量", "en": "Check data"},
            }
        ],
    )

    legacy = number_profiles.list_managed_profiles(path=path)
    assert len(legacy) == 1
    legacy_id = legacy[0]["id"]
    assert legacy_id.startswith("legacy_")

    updated = number_profiles.update_profile(
        legacy_id,
        {
            **legacy[0],
            "enabled": False,
            "label": {"zh": "电信·流量", "en": "Telecom · Data usage"},
        },
        path=path,
    )

    stored = json.loads(path.read_text(encoding="utf-8"))["profiles"]
    assert stored[0]["id"] == legacy_id
    assert updated["enabled"] is False
    assert number_profiles.lookup_profile("10000", "查流量", path=path) is None
    assert number_profiles.lookup_profile_by_id(legacy_id, "10000", "本月用量", path=path) is None
    assert number_profiles.list_profiles(path=path) == []

    created = number_profiles.create_profile(
        {
            "enabled": True,
            "number": "10086",
            "match_mode": "number",
            "label": {"zh": "移动·通用", "en": "China Mobile · General"},
            "task": {"zh": "", "en": ""},
            "scenario": {"zh": "号码通用策略", "en": "Number fallback strategy"},
            "opening": {"zh": "办理业务", "en": "I'd like some help"},
        },
        path=path,
    )
    assert created["id"]
    assert created["match_mode"] == "number"

    by_id = number_profiles.lookup_profile_by_id(
        created["id"], "10086", "任意本通子事项", path=path
    )
    assert by_id is not None
    assert by_id["scenario"] == "号码通用策略"
    assert by_id["profile_id"] == created["id"]
    assert number_profiles.lookup_profile_by_id(created["id"], "10010", "", path=path) is None

    assert number_profiles.delete_profile(created["id"], path=path)
    assert not number_profiles.delete_profile(created["id"], path=path)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"number": "not a number", "scenario": "策略"}, "号码格式"),
        ({"number": "10000", "scenario": ""}, "scenario"),
        ({"number": "10000", "scenario": "策" * 1201}, "1200"),
        ({"number": "10000", "scenario": "策略", "opening": "开" * 41}, "40"),
        (
            {"number": "10000", "match_mode": "exact", "task": "", "scenario": "策略"},
            "task",
        ),
    ],
)
def test_create_profile_rejects_invalid_input_without_changing_file(
    tmp_path, payload, message
):
    path = tmp_path / "number_profiles.json"
    original = '{"_comment":"keep","profiles":[]}\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(number_profiles.ProfileValidationError, match=message):
        number_profiles.create_profile(payload, path=path)

    assert path.read_text(encoding="utf-8") == original


def test_create_profile_rejects_ambiguous_exact_and_number_fallbacks(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {"number": "10000", "task": {"zh": "查流量", "en": "check data"}, "scenario": "精确"},
            {"number": "10000", "scenario": "通配"},
        ],
    )

    with pytest.raises(number_profiles.ProfileConflictError):
        number_profiles.create_profile(
            {"number": "10000", "task": "check data", "scenario": "重复精确"}, path=path
        )
    with pytest.raises(number_profiles.ProfileConflictError):
        number_profiles.create_profile(
            {"number": "10000", "match_mode": "number", "scenario": "重复通配"},
            path=path,
        )


def test_list_profiles_exposes_stable_id_without_rewriting_legacy_file(tmp_path):
    path = tmp_path / "number_profiles.json"
    write_profiles(path, [{"number": "10000", "task": "查流量", "scenario": "策略"}])
    before = path.read_text(encoding="utf-8")

    choices = number_profiles.list_profiles(path=path, include_id=True)

    assert choices[0]["id"].startswith("legacy_")
    assert path.read_text(encoding="utf-8") == before


def test_profile_scenario_limit_preserves_full_bundled_english_strategy():
    seed = Path(__file__).resolve().parents[2] / "data" / "number_profiles.example.json"
    result = number_profiles.lookup_profile(
        "10000", "check data usage", lang="en", path=seed
    )

    assert result is not None
    assert len(result["scenario"]) > 200
    assert "never claim you have the figures" in result["scenario"]


def test_lookup_profile_opening_mode_wait_and_fallbacks(tmp_path):
    """#80-B:opening_mode 归一——wait 透传;缺省/非法/大小写混排回落 say。"""
    path = tmp_path / "number_profiles.json"
    write_profiles(
        path,
        [
            {"number": "10086", "scenario": "IVR 热线", "opening_mode": " Wait "},
            {"number": "10000", "scenario": "默认开场"},
            {"number": "10010", "scenario": "非法值", "opening_mode": "shout"},
        ],
    )

    assert number_profiles.lookup_profile("10086", "x", path=path)["opening_mode"] == "wait"
    assert number_profiles.lookup_profile("10000", "x", path=path)["opening_mode"] == "say"
    assert number_profiles.lookup_profile("10010", "x", path=path)["opening_mode"] == "say"
