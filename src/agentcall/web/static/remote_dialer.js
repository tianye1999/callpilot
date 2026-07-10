(() => {
  "use strict";

  if (window.top !== window.self) {
    document.documentElement.textContent = "";
    return;
  }

  const CONTROL_TOPIC = "callpilot.control";
  const STATUS_TOPIC = "callpilot.status";
  const NUMBER_RE = /^\+?[0-9*#]{1,32}$/;
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
      connection_failed: "Could not connect",
      dtmf_failed: "Key was not sent",
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
      connection_failed: "连接失败",
      dtmf_failed: "按键发送失败",
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
  const numberInput = $("dialNumber");
  const callButton = $("callButton");
  const hangupButton = $("hangupButton");
  const keypadButtons = Array.from(document.querySelectorAll("#keypad button"));
  const audioHost = $("audioHost");

  let invite = null;
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
    if (status === "idle" || status === "media_ready") tone = "ready";
    if (status === "connected") tone = "connected";
    if (status === "failed") tone = "error";
    if (status === "ended") tone = "ready";
    statusPill.dataset.state = tone;
    const known = t(status);
    statusText.textContent = detail || (known === status ? t("connection_failed") : known);

    const connected = status === "connected";
    const busy = ["connecting", "waiting_for_phone", "media_ready", "dialing", "connected"].includes(status);
    numberInput.disabled = busy;
    callButton.disabled = busy || terminal || !invite;
    hangupButton.disabled = !["connecting", "media_ready", "dialing", "connected"].includes(status);
    keypadButtons.forEach((button) => { button.disabled = !connected; });
  }

  function stopMicrophone() {
    if (microphoneTrack) microphoneTrack.stop();
    microphoneTrack = null;
  }

  function parseInvite() {
    const fragment = window.location.hash.slice(1);
    history.replaceState(null, "", window.location.pathname + window.location.search);
    if (!fragment || fragment.length > 8192) return null;
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
      ) {
        return null;
      }
      return {
        url: url.toString(),
        token: payload.token,
        sessionId: payload.sessionId,
      };
    } catch (_error) {
      return null;
    }
  }

  function idempotencyKey() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  async function sendCommand(command) {
    if (!room) throw new Error("room not connected");
    await room.localParticipant.publishData(
      encoder.encode(JSON.stringify(command)),
      { reliable: true, topic: CONTROL_TOPIC },
    );
  }

  function onStatus(payload) {
    let event;
    try {
      event = JSON.parse(decoder.decode(payload));
    } catch (_error) {
      return;
    }
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
      terminal = true;
      stopMicrophone();
      if (room) room.disconnect().catch(() => {});
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

  async function connectAndDial() {
    const number = numberInput.value.trim();
    if (!NUMBER_RE.test(number)) {
      setStatus("idle", t("invalid_number"));
      numberInput.focus();
      return;
    }
    if (!invite || !window.LivekitClient) {
      terminal = true;
      setStatus("failed", t("invalid_invite"));
      return;
    }

    setStatus("connecting");
    try {
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
        if (!terminal) {
          terminal = true;
          setStatus("failed", t("connection_failed"));
        }
      });

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
      microphoneTrack = stream.getAudioTracks()[0];
      if (!microphoneTrack) throw new Error("microphone track unavailable");
      await room.connect(invite.url, invite.token);
      await room.localParticipant.publishTrack(microphoneTrack, {
        name: "phone-mic",
        source: Track.Source.Microphone,
      });
      await room.startAudio();
      await waitForMediaReady(12000);
      setStatus("dialing");
      await sendCommand({
        type: "dial",
        number,
        idempotency_key: idempotencyKey(),
      });
    } catch (error) {
      terminal = true;
      const denied = error && (error.name === "NotAllowedError" || error.name === "PermissionDeniedError");
      setStatus("failed", denied ? t("microphone_denied") : t("connection_failed"));
      stopMicrophone();
      if (room) await room.disconnect().catch(() => {});
    }
  }

  async function hangup() {
    hangupButton.disabled = true;
    try {
      await sendCommand({ type: "hangup" });
    } catch (_error) {
      terminal = true;
      setStatus("ended");
      if (room) await room.disconnect().catch(() => {});
    }
  }

  callButton.addEventListener("click", connectAndDial);
  numberInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") connectAndDial();
  });
  hangupButton.addEventListener("click", hangup);
  keypadButtons.forEach((button) => {
    button.addEventListener("click", async () => {
      if (callState !== "connected") return;
      try {
        await sendCommand({ type: "dtmf", digits: button.dataset.digit });
      } catch (_error) {
        setStatus("connected", t("dtmf_failed"));
      }
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

  invite = parseInvite();
  if (invite) {
    setStatus("idle", t("ready"));
  } else {
    terminal = true;
    setStatus("failed", t("invalid_invite"));
  }
})();
