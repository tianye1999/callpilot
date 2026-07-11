# 远程拨号协议契约（Edge ↔ 远程客户端）

Web Dialer（#31）与 Android App（#36）共同遵守的协议描述。**基线：已合入 `main`（#33）**；
其后 #39/#40 仅为客户端缓存/交付修复，无协议变更。协议如有变动，确认后同步更新本文档。

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
// 未配对（网关瘦身响应，只含两个布尔）
{"ok": true, "paired": false, "edge": {"enabled": bool, "configured": bool}}
// 已配对：edge = remote_dialer_status() 全量
{"ok": true, "paired": true, "device": {...}, "edge": {
  "enabled": bool, "cloud_enabled": bool, "configured": bool,
  "missing": ["缺失的必需配置键"], "active": bool,
  // ↓ coordinator 附加字段：仅在存在远程会话 worker 时出现，无 worker 时整组消失
  "session_id": "...", "edge_ready": bool, "browser_connected": bool,
  "media_ready": bool, "call_active": bool, "expires_at": 1234567890.0,
  "status": "<最近一次 status 事件值，初始 starting>"
}}
```

`browser_connected` 为当前兼容命名——原生客户端同样以此字段判媒体端连接（#37 结论）。

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
| `callpilot.status` | Edge → 客户端 | `{"type":"status","status":"<字符串>"}`（如 `media_ready`/`dialing`/`connected`/`ended`/`failed`，可带 `reason`/`code`） |

- 号码格式：`\+?[0-9*#]{1,32}`；DTMF：`[0-9*#]{1,16}`。
- 控制包经 topic、发送者身份、大小、schema、状态五重校验；重复 `dial`（同 idempotency
  key）不重复 ATD。
- `type=remote_call` 是 Edge 本地 EventHub/审计事件，**不经 data channel 下发**（#37 裁决），
  客户端只需消费 `type=status`。
- 身份命名：浏览器 `web-*`、Edge `edge-*`；`app-*` 原生身份类别按 #37 审计结论 **defer**
  （原生沿用 `web-*` 兼容身份，暂不新增类别）。

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
3. LiveKit 连接信息：请求 `/api/session` 后解析 `invite.url` 的 fragment；
4. 网关地址免手填：配对链接 `{REMOTE_CONTROL_URL}#pair=CODE` 的 origin 即
   `public_origin`（静态页与 /api 接口同源），原生端粘贴链接即可同时获得
   网关地址与配对码。

**提请 #31 侧评估的需求**（记录于对应 issue，非阻塞）：

1. 支持 `Authorization: Bearer <device_id>.<secret>` 作为 Cookie 的等价鉴权（原生更自然）；
2. 预留/允许 `app-*` 身份类别，审计可区分 web 与原生（按 #37 审计结论 **defer**，见 §二）；
3. `/api/session` 响应直接返回结构化 `{livekit_url, token, session_id, expires_at}`
   字段（原生免解 fragment；fragment 形式保留给 web）；
4. ~~`remote_dialer_status()` 的字段清单文档化~~（已落地：见 §一 `GET /api/device` 字段说明）。

## 五、Hosted 云控制面 `/v1`

当 Edge 启用 `REMOTE_CLOUD_ENABLED=true` 时，Android App 按配对来源改走 hosted
adapter；原有 Tunnel `/api/*` adapter 与已保存凭证保持不变。Hosted 的完整契约以
[`remote-cloud-protocol.md`](remote-cloud-protocol.md) 为 SSOT，本文只记录原生端差异：

- `POST /v1/pairing-sessions/claim` 使用 camelCase 请求字段，并从
  `__Host-callpilot-device` 的 `Set-Cookie` 响应中提取长期设备凭证；原生端后续手动发送
  同名 `Cookie` 与控制面 origin 对应的 `Origin`。
- 配对结果额外保存 `edgeId` 和协议标记 `hosted`；未包含协议标记的旧存储数据一律按
  `tunnel` 读取，避免升级后丢失已有配对。
- `POST /v1/calls` 携带 `edgeId` 与每次呼叫唯一的 `idempotencyKey`，随后轮询
  `GET /v1/calls/{callId}`；仅在响应的 `session` 中取得结构化 `livekitUrl`、`token`
  和 `expiresAt` 后连接 LiveKit，不解析 URL fragment。
- LiveKit 的 `callpilot.control` / `callpilot.status` topic 和 `media_ready` 后才发送
  `dial` 的门控语义与 Tunnel 共用，不因控制面协议改变。
- 信令边界：通话中 `dial`/`dtmf`/`hangup` 与 `status` 事件经 **LiveKit data packets
  在参与者间直传**；云 Durable Object 只承载 Edge WSS（`session.start` 下发与状态/ACK
  上报），不中转房间信令或媒体。
- Hosted 与 Tunnel 配对链接都可能使用根路径，App 不按 URL 形态猜协议。自动模式先向同一
  origin 提交 hosted claim；仅当 `/v1/pairing-sessions/claim` 返回 404/405 时回退
  `/api/pair`。业务错误不回退，避免重复提交已被服务端处理的配对码；界面仍保留显式协议选择兜底。
