(() => {
  "use strict";

  if (window.top !== window.self) {
    document.documentElement.textContent = "";
    return;
  }

  const CONTROL_TOPIC = "callpilot.control";
  const STATUS_TOPIC = "callpilot.status";
  const NUMBER_RE = /^\+?[0-9*#]{1,32}$/;
  const PAIR_CODE_RE = /^[23456789A-HJ-NP-Z]{4}-?[23456789A-HJ-NP-Z]{4}$/;
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  const $ = (id) => document.getElementById(id);

  const text = {
    en: {
      remote_line: "REMOTE LINE",
      title: "Call with your SIM",
      number_label: "Phone number",
      call: "Call",
      hangup: "Hang up",
      ready: "Ready",
      connecting: "Connecting",
      waiting_for_phone: "Connecting phone",
      media_ready: "Audio ready",
      dialing: "Dialing",
      connected: "On call",
      ended: "Call ended",
      failed: "Call failed",
      invalid_invite: "Invalid or expired link",
      invalid_number: "Enter a valid number",
      microphone_denied: "Microphone access is required",
      connection_failed: "Edge is unavailable",
      dtmf_failed: "Key was not sent",
      pair_title: "Pair this phone",
      pair_code_label: "Pairing code",
      device_name_label: "Device name",
      pair: "Pair phone",
      pairing: "Pairing",
      paired: "Paired",
      pair_failed: "Pairing failed",
      invalid_pair_code: "Enter the code shown by CallPilot",
      unpair: "Unpair",
      default_device_name: "My phone",
      edge_disabled: "Remote dialing is disabled",
      edge_unconfigured: "Remote dialing is not configured",
    },
    zh: {
      remote_line: "远程 SIM 线路",
      title: "使用 SIM 卡拨号",
      number_label: "电话号码",
      call: "拨打",
      hangup: "挂断",
      ready: "可以拨号",
      connecting: "连接中",
      waiting_for_phone: "正在连接手机",
      media_ready: "音频已就绪",
      dialing: "拨号中",
      connected: "通话中",
      ended: "通话已结束",
      failed: "呼叫失败",
      invalid_invite: "链接无效或已过期",
      invalid_number: "请输入有效号码",
      microphone_denied: "需要允许麦克风权限",
      connection_failed: "电脑端暂时不可用",
      dtmf_failed: "按键发送失败",
      pair_title: "配对这台手机",
      pair_code_label: "配对码",
      device_name_label: "设备名称",
      pair: "配对手机",
      pairing: "正在配对",
      paired: "已配对",
      pair_failed: "配对失败",
      invalid_pair_code: "请输入 CallPilot 电脑端显示的配对码",
      unpair: "解除配对",
      default_device_name: "我的手机",
      edge_disabled: "电脑端未启用远程拨号",
      edge_unconfigured: "电脑端远程拨号配置不完整",
    },
  };

  const language = navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
  const t = (key) => text[language][key] || text.en[key] || key;
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });

  const statusPill = $("statusPill");
  const statusText = $("statusText");
  const pairingSurface = $("pairingSurface");
  const callSurface = $("callSurface");
  const pairingCode = $("pairingCode");
  const deviceName = $("deviceName");
  const pairButton = $("pairButton");
  const pairedRow = $("pairedRow");
  const pairedDevice = $("pairedDevice");
  const unpairButton = $("unpairButton");
  const numberInput = $("dialNumber");
  const callButton = $("callButton");
  const hangupButton = $("hangupButton");
  const keypadButtons = Array.from(document.querySelectorAll("#keypad button"));
  const audioHost = $("audioHost");

  let invite = null;
  let paired = false;
  let room = null;
  let microphoneTrack = null;
  let mediaReady = false;
  let callState = "idle";
  let mediaReadyResolve = null;
  let terminal = false;

  function setStatus(status, detail) {
    callState = status;
    document.body.dataset.callState = status;
    let tone = "working";
    if (["idle", "media_ready", "paired", "ended"].includes(status)) tone = "ready";
    if (status === "connected") tone = "connected";
    if (status === "failed") tone = "error";
    statusPill.dataset.state = tone;
    const known = t(status);
    statusText.textContent = detail || (known === status ? t("connection_failed") : known);

    const connected = status === "connected";
    const busy = ["connecting", "waiting_for_phone", "media_ready", "dialing", "connected"].includes(status);
    numberInput.disabled = busy;
    callButton.disabled = busy || terminal || (!paired && !invite);
    hangupButton.disabled = !["connecting", "media_ready", "dialing", "connected"].includes(status);
    keypadButtons.forEach((button) => { button.disabled = !connected; });
  }

  function showPairing() {
    pairingSurface.hidden = false;
    callSurface.hidden = true;
    pairedRow.hidden = true;
  }

  function showDialer(device) {
    pairingSurface.hidden = true;
    callSurface.hidden = false;
    pairedRow.hidden = !device;
    pairedDevice.textContent = device ? device.display_name : "";
  }

  function stopMicrophone() {
    if (microphoneTrack) microphoneTrack.stop();
    microphoneTrack = null;
  }

  function takeFragment() {
    const fragment = window.location.hash.slice(1);
    history.replaceState(null, "", window.location.pathname + window.location.search);
    return fragment.length <= 8192 ? fragment : "";
  }

  function parseInviteFragment(fragment) {
    if (!fragment || fragment.startsWith("pair=")) return null;
    try {
      const normalized = fragment.replace(/-/g, "+").replace(/_/g, "/");
      const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
      const payload = JSON.parse(atob(padded));
      const url = new URL(payload.url);
      if (
        payload.v !== 1 ||
        url.protocol !== "wss:" ||
        typeof payload.token !== "string" ||
        payload.token.length < 40 ||
        typeof payload.sessionId !== "string" ||
        !/^[A-Za-z0-9_-]{8,64}$/.test(payload.sessionId)
      ) return null;
      return { url: url.toString(), token: payload.token, sessionId: payload.sessionId };
    } catch (_error) {
      return null;
    }
  }

  function parseInviteUrl(urlValue) {
    try {
      const url = new URL(urlValue, window.location.href);
      return parseInviteFragment(url.hash.slice(1));
    } catch (_error) {
      return null;
    }
  }

  function idempotencyKey() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  async function postJson(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    let payload = {};
    try { payload = await response.json(); } catch (_error) { payload = {}; }
    return { response, payload };
  }

  async function sendCommand(command) {
    if (!room) throw new Error("room not connected");
    await room.localParticipant.publishData(
      encoder.encode(JSON.stringify(command)),
      { reliable: true, topic: CONTROL_TOPIC },
    );
  }

  function finishRemoteCall(status) {
    stopMicrophone();
    invite = null;
    mediaReady = false;
    mediaReadyResolve = null;
    if (room) room.disconnect().catch(() => {});
    room = null;
    terminal = !paired;
    setStatus(status);
  }

  function onStatus(payload) {
    let event;
    try { event = JSON.parse(decoder.decode(payload)); } catch (_error) { return; }
    if (!event || event.type !== "status" || typeof event.status !== "string") return;
    if (event.event === "dtmf_failed") {
      setStatus("connected", t("dtmf_failed"));
      return;
    }
    if (event.status === "media_ready") {
      mediaReady = true;
      if (mediaReadyResolve) mediaReadyResolve();
    }
    if (event.status === "ended" || event.status === "failed") {
      finishRemoteCall(event.status);
      return;
    }
    setStatus(event.status);
  }

  function waitForMediaReady(timeoutMs) {
    if (mediaReady) return Promise.resolve();
    return new Promise((resolve, reject) => {
      mediaReadyResolve = resolve;
      window.setTimeout(() => {
        if (!mediaReady) reject(new Error("media timeout"));
      }, timeoutMs);
    });
  }

  async function requestInvite() {
    const { response, payload } = await postJson("/api/session", {});
    if (!response.ok || !payload.invite || typeof payload.invite.url !== "string") {
      throw new Error(payload.error || "session unavailable");
    }
    const parsed = parseInviteUrl(payload.invite.url);
    if (!parsed) throw new Error("invalid session");
    return parsed;
  }

  async function connectAndDial() {
    const number = numberInput.value.trim();
    if (!NUMBER_RE.test(number)) {
      setStatus("idle", t("invalid_number"));
      numberInput.focus();
      return;
    }
    if (!window.LivekitClient) {
      terminal = !paired;
      setStatus("failed", t("connection_failed"));
      return;
    }

    setStatus("connecting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
      microphoneTrack = stream.getAudioTracks()[0];
      if (!microphoneTrack) throw new Error("microphone track unavailable");
      if (!invite) invite = await requestInvite();

      const { Room, RoomEvent, Track } = window.LivekitClient;
      room = new Room({ adaptiveStream: false, dynacast: false });
      room.on(RoomEvent.TrackSubscribed, (track) => {
        if (track.kind !== Track.Kind.Audio) return;
        const audio = track.attach();
        audio.autoplay = true;
        audio.playsInline = true;
        audioHost.replaceChildren(audio);
      });
      room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
        if (topic === STATUS_TOPIC) onStatus(payload);
      });
      room.on(RoomEvent.Disconnected, () => {
        if (["ended", "failed"].includes(callState)) return;
        finishRemoteCall("failed");
      });

      await room.connect(invite.url, invite.token);
      await room.localParticipant.publishTrack(microphoneTrack, {
        name: "phone-mic",
        source: Track.Source.Microphone,
      });
      await room.startAudio();
      await waitForMediaReady(12000);
      setStatus("dialing");
      await sendCommand({ type: "dial", number, idempotency_key: idempotencyKey() });
    } catch (error) {
      const denied = error && (error.name === "NotAllowedError" || error.name === "PermissionDeniedError");
      finishRemoteCall("failed");
      setStatus("failed", denied ? t("microphone_denied") : t("connection_failed"));
    }
  }

  async function hangup() {
    hangupButton.disabled = true;
    try {
      await sendCommand({ type: "hangup" });
    } catch (_error) {
      finishRemoteCall("ended");
    }
  }

  async function pairPhone() {
    const code = pairingCode.value.trim().toUpperCase();
    const name = deviceName.value.trim();
    if (!PAIR_CODE_RE.test(code) || !name || name.length > 64) {
      setStatus("failed", t("invalid_pair_code"));
      return;
    }
    pairButton.disabled = true;
    setStatus("pairing");
    try {
      const { response, payload } = await postJson("/api/pair", { code, display_name: name });
      if (!response.ok || !payload.device) {
        setStatus("failed", payload.error || t("pair_failed"));
        return;
      }
      paired = true;
      terminal = false;
      showDialer(payload.device);
      setStatus("paired", t("ready"));
    } catch (_error) {
      setStatus("failed", t("connection_failed"));
    } finally {
      pairButton.disabled = false;
    }
  }

  async function refreshDevice() {
    try {
      const response = await fetch("/api/device", { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || !payload.paired) return false;
      paired = true;
      terminal = false;
      showDialer(payload.device);
      if (!payload.edge || !payload.edge.enabled) setStatus("failed", t("edge_disabled"));
      else if (!payload.edge.configured) setStatus("failed", t("edge_unconfigured"));
      else setStatus("paired", t("ready"));
      return true;
    } catch (_error) {
      return false;
    }
  }

  async function unpairPhone() {
    unpairButton.disabled = true;
    try { await postJson("/api/unpair", {}); } catch (_error) { /* clear local view below */ }
    paired = false;
    terminal = false;
    invite = null;
    showPairing();
    setStatus("idle", t("ready"));
    unpairButton.disabled = false;
  }

  callButton.addEventListener("click", connectAndDial);
  numberInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") connectAndDial();
  });
  hangupButton.addEventListener("click", hangup);
  pairButton.addEventListener("click", pairPhone);
  pairingCode.addEventListener("keydown", (event) => {
    if (event.key === "Enter") pairPhone();
  });
  unpairButton.addEventListener("click", unpairPhone);
  keypadButtons.forEach((button) => {
    button.addEventListener("click", async () => {
      if (callState !== "connected") return;
      try { await sendCommand({ type: "dtmf", digits: button.dataset.digit }); }
      catch (_error) { setStatus("connected", t("dtmf_failed")); }
    });
  });
  window.addEventListener("pagehide", () => {
    if (room && !terminal) {
      room.localParticipant.publishData(
        encoder.encode(JSON.stringify({ type: "hangup" })),
        { reliable: true, topic: CONTROL_TOPIC },
      ).catch(() => {});
    }
    stopMicrophone();
  });

  async function initialize() {
    deviceName.value = t("default_device_name");
    const fragment = takeFragment();
    invite = parseInviteFragment(fragment);
    if (invite) {
      terminal = false;
      showDialer(null);
      setStatus("idle", t("ready"));
      return;
    }
    if (fragment.startsWith("pair=")) pairingCode.value = fragment.slice(5).toUpperCase();
    if (await refreshDevice()) return;
    showPairing();
    setStatus("idle", t("ready"));
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/remote_dialer_sw.js?v=2", {
      updateViaCache: "none",
    }).catch(() => {});
  }
  initialize();
})();
