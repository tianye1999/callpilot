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
      app_name: "Phone App",
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
      microphone_missing: "No microphone found on this device",
      microphone_busy: "The microphone is in use by another app",
      microphone_unavailable: "This device's microphone is unavailable",
      pairing_replace: "New pairing code detected. Pair to bind this phone to it.",
      dtmf_failed: "Key was not sent",
      pair_title: "Pair this phone",
      pair_code_label: "Pairing code",
      device_name_label: "Device name",
      pair: "Pair phone",
      pairing: "Pairing",
      paired: "Paired",
      pair_failed: "Pairing failed",
      invalid_pair_code: "Enter the code shown by KinyAI SIM Assistant",
      unpair: "Unpair",
      default_device_name: "My phone",
      edge_disabled: "Remote dialing is disabled",
      edge_unconfigured: "Remote dialing is not configured",
    },
    zh: {
      app_name: "手机 App",
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
      microphone_missing: "本机没有可用的麦克风",
      microphone_busy: "麦克风被其它应用占用",
      microphone_unavailable: "本机麦克风不可用",
      pairing_replace: "检测到新的配对码，可用它重新配对本机。",
      dtmf_failed: "按键发送失败",
      pair_title: "配对这台手机",
      pair_code_label: "配对码",
      device_name_label: "设备名称",
      pair: "配对手机",
      pairing: "正在配对",
      paired: "已配对",
      pair_failed: "配对失败",
      invalid_pair_code: "请输入 KinyAI-SIM助手电脑端显示的配对码",
      unpair: "解除配对",
      default_device_name: "我的手机",
      edge_disabled: "电脑端未启用远程拨号",
      edge_unconfigured: "电脑端远程拨号配置不完整",
    },
  };

  const language = navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
  const t = (key) => text[language][key] || text.en[key] || key;
  document.title = t("app_name");
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

  function showPairingReplacePrompt() {
    // 已绑定旧设备、但用户手上有新配对码：给出配对界面，而不是默默沿用
    // 旧绑定（#98.3）。配对码已经预填好，点一下即可。
    //
    // 文案刻意不说「替换」：服务端 /api/pair 是**新增**一台设备并换一个
    // cookie，并不会吊销旧绑定，设备数满时还会直接被拒（Codex 评审 P2）。
    // 说了做不到的事比不说更糟。
    showPairing();
    setStatus("idle", t("pairing_replace"));
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

  // 本机允许的媒体 host（来自 /api/device）。null = 还没取到 → 一律不接受
  // fragment 邀请。宁可临时链接连不上（可见故障），也不放行任意端点。
  let allowedMediaHost = null;

  async function loadAllowedMediaHost() {
    try {
      const response = await fetch("/api/device", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      const host = payload && payload.edge && payload.edge.media_host;
      if (typeof host === "string" && host) allowedMediaHost = host;
    } catch (_error) {
      // 取不到就维持 null —— fail-closed。
    }
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
        // 端点必须是干净的 host：内嵌凭证 / 空 host 都不接受，与云侧
        // csp.ts 的 liveKitConnectSources() 同口径。
        !url.hostname ||
        url.username ||
        url.password ||
        // exact-host 白名单：端点只能是本机配置的那一个 LiveKit host。
        // 这是 #41 的正解 —— fragment 由攻击者可控，光校验协议挡不住把
        // 媒体导到别人服务器。CSP connect-src 是浏览器强制的第二道。
        !allowedMediaHost ||
        url.host !== allowedMediaHost ||
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

  function localMediaErrorKey(error) {
    // 只认 getUserMedia 的标准错误名，不猜 message 文本（本地化会变）。
    const name = error && error.name;
    if (name === "NotAllowedError" || name === "PermissionDeniedError") {
      return "microphone_denied";
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError") {
      return "microphone_missing";
    }
    if (name === "NotReadableError" || name === "TrackStartError") {
      return "microphone_busy";
    }
    if (name === "OverconstrainedError" || name === "SecurityError") {
      return "microphone_unavailable";
    }
    // 没拿到麦克风轨也属于本机问题，不是 Edge 不可达。
    if (error && error.message === "microphone track unavailable") {
      return "microphone_unavailable";
    }
    return "";
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
    // 本机媒体单独一个 try：localMediaErrorKey 只能覆盖 getUserMedia 这一段。
    // 否则 SecurityError 会撞车——WebSocket 构造函数在被拦截/不安全端口时也抛它，
    // 真正的 LiveKit 连接失败就会被显示成「麦克风不可用」（Codex 评审 P1）。
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
      microphoneTrack = stream.getAudioTracks()[0];
      if (!microphoneTrack) throw new Error("microphone track unavailable");
    } catch (error) {
      finishRemoteCall("failed");
      setStatus("failed", t(localMediaErrorKey(error) || "microphone_unavailable"));
      return;
    }

    try {
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
    } catch (_error) {
      // 走到这里就确实是 Edge / LiveKit 侧的问题——本机媒体故障已在上面
      // 那个 try 里分流掉了（#98.4）。
      finishRemoteCall("failed");
      setStatus("failed", t("connection_failed"));
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
    // 临时链接（fragment 邀请）仍是在售功能：面板「临时链接」按钮 +
    // remote_dialer.py 仍在签发 URL#<payload>。所以不能删入口，改为按
    // exact-host 白名单校验（#41 / G-04 给的两条路里的第二条）。
    // 白名单必须先从本机取回来，取不到就不接受 fragment —— fail-closed。
    await loadAllowedMediaHost();
    if (fragment && !fragment.startsWith("pair=")) {
      invite = parseInviteFragment(fragment);
      if (invite) {
        terminal = false;
        showDialer(null);
        setStatus("idle", t("ready"));
        return;
      }
    }
    const pairCode = fragment.startsWith("pair=")
      ? fragment.slice(5).toUpperCase()
      : "";
    if (pairCode) pairingCode.value = pairCode;
    // 带了新配对码就先给配对界面，别被旧 cookie 短路（#98.3）。
    // 浏览器留着上一台 Edge 的 __Host-callpilot-device 时，refreshDevice() 会
    // 直接 return，新配对码永远消费不掉，页面还一直显示 Edge 不可用——用户
    // 明明刚扫了新码，却完全没有出路。
    if (pairCode) {
      const bound = await refreshDevice();
      if (bound) {
        // 提示语自己会设状态，别再用 ready 把它盖掉。
        showPairingReplacePrompt();
      } else {
        showPairing();
        setStatus("idle", t("ready"));
      }
      return;
    }
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
