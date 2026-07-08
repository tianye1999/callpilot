"""summarize_call 单测：正常解析、markdown 围栏、异常兜底、空转写短路。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import dashscope
import pytest

from agentcall.summarizer import summarize_call

TRANSCRIPTS = [
    ("agent", "您好，我是李明的数字分身。"),
    ("user", "你好，我是顺丰快递员，有个包裹放驿站了，请让他尽快取。"),
    ("agent", "好的，我会转告李明。"),
]


def make_response(content: str, status_code: int = 200) -> SimpleNamespace:
    """构造与 dashscope GenerationResponse(result_format='message') 同形的对象。"""
    return SimpleNamespace(
        status_code=status_code,
        code="",
        message="",
        output=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        ),
    )


GOOD_PAYLOAD = {
    "caller_identity": "顺丰快递员",
    "intent": "通知包裹已放驿站，提醒尽快领取",
    "urgency": "中",
    "callback_needed": False,
    "summary": "快递员来电告知包裹已放驿站，李明需尽快去取，无需回电。",
}


def test_normal_parse(monkeypatch):
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return make_response(json.dumps(GOOD_PAYLOAD, ensure_ascii=False))

    monkeypatch.setattr(dashscope.Generation, "call", staticmethod(fake_call))
    monkeypatch.setenv("SUMMARY_MODEL", "qwen-test")

    result = summarize_call(TRANSCRIPTS, "inbound", "13800138000")

    assert result["ok"] is True
    assert result["error"] is None
    assert result["caller_identity"] == "顺丰快递员"
    assert result["intent"] == GOOD_PAYLOAD["intent"]
    assert result["urgency"] == "中"
    assert result["callback_needed"] is False
    assert result["summary"] == GOOD_PAYLOAD["summary"]
    # env 覆盖模型名 + prompt 里带上了转写与号码
    assert captured["model"] == "qwen-test"
    assert captured["result_format"] == "message"
    user_msg = captured["messages"][-1]["content"]
    assert "13800138000" in user_msg
    assert "顺丰快递员" in user_msg


def test_markdown_fenced_json(monkeypatch):
    fenced = "```json\n" + json.dumps(GOOD_PAYLOAD, ensure_ascii=False) + "\n```"
    monkeypatch.setattr(
        dashscope.Generation, "call", staticmethod(lambda **kw: make_response(fenced))
    )

    result = summarize_call(TRANSCRIPTS, "inbound", None)

    assert result["ok"] is True
    assert result["caller_identity"] == "顺丰快递员"


def test_missing_fields_get_defaults(monkeypatch):
    partial = json.dumps({"intent": "咨询", "urgency": "非法值"}, ensure_ascii=False)
    monkeypatch.setattr(
        dashscope.Generation, "call", staticmethod(lambda **kw: make_response(partial))
    )

    result = summarize_call(TRANSCRIPTS, "outbound", None)

    assert result["ok"] is True
    assert result["caller_identity"] == "未知"
    assert result["intent"] == "咨询"
    assert result["urgency"] == "中"  # 非法取值回落默认
    assert result["callback_needed"] is False
    assert result["summary"] == ""


def test_api_exception_returns_error(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(dashscope.Generation, "call", staticmethod(boom))

    result = summarize_call(TRANSCRIPTS, "inbound", "10086")

    assert result["ok"] is False
    assert "network down" in result["error"]
    assert result["urgency"] == "中"  # 兜底结果仍是完整契约结构


def test_non_200_status_returns_error(monkeypatch):
    resp = make_response("", status_code=429)
    resp.message = "Throttling"
    monkeypatch.setattr(
        dashscope.Generation, "call", staticmethod(lambda **kw: resp)
    )

    result = summarize_call(TRANSCRIPTS, "inbound", None)

    assert result["ok"] is False
    assert "429" in result["error"]


def test_invalid_json_returns_error(monkeypatch):
    monkeypatch.setattr(
        dashscope.Generation,
        "call",
        staticmethod(lambda **kw: make_response("对不起，我无法总结这通电话。")),
    )

    result = summarize_call(TRANSCRIPTS, "inbound", None)

    assert result["ok"] is False
    assert result["error"] is not None


@pytest.mark.parametrize(
    "transcripts",
    [
        [],
        [("agent", "您好，我是李明的数字分身。")],
        [("agent", "您好。"), ("user", "   ")],
    ],
)
def test_empty_or_agent_only_short_circuits(monkeypatch, transcripts):
    def must_not_call(**kwargs):
        raise AssertionError("短路场景不应调用 dashscope API")

    monkeypatch.setattr(dashscope.Generation, "call", staticmethod(must_not_call))

    result = summarize_call(transcripts, "inbound", None)

    assert result["ok"] is False
    assert result["error"] is not None


# ---- 多语言摘要（AGENT_LANGUAGE=en）----

def test_english_summary_prompt_and_normalize(monkeypatch):
    from agentcall import summarizer
    msgs = summarizer._build_messages(
        [("user", "Hi, is this a good time?"), ("agent", "Yes, go ahead.")],
        "inbound", "13800000000", "en",
    )
    sys_prompt = msgs[0]["content"]
    assert "call-log analysis assistant" in sys_prompt
    assert "通话记录" not in sys_prompt
    assert "Call direction" in msgs[1]["content"]

    # en urgency 值 high/medium/low 合法，中文「高」不合法回落 medium
    norm = summarizer._normalize({"urgency": "high", "summary": "ok"}, "en")
    assert norm["urgency"] == "high"
    norm2 = summarizer._normalize({"urgency": "高", "summary": "ok"}, "en")
    assert norm2["urgency"] == "medium"
    # 默认结果英文
    assert summarizer._default_result("en")["caller_identity"] == "unknown"
    assert summarizer._default_result("zh")["caller_identity"] == "未知"
