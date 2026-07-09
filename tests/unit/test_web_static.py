"""前端静态页护栏：设置渲染标注与 XSS 高风险 API。"""

from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "src" / "agentcall" / "web" / "static" / "index.html"


def test_index_does_not_use_html_injection_apis():
    text = INDEX.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "outerHTML" not in text
    assert "insertAdjacentHTML" not in text
    assert "dangerouslySetInnerHTML" not in text


def test_settings_render_uses_choice_labels_for_provider_badges():
    text = INDEX.read_text(encoding="utf-8")
    assert "(c.choice_labels && c.choice_labels[ch]) || ch" in text
    assert '<option value="doubao">Doubao (experimental)</option>' in text


def test_setup_qwen_key_help_has_safe_console_link():
    text = INDEX.read_text(encoding="utf-8")
    assert 'id="setupQwenKeyHelp"' in text
    assert "QWEN_API_KEY_URL" in text
    assert "https://bailian.console.aliyun.com/?tab=api#/api-key" in text
    assert 'link.rel = "noopener noreferrer"' in text
    assert "Qwen / DashScope API Key" in text
    assert "免费调用额度" in text


def test_setup_sms_copy_spells_out_receiver_number():
    text = INDEX.read_text(encoding="utf-8")
    assert "Phone number to receive the test SMS" in text
    assert "接收测试短信的手机号" in text
    assert "you can receive on" in text
    assert "你能接收短信的手机号" in text
    assert "It is listed in the SMS tab" in text
    assert "可在短信页查看" in text


def test_history_recording_players_stop_click_propagation():
    text = INDEX.read_text(encoding="utf-8")

    assert 'const box = el("div", "rec-audio");' in text
    assert 'box.addEventListener("click", (event) => event.stopPropagation());' in text
    assert 'a.addEventListener("click", (event) => event.stopPropagation());' in text
