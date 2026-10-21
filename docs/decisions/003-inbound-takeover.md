# ADR-003: 来电 AI 到手机真人接管采用有围栏的两阶段交接

## Status

Accepted for issue #95 Phase A (2026-07-15)

## Context

#95 是 #30 的 Android-first 前台 discovery slice。现有 `CallSession` 与
`RemoteWebDialerCoordinator` 各自拥有整通电话;前者 shutdown 会挂断 modem,后者
假设自己执行 ATD 并创建 CallRecord,因此不能直接拼接来接管一通已接通的来电。

接管默认关闭。Owner 偏好是带 revision 的自由文本,不是代码类别表:真实偏好要求
快递、外卖也转接,只由 AI 处理营销/骚扰电话。模型只能请求接管,Edge 的确定性
开关和状态 gate 才有切流权;来电者不能修改 owner policy。

## Decision

每通物理电话只有一个权威 CallCoordinator 和一个长生命周期媒体 owner。Phase A
的 `InboundTakeoverCoordinator` 是纯离线状态核心,通过注入的 FakeMediaRouter 和
clock 验证语义;它不连接 modem、LiveKit 或 Cloud。

| 当前状态 | 事件 | 下一状态 | 媒体 owner |
| --- | --- | --- | --- |
| `AI_ACTIVE` | policy 允许请求 | `TAKEOVER_PREPARING` | `AI` |
| `TAKEOVER_PREPARING` | offer 发出、固定垫话完成 | `WAITING_OWNER` | `HOLD` |
| `WAITING_OWNER` | Edge 本地确认手机媒体就绪 | `MOBILE_MEDIA_READY` | `HOLD` |
| `MOBILE_MEDIA_READY` | Edge commit | `MOBILE_ACTIVE` | `MOBILE` |
| `MOBILE_ACTIVE` | 手机媒体断开 | `MOBILE_RECONNECTING` | `HOLD` |
| `MOBILE_RECONNECTING` | 同一 claim 恢复 | `MOBILE_ACTIVE` | `MOBILE` |
| 任一 commit 前状态 | 拒接、超时或准备失败 | `AI_ACTIVE` | `AI` |
| `MOBILE_RECONNECTING` | grace 到期 | `ENDED` | `NONE` |
| 任一非终态 | 物理通话结束 | `ENDED` | `NONE` |

未列出的迁移全部 fail closed。commit 前保留旧 AI transport 但冻结普通 audio、tool
和 timer,失败时恢复 AI;commit 时推进 generation、清空旧 TTS、detach agent 并
原子交给手机。commit 后不得静默回 AI;断网超时产生有序
`NOTICE_THEN_HANGUP` terminal effect,真实提示 PCM 资产与 chaos 验收后补。

### Fencing contract

`inbound.offer` 最少包含:

```text
offer_id, nonce, call_id, generation, target_device_id, expires_at
```

Cloud first-claim-wins 原子生成 `claim_id`;Edge 仍只接受第一个有效 claim。claim 后
的 session/media/DTMF/hangup/reconnect 命令携带
`call_id + generation + claim_id`,设备身份从 authenticated channel 取得而非信任
body 自报。Edge 逐项校验当前 call、generation、唯一且未过期的 offer、nonce、目标
设备、winning claim 和允许状态。只有完全相同的 tuple 可幂等重放;stale、loser、
重复 offer ID 或变异重试不得产生任何 side effect。`MOBILE_MEDIA_READY` 只能是
Edge 本地同时观察到 App mic 已订阅和 Edge 下行 track 已发布的事实。

### Media and D5 boundary

MediaRouter 保证任一时刻只有一个 modem writer,切换时丢弃旧 generation 的 PCM。
它不负责 policy、Cloud claim、CallRecord finalization、ATH 或 QPCMV 生命周期。
AI 到 Mobile 过渡及物理通话仍存续的 rollback/reconnect 期间不得发送 `ATH` 或
`AT+QPCMV=0`;只有真正 `ENDED` 后才正常挂断并关闭语音通道。为此生产接线必须把
`detach_agent()` 与会挂断物理线路的 `_shutdown_agent()` 分开。

Android 首先实现前台在线 App;iOS 后续复用状态机、fencing、LiveKit/MediaRouter
契约和验收矩阵,另接 PushKit/CallKit。完整号码、偏好原文、录音、转写和模型
reasoning 不上 Cloud;一通电话只保留一个 CallRecord 和一个 finalizer。

## Consequences

Phase B 必须先拆分 agent 与物理通话生命周期,不能只增加一个工具。LiveKit 房间仅
在真实接管请求后创建。Phase A 单测只证明迁移、stale fence、双 claim 单胜和超时
决策;D5、真实音频 gap、提示后挂断和网络 chaos 仍需后续集成/真机验收。

References: #95, #30, #22, [ADR-001](001-remote-web-dialer-livekit.md),
[Android ADR](002-android-native-client.md), [remote protocol](../remote-protocol.md).

## Physical SIM Fallback (#131)

Applies only to `product/physical-sim` and its feature branches, never `main`.

- This implementation is Edge-only and requires no shared-cloud changes or new
  messages. Before either triage or the agent tool requests takeover, Edge checks
  `REMOTE_WEB_DIALER_ENABLED` and `REMOTE_CLOUD_ENABLED`, then reads the existing
  `CloudCredentialStore`. Both switches must be enabled and the local credential
  must parse as a valid Edge registration credential. Missing, malformed, or
  unreadable credentials reject with `NO_TAKEOVER_ENDPOINT` without creating an
  offer or changing AI media ownership. Events distinguish `edge_not_registered`
  from `hosted_takeover_disabled`.
- Credential I/O runs in a read-only daemon worker outside the takeover lock;
  both entries wait at most one second (`local_registration_timeout` fails closed),
  and triage waits off-loop. The result is
  applied only after rechecking the active call generation. A late credential
  read cannot create an offer after timeout or against a replacement call.
- Registration is deliberately only a local indication that a phone **might**
  take over. It does not prove a phone is paired, online, or push-reachable, nor
  that the server still accepts the credential. A registered Edge with no phone,
  an offline App, or an unavailable control connection uses the 20-second fallback.
  No presence query, heartbeat extension, or cloud deployment is required.
- The current inbound offer/claim path is hosted-only. Local gateway pairing
  enables remote outbound calling but has no inbound offer discovery/claim route,
  so its device records alone do not qualify as a takeover endpoint. This does
  not add a second, nonfunctional takeover path.
- One successfully queued takeover attempt is allowed per physical call. A
  preflight rejection does not consume the attempt. After fallback, triage stops
  making irreversible decisions; the AI continues intent/message handling rather
  than repeatedly transferring or rejecting the same caller.
- `INBOUND_TAKEOVER_TIMEOUT_SECONDS` defaults to 20 on every platform; invalid or
  non-positive values use 20. The monotonic budget starts at the request and covers
  hold generation, offer/claim waiting, and media preparation through commit.
  A claim is not a completed handoff. Successful mobile commit keeps the existing
  single-writer/media lifecycle and post-commit reconnect behavior.
- Timeout or another precommit failure uses one finalizer: restore AI ownership,
  clear request/queued claims, revoke the offer, release the triage fence, cancel
  old generation output, update provider instructions, and request a natural
  explanation plus continued intent/message handling. Hold and recovery prompts
  describe the situation, not fixed phrases. Provider awaits are bounded; actual
  audio arrival still depends on provider and physical-link health.
- `takeover_unavailable` carries reason/code/trigger. `takeover_timeout_reclaimed`
  carries `OWNER_TIMEOUT`, generation, and observed `elapsed_ms` from request to
  ownership restoration. Metrics schema v5 adds `takeover_unavailable_reasons`
  and `takeover_reclaim_latency_ms`; absent duration is null with a reason.

This change does not deploy shared infrastructure, pair devices, register tokens,
or exercise real AT. Real-device acceptance must check an unregistered new Edge,
audible hold/recovery on a registered Edge whose App is offline or unanswered,
and normal mobile answer before the budget against the unchanged cloud.
