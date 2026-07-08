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
