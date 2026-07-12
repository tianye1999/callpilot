"""前端静态页护栏：设置渲染标注与 XSS 高风险 API。"""

from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "src" / "agentcall" / "web" / "static" / "index.html"
REMOTE_HTML = INDEX.with_name("remote_dialer.html")
REMOTE_JS = INDEX.with_name("remote_dialer.js")
REMOTE_CSS = INDEX.with_name("remote_dialer.css")
REMOTE_MANIFEST = INDEX.with_name("manifest.webmanifest")
REMOTE_SW = INDEX.with_name("remote_dialer_sw.js")


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


def test_setup_requires_explicit_recording_choice_and_shows_storage_details():
    text = INDEX.read_text(encoding="utf-8")

    assert 'name="setupRecording"' in text
    assert 'value="true"' in text
    assert 'value="false"' in text
    assert "setupRecordingConfirmed" in text
    assert "setupRecordingSaved" in text
    assert "saveSetupRecording" in text
    assert 'postJson("/api/config", { RECORDING_ENABLED: enabled })' in text
    assert 'if (!setupRecordingSaved && !(await saveSetupRecording())) return;' in text
    assert 'postJson("/api/setup/complete", { recording_enabled: enabled })' in text
    assert 'radio.addEventListener("click"' in text
    assert "recordings_dir" in text
    assert 'cfgVal("RECORDING_RETENTION_DAYS")' in text


def test_recording_setting_and_active_call_show_context():
    text = INDEX.read_text(encoding="utf-8")

    assert 'c.key === "RECORDING_ENABLED"' in text
    assert 'el("span", "cfg-note", recordingDetailsText())' in text
    assert 'id="recordingBadge"' in text
    assert 'classList.toggle("show", callActive && recordingEnabled)' in text


def test_setup_prefill_finishes_before_wizard_accepts_input():
    text = INDEX.read_text(encoding="utf-8")

    assert "async function showSetupWizard(manual, meta)" in text
    assert "await prefillSetupFields();" in text


def test_websocket_reconnect_clears_stale_call_and_recording_state():
    text = INDEX.read_text(encoding="utf-8")

    assert 'ws.onopen = () => {' in text
    assert 'setCall("idle", "");' in text


def test_dashboard_listener_recovers_suspended_web_audio():
    """WebKit 打断 AudioContext 后，旁听要恢复而不是保持“旁听中”但静音。"""
    text = INDEX.read_text(encoding="utf-8")

    assert "async function ensureListenAudioRunning()" in text
    assert 'if (audioCtx.state === "running") return true;' in text
    assert "await audioCtx.resume();" in text
    assert "if (!(await ensureListenAudioRunning()))" in text
    assert "void resumeAndPlayPcm(int16, kind);" in text


def test_settings_expose_sms_email_forwarding_with_bilingual_privacy_notice():
    text = INDEX.read_text(encoding="utf-8")

    for key in (
        "SMS_EMAIL_FORWARD_ENABLED",
        "SMS_EMAIL_RECIPIENT",
        "SMS_EMAIL_SMTP_HOST",
        "SMS_EMAIL_SMTP_PORT",
        "SMS_EMAIL_SMTP_SECURITY",
        "SMS_EMAIL_SMTP_USERNAME",
        "SMS_EMAIL_SMTP_PASSWORD",
        "SMS_EMAIL_FROM",
    ):
        assert key in text
    assert "SMS content will be sent to the configured email address" in text
    assert "短信内容将发送到你配置的收件邮箱" in text
    assert 'el("span", "cfg-note", t("sms_email_privacy"))' in text


def test_history_recording_players_stop_click_propagation():
    text = INDEX.read_text(encoding="utf-8")

    assert 'const box = el("div", "rec-audio");' in text
    assert 'box.addEventListener("click", (event) => event.stopPropagation());' in text
    assert 'a.addEventListener("click", (event) => event.stopPropagation());' in text


def test_profile_manager_has_crud_controls_and_safe_rendering():
    text = INDEX.read_text(encoding="utf-8")

    for element_id in (
        "page-profiles",
        "profileList",
        "profileSearch",
        "profileNew",
        "profileForm",
        "profileSave",
        "profileDelete",
    ):
        assert f'id="{element_id}"' in text
    assert 'fetch("/api/number_profiles/manage")' in text
    assert 'method: id ? "PATCH" : "POST"' in text
    assert 'method: "DELETE"' in text
    assert 'preset_id: dialPresetId' in text
    assert 'title.appendChild(el("b", "", profileLangValue(profile, "label")' in text

    # #80-B:opening_mode UI 控件存在（select + read/open/blank/form payload 四处）
    assert 'id="profileOpeningMode"' in text
    assert 'value="say"' in text       # option
    assert 'value="wait"' in text      # option
    assert 'profile_opening_mode' in text   # i18n key
    assert 'opening_mode: "say"' in text    # blankManagedProfile 默认
    assert 'opening_mode: $("profileOpeningMode").value' in text  # readProfileForm
    assert '$("profileOpeningMode").value' in text  # openProfileEditor set


def test_manual_dial_requires_explicit_task_or_selected_preset():
    """#80-F:空任务不能静默沿用历史配置；用户必须填写或选择预设。"""
    text = INDEX.read_text(encoding="utf-8")

    assert 'ph_task: "Describe what this call should accomplish"' in text
    assert 'ph_task: "描述本次要完成的事项"' in text
    assert 'if (!task && !dialPresetId)' in text
    assert 'setToast("dialToast", "err", t("need_task"));' in text
    assert "loadDefaultTask();" not in text
    assert "blank = reuse" not in text
    assert "空=沿用" not in text


def test_remote_dialer_assets_are_mobile_safe_and_xss_hardened():
    html = REMOTE_HTML.read_text(encoding="utf-8")
    script = REMOTE_JS.read_text(encoding="utf-8")
    css = REMOTE_CSS.read_text(encoding="utf-8")

    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "localStorage",
        "console.log",
    ):
        assert forbidden not in html
        assert forbidden not in script
    assert 'name="viewport"' in html
    assert "env(safe-area-inset-top)" in css
    assert 'role="status"' in html
    assert "textContent" in script


def test_remote_dialer_uses_pinned_livekit_sri_and_fragment_credentials():
    html = REMOTE_HTML.read_text(encoding="utf-8")
    script = REMOTE_JS.read_text(encoding="utf-8")

    assert "livekit-client@2.20.1" in html
    assert 'integrity="sha384-' in html
    assert "Content-Security-Policy" in html
    assert "callpilot.control" in script
    assert "callpilot.status" in script
    assert "navigator.mediaDevices.getUserMedia" in script
    assert "publishTrack" in script
    assert "echoCancellation: true" in script
    assert "replaceChildren" in script
    assert script.index("history.replaceState") < script.index("JSON.parse(atob")
    assert "window.top !== window.self" in script


def test_remote_dialer_supports_cookie_pairing_fixed_entry_and_pwa_without_browser_token_storage():
    html = REMOTE_HTML.read_text(encoding="utf-8")
    script = REMOTE_JS.read_text(encoding="utf-8")
    manifest = REMOTE_MANIFEST.read_text(encoding="utf-8")
    service_worker = REMOTE_SW.read_text(encoding="utf-8")

    for element_id in (
        "pairingSurface",
        "pairingCode",
        "deviceName",
        "pairButton",
        "pairedDevice",
        "unpairButton",
    ):
        assert f'id="{element_id}"' in html
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'fetch("/api/device"' in script
    assert 'postJson("/api/pair"' in script
    assert 'postJson("/api/session"' in script
    assert 'postJson("/api/unpair"' in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "document.cookie" not in script
    assert 'navigator.serviceWorker.register("/remote_dialer_sw.js?v=2"' in script
    assert 'updateViaCache: "none"' in script
    assert 'href="/remote_dialer.css?v=2"' in html
    assert 'src="/remote_dialer.js?v=2"' in html
    assert 'const CACHE_NAME = "callpilot-remote-v2"' in service_worker
    assert '"/remote_dialer.css?v=2"' in service_worker
    assert '"/remote_dialer.js?v=2"' in service_worker
    assert "self.skipWaiting()" in service_worker
    assert "self.clients.claim()" in service_worker
    assert '"start_url": "/"' in manifest
    assert '"src": "/callpilot-192.png"' in manifest
    assert '"src": "/callpilot-512.png"' in manifest
    assert 'url.pathname.startsWith("/api/")' in service_worker


def test_dashboard_has_pairing_device_list_revoke_and_legacy_invite_fallback():
    text = INDEX.read_text(encoding="utf-8")

    for element_id in (
        "remotePairBtn",
        "remotePairPanel",
        "remotePairCode",
        "remotePairUrl",
        "remoteDeviceList",
        "remoteInviteBtn",
    ):
        assert f'id="{element_id}"' in text
    assert 'postJson("/api/remote_dialer/pairing"' in text
    assert 'fetch("/api/remote_dialer/devices"' in text
    assert 'method: "DELETE"' in text
    assert '"/api/remote_dialer/devices/" + encodeURIComponent(deviceId)' in text
