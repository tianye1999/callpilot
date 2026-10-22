"""呼叫情报库（WIL-129）：种子校验 / 匹配 / 缺失检查 / 回流合并与隐私拒绝。"""

import json
import re
from pathlib import Path

import pytest

from agentcall import call_playbooks
from agentcall.call_playbooks import (
    PlaybookValidationError,
    ivr_notes_text,
    learn_from_call,
    list_playbooks,
    lookup_playbook,
    merge_learned,
    missing_info_message,
    missing_required_info,
)

SEED = Path(__file__).resolve().parents[2] / "data" / "call_playbooks.example.json"

# 种子只允许公共客服热线；新增条目必须同步维护这份白名单。
_ALLOWED_SEED_NUMBERS = {"611", "8009019878", "8003310500", "8009346489"}


def write_playbooks(path: Path, playbooks: list) -> None:
    path.write_text(
        json.dumps({"playbooks": playbooks}, ensure_ascii=False), encoding="utf-8"
    )


def sample_playbook() -> dict:
    return {
        "id": "att_prepaid_611",
        "numbers": ["611"],
        "label": {"zh": "AT&T Prepaid 客服", "en": "AT&T Prepaid CS"},
        "required_info": [
            {
                "key": "account_pin",
                "label": {"zh": "四位账户 PIN", "en": "Four-digit account PIN"},
                "purpose": {"zh": "转人工核身", "en": "identity gate"},
            },
            {
                "key": "activation_zip",
                "label": {"zh": "激活 ZIP", "en": "Activation ZIP"},
                "purpose": {"zh": "第二道核身", "en": "second gate"},
            },
        ],
        "ivr_notes": {"zh": "短词应答。", "en": "Answer with short tokens."},
    }


# ---------------------------------------------------------------------------
# 种子文件
# ---------------------------------------------------------------------------


def test_seed_file_is_valid_and_bilingual():
    data = json.loads(SEED.read_text(encoding="utf-8"))
    playbooks = data["playbooks"]
    assert playbooks, "种子不能为空"
    ids = [p["id"] for p in playbooks]
    assert len(ids) == len(set(ids))
    for p in playbooks:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p["id"])
        assert p["numbers"], p["id"]
        for number in p["numbers"]:
            assert number in _ALLOWED_SEED_NUMBERS, f"非白名单号码: {number}"
        for field in ("label", "ivr_notes", "source"):
            assert p[field].get("zh") and p[field].get("en"), f"{p['id']}.{field}"
        assert p["required_info"], p["id"]
        for entry in p["required_info"]:
            assert re.fullmatch(r"[a-z0-9_]{1,40}", entry["key"])
            assert entry["label"].get("zh") and entry["label"].get("en")
            assert entry["purpose"].get("zh") and entry["purpose"].get("en")
        # use_cases：常见办理事项知识（建单/intake 消费），双语齐备
        assert p["use_cases"], p["id"]
        for case in p["use_cases"]:
            assert case["label"].get("zh") and case["label"].get("en")
            assert case["notes"].get("zh") and case["notes"].get("en")


def test_seed_file_contains_no_value_like_digits():
    """键值分离红线：种子除 numbers 外任何字段都不得含 ≥4 位连续数字。"""
    data = json.loads(SEED.read_text(encoding="utf-8"))

    def walk(node, in_numbers=False):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, in_numbers=(key == "numbers"))
        elif isinstance(node, list):
            for item in node:
                walk(item, in_numbers=in_numbers)
        elif isinstance(node, str) and not in_numbers:
            # ISO 日期（updated/source 溯源）不是账户值，放行
            cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", "", node)
            assert not re.search(r"\d{4}", cleaned), f"疑似真实值泄漏: {node[:60]}"

    walk(data["playbooks"])


def test_ensure_seeded_copies_once(tmp_path):
    target = tmp_path / "pb.json"
    assert call_playbooks.ensure_seeded(target=target, seed=SEED) is True
    assert json.loads(target.read_text(encoding="utf-8"))["playbooks"]
    # 已存在时不覆盖
    target.write_text('{"playbooks": []}', encoding="utf-8")
    assert call_playbooks.ensure_seeded(target=target, seed=SEED) is False
    assert json.loads(target.read_text(encoding="utf-8"))["playbooks"] == []


def test_ensure_seeded_missing_seed_warns_not_raises(tmp_path, caplog):
    target = tmp_path / "pb.json"
    missing = tmp_path / "no-such-seed.json"
    assert call_playbooks.ensure_seeded(target=target, seed=missing) is False
    assert not target.exists()


# ---------------------------------------------------------------------------
# 读路径与匹配
# ---------------------------------------------------------------------------


def test_lookup_matches_number_and_misses_gracefully(tmp_path):
    path = tmp_path / "pb.json"
    write_playbooks(path, [sample_playbook()])
    hit = lookup_playbook("611", path=path)
    assert hit is not None and hit["id"] == "att_prepaid_611"
    assert lookup_playbook("10086", path=path) is None
    assert lookup_playbook("", path=path) is None


def test_lenient_read_never_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    assert list_playbooks(path=bad) == []
    assert lookup_playbook("611", path=bad) is None
    assert list_playbooks(path=tmp_path / "missing.json") == []


def test_missing_required_info_states():
    pb = sample_playbook()
    # 无 playbook → 无缺失
    assert missing_required_info(None, {"verification": {}}) == []
    # 无 verification → 全缺
    missing = missing_required_info(pb, None)
    assert [m["key"] for m in missing] == ["account_pin", "activation_zip"]
    # 部分缺
    missing = missing_required_info(pb, {"verification": {"account_pin": "1234"}})
    assert [m["key"] for m in missing] == ["activation_zip"]
    # 空值视作缺失
    missing = missing_required_info(
        pb, {"verification": {"account_pin": " ", "activation_zip": "55401"}}
    )
    assert [m["key"] for m in missing] == ["account_pin"]
    # 全齐
    assert (
        missing_required_info(
            pb, {"verification": {"account_pin": "1234", "activation_zip": "55401"}}
        )
        == []
    )


def test_missing_info_message_lists_label_and_key():
    pb = sample_playbook()
    missing = missing_required_info(pb, None)
    message = missing_info_message(missing, "zh")
    assert "四位账户 PIN" in message
    assert "account_pin" in message
    assert "verification" in message
    message_en = missing_info_message(missing, "en")
    assert "Four-digit account PIN" in message_en


def test_ivr_notes_text_language_pick():
    pb = sample_playbook()
    assert ivr_notes_text(pb, "zh") == "短词应答。"
    assert ivr_notes_text(pb, "en") == "Answer with short tokens."
    assert ivr_notes_text(None, "zh") == ""


# ---------------------------------------------------------------------------
# 回流合并
# ---------------------------------------------------------------------------


def learned_payload() -> dict:
    return {
        "new_required_info": [
            {
                "key": "activation_zip",
                "label": {"zh": "激活 ZIP", "en": "Activation ZIP"},
                "purpose": {"zh": "第二道核身", "en": "second gate"},
            }
        ],
        "ivr_notes_update": None,
    }


def test_merge_learned_adds_new_key_with_source(tmp_path):
    path = tmp_path / "pb.json"
    pb = sample_playbook()
    pb["required_info"] = pb["required_info"][:1]  # 只有 account_pin
    write_playbooks(path, [pb])
    merged = merge_learned(
        "611", learned_payload(), call_id="call-1", updated="2026-08-18", path=path
    )
    assert merged is not None
    keys = [e["key"] for e in merged["required_info"]]
    assert keys == ["account_pin", "activation_zip"]
    added = merged["required_info"][1]
    assert added["source"] == {"call_id": "call-1", "date": "2026-08-18"}
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["playbooks"][0]["updated"] == "2026-08-18"


def test_merge_learned_never_overwrites_existing_key(tmp_path):
    path = tmp_path / "pb.json"
    write_playbooks(path, [sample_playbook()])
    original = json.loads(path.read_text(encoding="utf-8"))
    result = merge_learned(
        "611", learned_payload(), call_id="call-2", updated="2026-08-19", path=path
    )
    assert result is None  # 没有新知识
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_merge_learned_auto_creates_entry_for_unknown_hotline(tmp_path):
    path = tmp_path / "pb.json"
    write_playbooks(path, [])
    merged = merge_learned(
        "8009346489",
        learned_payload(),
        call_id="call-3",
        updated="2026-08-18",
        path=path,
    )
    assert merged is not None
    assert merged["numbers"] == ["8009346489"]
    assert merged["id"].startswith("learned_")
    assert lookup_playbook("8009346489", path=path) is not None


def test_merge_learned_appends_ivr_notes_once(tmp_path):
    path = tmp_path / "pb.json"
    write_playbooks(path, [sample_playbook()])
    learned = {
        "new_required_info": [],
        "ivr_notes_update": {"zh": "核身先 PIN 后 ZIP。", "en": "PIN before ZIP."},
    }
    merged = merge_learned(
        "611", learned, call_id="call-4", updated="2026-08-18", path=path
    )
    assert merged is not None
    assert "实测补充(2026-08-18): 核身先 PIN 后 ZIP。" in merged["ivr_notes"]["zh"]
    # 同样内容再来一次 → 无新知识
    assert (
        merge_learned("611", learned, call_id="call-5", updated="2026-08-19", path=path)
        is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"new_required_info": [{"key": "PIN CODE!", "label": {"zh": "x"}, "purpose": {"zh": "y"}}]},
        {"new_required_info": [{"key": "ok_key", "label": {}, "purpose": {"zh": "y"}}]},
        {"new_required_info": ["not-a-dict"]},
        {"new_required_info": [], "ivr_notes_update": "not-a-dict"},
    ],
)
def test_merge_learned_rejects_malformed(tmp_path, bad):
    path = tmp_path / "pb.json"
    write_playbooks(path, [sample_playbook()])
    with pytest.raises(PlaybookValidationError):
        merge_learned("611", bad, call_id="c", updated="2026-08-18", path=path)


@pytest.mark.parametrize(
    "bad",
    [
        {
            "new_required_info": [
                {
                    "key": "account_code",
                    "label": {"zh": "口令 5566 相关", "en": "code"},
                    "purpose": {"zh": "核身", "en": "gate"},
                }
            ]
        },
        {
            "new_required_info": [],
            "ivr_notes_update": {"zh": "输入 123456 即可", "en": "type it"},
        },
    ],
)
def test_merge_learned_privacy_rejects_digit_runs(tmp_path, bad):
    """隐私红线：知识字段带 ≥4 位连续数字（疑似真实值）整条拒绝。"""
    path = tmp_path / "pb.json"
    write_playbooks(path, [sample_playbook()])
    original = path.read_text(encoding="utf-8")
    with pytest.raises(PlaybookValidationError):
        merge_learned("611", bad, call_id="c", updated="2026-08-18", path=path)
    assert path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# learn_from_call（模型交互 mock）
# ---------------------------------------------------------------------------


def _enable(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "pb.json"
    monkeypatch.setenv("CALL_PLAYBOOKS_ENABLED", "true")
    return path


def test_learn_from_call_merges_model_findings(tmp_path, monkeypatch):
    path = _enable(monkeypatch, tmp_path)
    write_playbooks(path, [sample_playbook()])
    seen_prompts = {}

    def fake_call_text_model(messages, **kwargs):
        seen_prompts["user"] = messages[-1]["content"]
        return (
            json.dumps(
                {
                    "new_required_info": [
                        {
                            "key": "billing_zip",
                            "label": {"zh": "账单 ZIP", "en": "Billing ZIP"},
                            "purpose": {"zh": "备用核身", "en": "fallback gate"},
                        }
                    ],
                    "ivr_notes_update": None,
                }
            ),
            None,
        )

    monkeypatch.setattr(
        "agentcall.prompt_gen.call_text_model", fake_call_text_model
    )
    merged = learn_from_call(
        "611",
        [("user", "Please say or enter your billing zip code."), ("agent", "OK.")],
        {"verification": {"account_pin": "1234"}},
        call_id="call-9",
        updated="2026-08-18",
        path=path,
    )
    assert merged is not None
    assert "billing_zip" in [e["key"] for e in merged["required_info"]]
    # 隐私：learner 输入只含键名，绝不含 verification 的值
    assert "account_pin" in seen_prompts["user"]
    assert "1234" not in seen_prompts["user"]


def test_learn_from_call_disabled_or_garbage_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "pb.json"
    write_playbooks(path, [sample_playbook()])
    # 开关关（conftest 默认 false）→ 不触模型
    assert (
        learn_from_call(
            "611",
            [("user", "hello")],
            None,
            call_id="c",
            updated="2026-08-18",
            path=path,
        )
        is None
    )
    # 开关开但模型输出垃圾 → None 且不写盘
    monkeypatch.setenv("CALL_PLAYBOOKS_ENABLED", "true")
    monkeypatch.setattr(
        "agentcall.prompt_gen.call_text_model",
        lambda messages, **kwargs: ("not json at all", None),
    )
    original = path.read_text(encoding="utf-8")
    assert (
        learn_from_call(
            "611",
            [("user", "hello")],
            None,
            call_id="c",
            updated="2026-08-18",
            path=path,
        )
        is None
    )
    assert path.read_text(encoding="utf-8") == original


def test_learn_from_call_privacy_rejection_is_swallowed(tmp_path, monkeypatch):
    """模型违规输出值 → merge 拒绝 → learn_from_call 返回 None 不抛。"""
    path = _enable(monkeypatch, tmp_path)
    write_playbooks(path, [sample_playbook()])
    monkeypatch.setattr(
        "agentcall.prompt_gen.call_text_model",
        lambda messages, **kwargs: (
            json.dumps(
                {
                    "new_required_info": [
                        {
                            "key": "leaked",
                            "label": {"zh": "值 9876", "en": "value 9876"},
                            "purpose": {"zh": "x", "en": "y"},
                        }
                    ],
                    "ivr_notes_update": None,
                }
            ),
            None,
        ),
    )
    assert (
        learn_from_call(
            "611",
            [("user", "hi")],
            None,
            call_id="c",
            updated="2026-08-18",
            path=path,
        )
        is None
    )


def test_learner_system_prompt_is_demand_gated():
    """真机教训（2026-08-18 测试2）：IVR 广告词"user ID and password unchanged"
    被 learner 当成必备信息写库。规则必须是"仅限本通被明确索要"。"""
    s = call_playbooks._LEARNER_SYSTEM
    assert "EXPLICITLY DEMANDED" in s
    assert "announcements, marketing" in s


def evidence_payload(*, eligible=True, reason="Explicit prerequisite on the successful normal route"):
    payload = learned_payload()
    payload["new_required_info"][0].update(
        hard_eligible=eligible, evidence_reason=reason
    )
    return payload


def test_single_learned_demand_is_soft_then_two_independent_calls_promote(tmp_path, caplog):
    path = tmp_path / "pb.json"
    with caplog.at_level("INFO"):
        first = merge_learned(
            "611", evidence_payload(), call_id="call-a", updated="2026-10-21", path=path
        )
        assert missing_required_info(first, None) == []
        assert "activation_zip" in call_playbooks.soft_info_text(first, "en")
        # Reprocessing one call must not create independent evidence or rewrite the file.
        original = path.read_bytes()
        assert merge_learned(
            "611", evidence_payload(), call_id="call-a", updated="2026-10-21", path=path
        ) is None
        assert path.read_bytes() == original
        second = merge_learned(
            "611", evidence_payload(), call_id="call-b", updated="2026-10-21", path=path
        )
    assert [e["key"] for e in missing_required_info(second, None)] == ["activation_zip"]
    assert call_playbooks.soft_info_text(second, "en") == ""
    assert "playbook_requirement_promoted" in caplog.text
    assert "evidence_count=2" in caplog.text
    assert "independent_explicit_demands" in caplog.text


@pytest.mark.parametrize("reason", [
    "One-off human follow-up after transfer",
    "Call ended before the goal was achieved",
    "Caller account is automatically identified by the calling SIM",
])
def test_weak_model_evidence_cannot_promote_even_when_repeated(tmp_path, reason):
    path = tmp_path / "pb.json"
    for call_id in ("weak-a", "weak-b"):
        merged = merge_learned(
            "611", evidence_payload(eligible=False, reason=reason),
            call_id=call_id, updated="2026-10-21", path=path,
        )
        assert merged is not None
    assert missing_required_info(merged, None) == []
    assert "activation_zip" in call_playbooks.soft_info_text(merged, "zh")
    merged = merge_learned(
        "611", evidence_payload(), call_id="strong-a", updated="2026-10-21", path=path
    )
    assert missing_required_info(merged, None) == []


def test_legacy_single_source_is_soft_without_rewriting_and_not_strong_evidence(tmp_path):
    path = tmp_path / "pb.json"
    pb = sample_playbook()
    # Old learners also appended sourced fields to otherwise manual/seed playbooks.
    pb["required_info"][0]["source"] = {"call_id": "old-call", "date": "2026-08-18"}
    write_playbooks(path, [pb])
    original = path.read_bytes()
    loaded = lookup_playbook("611", path=path)
    assert [e["key"] for e in missing_required_info(loaded, None)] == ["activation_zip"]
    assert "account_pin" in call_playbooks.soft_info_text(loaded, "en")
    assert path.read_bytes() == original
    payload = evidence_payload()
    payload["new_required_info"][0]["key"] = "account_pin"
    merged = merge_learned(
        "611", payload, call_id="new-call", updated="2026-10-21", path=path
    )
    assert [e["key"] for e in missing_required_info(merged, None)] == ["activation_zip"]


def test_legacy_auto_discovered_entry_without_source_is_also_soft():
    pb = sample_playbook()
    pb["id"] = "learned_hotline"
    assert missing_required_info(pb, None) == []


@pytest.mark.parametrize("threshold", ["3", "1", "0", "-1", "invalid"])
def test_evidence_threshold_is_configurable_but_never_below_two(tmp_path, monkeypatch, threshold):
    monkeypatch.setenv("CALL_PLAYBOOKS_MIN_EVIDENCE_CALLS", threshold)
    path = tmp_path / "pb.json"
    for i in range(2):
        merged = merge_learned(
            "611", evidence_payload(), call_id=f"call-{i}", updated="2026-10-21", path=path
        )
    assert bool(missing_required_info(merged, None)) is (threshold != "3")
    if threshold == "3":
        merged = merge_learned(
            "611", evidence_payload(), call_id="call-3", updated="2026-10-21", path=path
        )
        assert missing_required_info(merged, None)


def test_ungraded_model_findings_default_to_soft(tmp_path):
    path = tmp_path / "pb.json"
    for call_id in ("a", "b"):
        merged = merge_learned(
            "611", learned_payload(), call_id=call_id, updated="2026-10-21", path=path
        )
        assert merged is not None
    assert missing_required_info(merged, None) == []


def test_evidence_is_per_field_and_deduplicates_repeated_demands_in_one_payload(tmp_path):
    path = tmp_path / "pb.json"
    payload = evidence_payload()
    payload["new_required_info"] *= 2
    merged = merge_learned("611", payload, call_id="a", updated="2026-10-21", path=path)
    assert call_playbooks.required_info_status(merged)[0]["evidence_count"] == 1
    other = evidence_payload()
    other["new_required_info"][0]["key"] = "other_field"
    merged = merge_learned("611", other, call_id="b", updated="2026-10-21", path=path)
    assert merged is not None and missing_required_info(merged, None) == []


@pytest.mark.parametrize("evidence", [
    None, "bad", [None, "bad", {"call_id": []}],
    [{"call_id": "a", "hard_eligible": True}] * 2,
    [{"call_id": "a", "hard_eligible": "true"}, {"call_id": "b", "hard_eligible": 1}],
])
def test_bad_or_duplicate_persisted_evidence_never_creates_strong_calls(evidence):
    pb = sample_playbook()
    for entry in pb["required_info"]:
        entry["evidence"] = evidence
    assert missing_required_info(pb, None) == []


@pytest.mark.parametrize("overrides", [
    {"hard_eligible": "true"}, {"hard_eligible": 1},
    {"evidence_reason": "PIN was 5566"}, {"evidence_reason": {}},
])
def test_invalid_evidence_metadata_is_rejected_without_writing(tmp_path, overrides):
    path = tmp_path / "pb.json"
    write_playbooks(path, [])
    before = path.read_bytes()
    payload = evidence_payload()
    payload["new_required_info"][0].update(overrides)
    with pytest.raises(PlaybookValidationError):
        merge_learned("611", payload, call_id="a", updated="2026-10-21", path=path)
    assert path.read_bytes() == before


def test_learner_reobserves_known_fields_and_receives_goal_and_caller_id_guidance(tmp_path, monkeypatch):
    path = _enable(monkeypatch, tmp_path)
    seen = []

    def fake_model(messages, **kwargs):
        seen.append(messages)
        return json.dumps(evidence_payload()), None

    monkeypatch.setattr("agentcall.prompt_gen.call_text_model", fake_model)
    for call_id in ("a", "b"):
        learned = learn_from_call(
            "611", [("user", "Please provide the verification field to proceed.")],
            {"verification": {"activation_zip": "55401"}},
            task="Check the current plan", call_id=call_id, updated="2026-10-21", path=path,
        )
    system = seen[-1][0]["content"]
    user = seen[-1][1]["content"]
    assert "including already known keys" in system
    assert "calling SIM's own carrier" in system
    assert "caller ID" in system
    assert "account number" in system
    assert "one-off human" in system
    assert "goal was not achieved" in system
    assert "hard_eligible" in system
    assert "Check the current plan" in user
    assert "activation_zip" in user and "55401" not in user
    assert missing_required_info(learned, None)
