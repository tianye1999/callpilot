"""复读抑制：纯文本相似度判重。"""

from __future__ import annotations

from agentcall.repeat_suppression import is_repetitive, normalize_for_similarity


def test_repetitive_detects_high_similarity_after_normalization():
    recent = ["您好，我是张三的数字分身，想咨询一下套餐情况。"]

    assert is_repetitive(
        "您好！我是张三的数字分身，想咨询一下套餐情况",
        recent,
        threshold=0.9,
    )


def test_repetitive_allows_different_content():
    recent = ["您好，我是张三的数字分身，想咨询一下套餐情况。"]

    assert not is_repetitive(
        "请问现在转人工需要按几号键？",
        recent,
        threshold=0.9,
    )


def test_repetitive_short_reply_exempted():
    assert not is_repetitive("好的", ["好的"], threshold=0.9)
    assert not is_repetitive("您好", ["您好"], threshold=0.9)


def test_repetitive_threshold_zero_disables():
    assert not is_repetitive(
        "您好，我是张三的数字分身，想咨询套餐。",
        ["您好，我是张三的数字分身，想咨询套餐。"],
        threshold=0,
    )


def test_similarity_normalization_removes_punctuation_and_spacing():
    assert normalize_for_similarity(" 您好， 我是 AI。 ") == "您好我是ai"
