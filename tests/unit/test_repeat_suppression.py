"""复读抑制：纯文本相似度判重。"""

from __future__ import annotations

from agentcall.repeat_suppression import (
    RepeatSuppressor,
    ResponseAudioGate,
    is_repetitive,
    normalize_for_similarity,
)


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


def test_repeat_suppressor_allows_second_occurrence_then_suppresses_third():
    suppressor = RepeatSuppressor(threshold_getter=lambda: 0.9)
    first = "您好，我是张三的数字分身，想咨询一下套餐情况。"
    repeated = "您好！我是张三的数字分身，想咨询一下套餐情况"

    assert suppressor.should_suppress(first) is False
    assert suppressor.should_suppress(repeated) is False
    assert suppressor.should_suppress(repeated) is True


def test_response_gate_suppression_nudges_with_cooldown_and_stuck_limit():
    now = 100.0
    emitted: list[bytes] = []
    nudges: list[str] = []
    stuck: list[tuple[int, str]] = []
    gate = ResponseAudioGate(
        "test",
        emitted.append,
        suppressor=RepeatSuppressor(threshold_getter=lambda: 0.9),
        on_suppressed=lambda text: nudges.append(text),
        on_stuck=lambda count, text: stuck.append((count, text)),
        time_fn=lambda: now,
        nudge_cooldown_seconds=8.0,
        stuck_limit=3,
    )
    first = "您好，我是张三的数字分身，想咨询一下套餐情况。"
    repeated = "您好！我是张三的数字分身，想咨询一下套餐情况"

    gate.push_audio("r1", b"first")
    assert gate.complete_transcript("r1", first) is False
    gate.push_audio("r2", b"second")
    assert gate.complete_transcript("r2", repeated) is False
    gate.push_audio("r3", b"third")
    assert gate.complete_transcript("r3", repeated) is True
    gate.push_audio("r4", b"fourth")
    assert gate.complete_transcript("r4", repeated) is True
    now = 109.0
    gate.push_audio("r5", b"fifth")
    assert gate.complete_transcript("r5", repeated) is True

    assert emitted == [b"first", b"second"]
    assert nudges == [repeated, repeated]
    assert stuck == [(3, repeated)]


def test_response_gate_resets_suppression_streak_after_different_content():
    emitted: list[bytes] = []
    stuck: list[tuple[int, str]] = []
    gate = ResponseAudioGate(
        "test",
        emitted.append,
        suppressor=RepeatSuppressor(threshold_getter=lambda: 0.9),
        on_stuck=lambda count, text: stuck.append((count, text)),
        stuck_limit=2,
    )
    first = "您好，我是张三的数字分身，想咨询一下套餐情况。"
    repeated = "您好！我是张三的数字分身，想咨询一下套餐情况"
    different = "请问现在转人工应该按几号键？"

    gate.push_audio("r1", b"first")
    gate.complete_transcript("r1", first)
    gate.push_audio("r2", b"second")
    gate.complete_transcript("r2", repeated)
    gate.push_audio("r3", b"third")
    assert gate.complete_transcript("r3", repeated) is True

    gate.push_audio("r4", b"different")
    assert gate.complete_transcript("r4", different) is False
    gate.push_audio("r5", b"repeat-after-reset")
    assert gate.complete_transcript("r5", repeated) is False

    assert emitted == [b"first", b"second", b"different", b"repeat-after-reset"]
    assert stuck == []
