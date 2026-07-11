# 远程拨号协议契约（Edge ↔ 远程客户端）

Web Dialer（#31）与 Android App（#36）共同遵守的协议描述。**基线：`feat/issue-31-web-dialer`
@ `8003677`**；该分支仍在演进，协议变动以 codex 确认为准，变动后同步更新本文档。

## 总体架构

```
远程客户端（手机浏览器 / Android App）
   │  ①HTTPS：配对 + 建会话（远程网关，经 Cloudflare Tunnel 暴露）
   │  ②WSS：LiveKit 房间（媒体 + 控制信令都在房间里）
   ▼
LiveKit（云或自建）
   ▲
   │  Edge 只向外连接，不开任何入站端口
AgentCall Edge ──AT/PCM── EC20 Dongle ──PSTN── 对端
```

- 管理端口（`WEB_PORT` 47100）**绝不**暴露公网；对外只有独立最小权限网关
  （`REMOTE_GATEWAY_PORT`，默认 47445）。
- 总开关 `REMOTE_WEB_DIALER_ENABLED=false` 默认关闭。

## 一、远程网关 HTTP API

所有写接口要求 `Origin` 头与配置的 `public_origin` 精确相等（constant-time 比较）。
**原生客户端注意：HTTP 库默认不发 Origin，必须手动设置，否则 403。**

### GET `/api/device`

设备/线路状态。凭 Cookie 鉴别是否已配对：

```jsonc
// 未配对
{"ok": true, "paired": false, "edge": {"enabled": bool, "configured": bool}}
// 已配对
{"ok": true, "paired": true, "device": {...}, "edge": {/* remote_dialer_status() 全量 */}}
```

### POST `/api/pair`　`{"code": "XXXX-XXXX", "display_name": "..."}`

- 配对码：8 位（展示为 `XXXX-XXXX`），TTL `REMOTE_PAIRING_TTL_SECONDS`（默认 300s），一次性。
- 成功：`{"ok": true, "paired": true, "device": {...}}` + `Set-Cookie`（见下）。
- 失败：401 配对码无效/过期；409 超设备数上限（`REMOTE_MAX_PAIRED_DEVICES`=5）；429 频控。

凭证 Cookie：

```
__Host-callpilot-device=<device_id>.<secret>   ; Secure; HttpOnly; SameSite=Strict; Max-Age=180天
```

服务端只存 secret 的 hash。**原生客户端以同名 Cookie 头携带凭证**（凭证存
EncryptedSharedPreferences），后续鉴权接口同理。

### POST `/api/session`　`{}`

已配对设备创建一次性拨号会话：

```jsonc
{"ok": true, "invite": {"session_id": "...", "url": "...", "expires_at": 1234567890.0}}
```

- 401 未配对/已撤销；409 线路占用（一 SIM 一通）。
- `invite.url` = `REMOTE_CONTROL_URL#<fragment>`，fragment 为 **base64url（无填充）** 的
  compact JSON：

```json
{"v": 1, "url": "<livekit_url>", "token": "<browser JWT>", "sessionId": "..."}
```

- 邀请 TTL `REMOTE_INVITE_TTL_SECONDS`（默认 300s，允许 30-900）；单邀请只允许一个客户端
  身份加入、至多一次拨号尝试。
- `#pair=CODE` 形式的 fragment 是配对码深链（与邀请 fragment 互斥）。

### POST `/api/unpair`

撤销当前设备配对。

## 二、LiveKit 房间信令（data packets，reliable）

| topic | 方向 | 消息 |
|---|---|---|
| `callpilot.control` | 客户端 → Edge | `{"type":"dial","number":"...","idempotency_key":"..."}`；`{"type":"hangup"}`；`{"type":"dtmf","digits":"..."}` |
| `callpilot.status` | Edge → 客户端 | `{"type":"status","status":"<字符串>"}`；`{"type":"remote_call","status":"dialing"\|"connected"\|...}` |

- 号码格式：`\+?[0-9*#]{1,32}`；DTMF：`[0-9*#]{1,16}`。
- 控制包经 topic、发送者身份、大小、schema、状态五重校验；重复 `dial`（同 idempotency
  key）不重复 ATD。
- 身份命名：浏览器 `web-*`、Edge `edge-*`（原生拟用 `app-*`，待 #31 确认）。

## 三、媒体与安全语义

- **media-ready 前不 ATD**：Edge 在预期客户端身份发布音轨前不执行拨号；ATD 前重复校验。
- 音频：Edge/Dongle 侧恒为 8 kHz s16 mono PCM；WebRTC 侧由 LiveKit 在 programmatic
  participant 边界重采样；双向有界队列，拥塞丢最旧帧。
- DTMF 默认 `REMOTE_DTMF_MODE=qvts`（模组 QVTS；`both` 时叠加带内双音）。
- 断线：媒体中断超 `REMOTE_DISCONNECT_GRACE_SECONDS`（默认 5s）→ Edge 自动挂断物理线路。
- 单通上限 `REMOTE_OUTBOUND_MAX_SECONDS`（默认 1800s）；拨号频控
  `REMOTE_DIAL_LIMIT_PER_HOUR`（默认 10）。
- 所有终止路径幂等（客户端挂断 / 对端挂断 / 媒体异常 / Edge 退出）。
- 日志与事件不含 LiveKit token、API 凭证、完整被叫号码（本地通话记录保留号码作审计）。

## 四、原生客户端（#36）现状适配与待 #31 方确认的需求

**现状即可工作的适配**（无需 Edge 改动）：

1. Cookie 鉴权：原生手动携带 `__Host-callpilot-device` Cookie 头；
2. Origin 校验：原生请求手动设置 `Origin: <public_origin>`；
3. LiveKit 连接信息：请求 `/api/session` 后解析 `invite.url` 的 fragment。

**提请 #31 侧评估的需求**（记录于对应 issue，非阻塞）：

1. 支持 `Authorization: Bearer <device_id>.<secret>` 作为 Cookie 的等价鉴权（原生更自然）；
2. 预留/允许 `app-*` 身份类别，审计可区分 web 与原生；
3. `/api/session` 响应直接返回结构化 `{livekit_url, token, session_id, expires_at}`
   字段（原生免解 fragment；fragment 形式保留给 web）；
4. `remote_dialer_status()` 的字段清单文档化（App 首页线路状态依赖它）。
