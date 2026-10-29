"""Incremental clause splitting and PCM normalization, without models."""

import numpy as np
import pytest

from agentcall.agents.local_speech import ClauseSplitter, normalize_peak


@pytest.mark.parametrize("mark", list("，。！？；：、,.!?;:．"))
def test_split_at_chinese_and_english_punctuation(mark):
    splitter = ClauseSplitter()
    assert splitter.feed(f"你好{mark}世界。") == [f"你好{mark}", "世界。"]
    assert splitter.finish() == []


def test_short_clause_merges_forward_and_preserves_text():
    splitter = ClauseSplitter()
    assert splitter.feed("好，") == []
    assert splitter.feed("我来查询。") == ["好，我来查询。"]


def test_first_chinese_clause_needs_two_han_characters():
    splitter = ClauseSplitter()
    assert splitter.feed("A好，") == []
    assert splitter.feed("请稍等。") == ["A好，请稍等。"]


def test_long_text_is_forced_at_twenty_characters():
    splitter = ClauseSplitter()
    assert splitter.feed("中" * 45) == ["中" * 20, "中" * 20]
    assert splitter.finish() == ["中" * 5]
    assert splitter.finish() == []


def test_incremental_input_matches_whole_input_and_flushes_tail():
    text = "嗯，您好！Please wait,我正在查询剩余额度"
    whole = ClauseSplitter()
    expected = whole.feed(text) + whole.finish()
    incremental = ClauseSplitter()
    actual = []
    for char in text:
        actual.extend(incremental.feed(char))
    assert actual + incremental.finish() == expected
    assert "".join(expected) == text


def test_finish_does_not_synthesize_punctuation_only_tail():
    splitter = ClauseSplitter()
    assert splitter.feed("你好？！") == ["你好？"]
    assert splitter.finish() == []


@pytest.mark.parametrize("parts, expected", [
    (["（保", "持等待）"], ""),
    (["(保", "持等待)"], ""),
    (["好的（停顿）我帮您查"], "好的我帮您查"),
    (["好的（等待，", "再等。" * 20, "）请讲。"], "好的请讲。"),
    (["（外层(内层)继续）？！"], ""),
    (["好的（未闭合"], "好的"),
    (["！" * 40], ""),
])
def test_stage_directions_are_statefully_removed_before_splitting(parts, expected):
    splitter = ClauseSplitter()
    clauses = [clause for part in parts for clause in splitter.feed(part)]
    assert "".join(clauses + splitter.finish()) == expected


@pytest.mark.parametrize("text", [
    "余额为84.31元", "现在时间21:26", "余额为1,000元",
    "余额为８４．３１元", "现在时间２１：２６", "共19元。",
])
def test_numeric_separators_do_not_split_amounts_or_times(text):
    splitter = ClauseSplitter()
    assert splitter.feed(text) + splitter.finish() == [text]


@pytest.mark.parametrize("separator", list(".,:．："))
def test_numeric_separator_waits_across_delta_boundary(separator):
    splitter = ClauseSplitter()
    assert splitter.feed(f"当前数值84{separator}") == []
    assert splitter.feed("31") == []
    assert splitter.feed("元。") == [f"当前数值84{separator}31元。"]
    assert splitter.finish() == []


@pytest.mark.parametrize("separator", list(".,:．："))
def test_digit_then_separator_splits_when_next_character_is_not_digit(separator):
    splitter = ClauseSplitter()
    assert splitter.feed(f"当前数值84{separator}") == []
    assert splitter.feed("请稍等。") == [f"当前数值84{separator}", "请稍等。"]


@pytest.mark.parametrize("separator", list(".,:．："))
def test_finish_flushes_pending_numeric_separator(separator):
    splitter = ClauseSplitter()
    text = f"当前数值84{separator}"
    assert splitter.feed(text) == []
    assert splitter.finish() == [text]
    assert splitter.finish() == []


def test_force_cut_does_not_reintroduce_split_inside_numeric_amount():
    splitter = ClauseSplitter()
    prefix = "中" * 17
    assert splitter.feed(prefix + "84.") == []
    assert splitter.feed("31元") == [prefix + "84.31元"]


def test_peak_normalization_is_optional_and_caps_gain_at_twelve_db():
    pcm = np.array([100, -100, 0], dtype="<i2").tobytes()
    assert normalize_peak(pcm, None) == pcm
    result = np.frombuffer(normalize_peak(pcm, 0.9), dtype="<i2")
    assert 397 <= result[0] <= 399
    loud = np.array([32767, -32768], dtype="<i2").tobytes()
    assert max(abs(np.frombuffer(normalize_peak(loud, 0.9), dtype="<i2").astype(int))) <= 29491
    assert normalize_peak(b"\0\0" * 10, 0.9) == b"\0\0" * 10
