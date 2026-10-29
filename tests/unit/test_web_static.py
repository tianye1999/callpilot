"""前端静态页护栏：设置渲染标注与 XSS 高风险 API。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Windows CI 上 Node 冷启动可能超过 10 秒（3.12 腿实测超时），放宽到 60 秒避免偶发红。
NODE_TIMEOUT = 60

INDEX = Path(__file__).resolve().parents[2] / "src" / "agentcall" / "web" / "static" / "index.html"
REMOTE_HTML = INDEX.with_name("remote_dialer.html")
REMOTE_JS = INDEX.with_name("remote_dialer.js")
REMOTE_CSS = INDEX.with_name("remote_dialer.css")
REMOTE_MANIFEST = INDEX.with_name("manifest.webmanifest")
REMOTE_SW = INDEX.with_name("remote_dialer_sw.js")


def test_browser_language_defaults_and_saved_override():
    node = shutil.which("node")
    if node is None:
        pytest.skip("需要 Node 执行真实前端语言选择函数")
    text = INDEX.read_text(encoding="utf-8")
    start = text.index("function initialUiLanguage(")
    end = text.index("\nlet LANG =", start)
    source = text[start:end]
    result = subprocess.run([node, "-e", source + '''
const assert = require('node:assert/strict');
assert.equal(initialUiLanguage(null, ['zh-CN']), 'zh');
assert.equal(initialUiLanguage(null, ['zh-Hant-TW']), 'zh');
assert.equal(initialUiLanguage(null, ['en-US', 'zh-CN']), 'zh');
assert.equal(initialUiLanguage(null, ['ja-JP']), 'en');
assert.equal(initialUiLanguage(null, []), 'en');
assert.equal(initialUiLanguage('en', ['zh-CN']), 'en');
assert.equal(initialUiLanguage('zh', ['en-US']), 'zh');
assert.equal(initialUiLanguage('invalid', ['zh-CN']), 'zh');
'''], capture_output=True, text=True, timeout=NODE_TIMEOUT)
    assert result.returncode == 0, result.stderr
    assert 'navigator.languages || []' in text and 'navigator.language || ""' in text


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


def test_setup_local_defaults_voices_key_and_call_results_in_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("需要 Node 执行真实向导函数")
    text = INDEX.read_text(encoding="utf-8")
    advanced = text[text.index('<details id="setupAdvanced">'):]
    advanced = advanced[:advanced.index("</details>")]
    assert " open" not in advanced.split(">", 1)[0]
    assert all(f'value="{provider}"' in advanced for provider in ("qwen", "openai", "doubao"))
    source = text[text.index("let setupStep = 0;"):text.index("const truthy =")]
    harness = r'''
const assert = require('node:assert/strict');
class Element {
  constructor() { this.value = ''; this.style = {}; this.dataset = {}; this.children = [];
    this.classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } }; }
  set textContent(v) { this.text = v; this.children = []; }
  get textContent() { return this.text || ''; }
  appendChild(v) { this.children.push(v); }
  addEventListener() {}
}
const elements = new Map();
const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const document = { querySelectorAll: () => [], createElement: () => new Element(), createTextNode: text => ({text}) };
const window = { addEventListener() {} };
const el = (tag, cls, text) => { const node = new Element(); node.textContent = text; return node; };
const t = key => key === 'setup_call_target' ? number => number : key;
const setInterval = () => 0;
const CFG_SUGGEST = { QWEN_VOICE: ['Cherry'], OPENAI_VOICE: ['alloy'] };
let cfgSpecs = [];
let lastMeta = {};
let callActive = false;
const boolChecked = value => value === 'true';
const loadMeta = async () => {};
const buildRecPlayers = id => ({recording: id});
const posts = [];
const postJson = async (url, data) => {
  posts.push({url,data}); return {httpOk: true, res: {ok: true, status: 'valid'}};
};
'''
    assertions = r'''
(async () => {
  cfgSpecs = [{key: 'OWNER_NAME', value: 'Alex'}];
  await prefillSetupFields();
  assert.equal($('setupProvider').value, 'local');
  assert.equal($('setupAdvanced').open, false);
  assert.equal($('setupApiKeyLabel').textContent, 'DeepSeek API Key');
  assert.equal($('setupDeepseekKeyHelp').hidden, false);
  assert.equal($('setupVoice').hidden, true);
  assert.equal($('setupLocalVoice').hidden, false);
  await saveSetupPersonal();
  assert.equal(posts.at(-1).data.LOCAL_TTS_SPEAKER_ID, 25);
  $('setupLocalVoice').value = 'vits-piper-zh_CN-chaowen-medium-int8';
  await saveSetupPersonal();
  assert.equal(posts.at(-1).data.LOCAL_TTS_SPEAKER_ID, 0);
  $('setupApiKey').value = 'sk-fake';
  assert.equal(await validateSetupKey(), true);
  assert.deepEqual(posts.at(-2).data, {provider: 'deepseek', api_key: 'sk-fake'});
  assert.deepEqual(posts.at(-1).data, {DEEPSEEK_API_KEY: 'sk-fake'});
  cfgSpecs = [{key: 'AGENT_PROVIDER', value: 'openai'}];
  await prefillSetupFields();
  assert.equal($('setupProvider').value, 'openai');
  assert.equal($('setupAdvanced').open, true);
  assert.equal($('setupLocalVoice').hidden, true);
  lastMeta = {sim: {service_number: '611'}, setup_call: {state: 'running'}};
  renderSetupCall();
  assert.equal($('setupStartCall').disabled, true);
  lastMeta.setup_call = {state: 'done', status: 'completed', call_id: 'test',
    received: {ok: true, reason: 'energy'}, sent: {ok: false, reason: 'no_write'}};
  renderSetupCall();
  assert.equal($('setupStartCall').disabled, false);
  assert.equal($('setupCallResults').children.length, 2);
  assert.match($('setupCallResults').children[0].children[1].textContent, /setup_call_pass/);
  assert.match($('setupCallResults').children[1].children[1].textContent, /setup_call_fail/);
  assert.equal($('setupCallPlayback').children[0].recording, 'test');
  const player = $('setupCallPlayback').children[0];
  renderSetupCall();
  assert.equal($('setupCallPlayback').children[0], player);
  lastMeta.setup_call.recording_error = true;
  delete lastMeta.setup_call.call_id;
  renderSetupCall();
  assert.equal($('setupCallResults').children.at(-1).textContent, 'setup_call_recording_error');
  assert.equal($('setupCallPlayback').children.length, 0);
  await refreshSetupNotifications();
  assert.equal($('setupNotifications').hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", harness + source + assertions],
                            capture_output=True, text=True, encoding="utf-8", timeout=NODE_TIMEOUT)
    assert result.returncode == 0, result.stderr


def test_websocket_reconnect_clears_stale_call_and_recording_state():
    text = INDEX.read_text(encoding="utf-8")

    assert 'ws.onopen = () => {' in text
    assert 'setCall("idle", "");' in text


def test_modem_and_sim_events_debounce_metadata_refresh():
    text = INDEX.read_text(encoding="utf-8")

    assert "function scheduleMetaRefresh()" in text
    assert 'ev.type === "modem_status" || ev.type === "sim_status"' in text
    assert "clearTimeout(metaRefreshTimer)" in text
    assert "metaRefreshTimer = setTimeout(() => { void loadMeta(); }" in text


def test_modem_reset_state_is_preserved_in_meta_events_and_language_switches():
    text = INDEX.read_text(encoding="utf-8")
    assert "模组重启中（约 30 秒）" in text
    assert "Modem restarting (about 30 seconds)" in text
    assert "function setModem(connected, resetting = false)" in text
    assert 'setModem(!!ev.connected, !!ev.modem_resetting)' in text
    assert 'setModem(!!m.hardware.modem_connected, !!m.hardware.modem_resetting)' in text
    assert 'setModem(modemConnected, modemResetting)' in text
    assert 'modemResetting ? "modem_resetting"' in text


def test_setup_hardware_shows_lte_registration_for_volte_only_sim():
    text = INDEX.read_text(encoding="utf-8")

    assert "!sim.registered && sim.eps_registered" in text
    assert 't("setup_network_lte")' in text
    assert "${sim.carrier} · ${simRegistration}" in text
    assert 'sim.network_attached ? "ready"' in text
    assert 'hw.port || "-"' not in text
    assert 'postJson("/api/modem/reread_sim", {})' in text
    assert 'setup_hint_denied:' in text


def test_dashboard_listener_recovers_suspended_web_audio():
    """WebKit 打断 AudioContext 后，旁听要恢复而不是保持“旁听中”但静音。"""
    text = INDEX.read_text(encoding="utf-8")

    assert "async function ensureListenAudioRunning()" in text
    assert 'if (audioCtx.state === "running") return true;' in text
    assert "await audioCtx.resume();" in text
    assert "if (!(await ensureListenAudioRunning()))" in text
    assert "void resumeAndPlayPcm(int16, kind);" in text


def test_listen_anchor_navigates_highlights_without_audio_and_respects_unsaved_changes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("需要 Node 执行真实前端锚点函数")
    text = INDEX.read_text(encoding="utf-8")
    assert 'id="listen"' in text
    assert 'window.addEventListener("hashchange", focusListenAnchor);' in text
    assert '.listen-btn.listen-target { outline: 3px solid var(--blue)' in text
    assert '$("listenBtn").addEventListener("click", () => $("listenBtn").classList.remove("listen-target"));' in text
    start = text.index("async function focusListenAnchor()")
    end = text.index('\nwindow.addEventListener("hashchange"', start)
    result = subprocess.run([node, "-e", text[start:end] + '''
const assert = require('node:assert/strict');
const location = {hash: '#listen'};
let currentPage = 'settings', allowNavigation = true;
const events = [];
async function navTo(name) { if (allowNavigation) currentPage = name; }
function $(id) {
  return {scrollIntoView: () => events.push('scroll:' + id),
          focus: () => events.push('focus:' + id),
          classList: {add: name => events.push(name)}};
}
(async () => {
  await focusListenAnchor();
  assert.equal(currentPage, 'live');
  assert.deepEqual(events, ['scroll:listen', 'listen-target', 'focus:listenBtn']);
  events.length = 0;
  currentPage = 'settings'; allowNavigation = false;
  await focusListenAnchor();
  assert.deepEqual(events, []);
  location.hash = '#metrics'; allowNavigation = true;
  await focusListenAnchor();
  assert.equal(currentPage, 'settings');
  assert.deepEqual(events, []);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''], capture_output=True, text=True, encoding="utf-8", timeout=NODE_TIMEOUT)
    assert result.returncode == 0, result.stderr


def test_settings_restart_entry_uses_existing_api_and_confirmation():
    text = INDEX.read_text(encoding="utf-8")
    assert 'id="restartBtn"' in text
    assert '$("restartBtn").addEventListener("click", confirmRestart)' in text
    assert 'if (choice === "ok") restartService();' in text
    assert 'postJson("/api/restart", {})' in text


def test_dashboard_listener_resets_only_downlink_timeline_on_rate_change():
    text = INDEX.read_text(encoding="utf-8")

    assert "if (m.rate && m.rate !== listenRate[0])" in text
    assert "listenRate[0] = m.rate; listenNextAt[0] = 0;" in text
    assert "if (m.uplink_rate) listenRate[1] = m.uplink_rate;" in text


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
    assert 'id="profileDtmfSpokenFollowup"' in text
    assert "dtmf_spoken_followup: false" in text
    assert 'dtmf_spoken_followup: $("profileDtmfSpokenFollowup").checked' in text
    assert 'id="profileResultVerification"' in text
    assert 'result_verification: "none"' in text
    assert 'result_verification: $("profileResultVerification").value' in text
    assert 'item.result_verification === "carrier_sms"' in text


def test_history_marks_verified_and_unverified_result_sources():
    text = INDEX.read_text(encoding="utf-8")

    assert 'summary.result_verification === "verified"' in text
    assert 'summary.result_verification === "unverified"' in text
    assert 't("summary_verified_sms")' in text
    assert 't("summary_unverified")' in text


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


def test_intake_dial_goes_through_a_confirmation_step():
    """模型给的号码不得一键直拨（WIL-130）。

    硬约束要求「拨号前核对屏幕号码」；建单流程里号码由模型产出，直落
    doDial() 等于绕过这条。删掉确认环节本测试必须变红。
    """
    text = INDEX.read_text(encoding="utf-8")
    assert "function confirmIntakeDial(" in text, "确认弹窗被删了"
    assert "confirmIntakeDial(d.number, doDial)" in text, "建单拨号未走确认"
    # 逐位分隔展示：连读一串数字最容易看漏一位。
    assert 'String(number).split("").join(" ")' in text
    for key in ("dial_confirm_title", "dial_confirm_ask", "dial_confirm_yes"):
        assert text.count(key) >= 3, f"{key} 缺中英任一份文案或未被引用"
