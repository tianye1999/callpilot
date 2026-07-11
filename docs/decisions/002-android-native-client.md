# ADR-002: Android 原生客户端先行，复用 #31 远程拨号协议

## Status

Accepted for issue #36

## Date

2026-07-11

## Context

#30（Mobile 远程话机桥 Epic）原假设「MVP 先做 iOS 17+」。实际评估：iOS 真机交付依赖
付费开发者账号（$99/年）、完整 Xcode 与 GUI 签名流程，PushKit/CallKit 均绑定付费账号；
而本项目开发机具备完整 Android 工具链（SDK/adb/Java 21），APK 可免费侧载真机。
经 owner 确认，调整为 **Android 先行**，iOS 推迟到协议与产品形态被 Android 版验证之后。

#31 的浏览器 Web Dialer（codex 实现中）已确立协议：LiveKit 房间承载媒体与控制信令，
Edge 只向外连接；配对与会话签发走独立最小权限网关。原生客户端完整复用该协议
（契约见 [`docs/remote-protocol.md`](../remote-protocol.md)），不新增 Edge 协议面。

原生相对 PWA 的增量价值（做 App 的理由）：

1. **听筒通话**：浏览器无法路由到听筒，只能外放——这是「像打电话」的关键体验差距；
2. 锁屏/切后台不断流（#31 自列限制：页面必须保持前台）；
3. 系统音频焦点、蓝牙耳机路由；
4. 为 #30 锁屏来电铺路（ConnectionService + FCM 只有原生能做）。

## Decision

- **单仓 monorepo**：新增顶层 `android/` Gradle 工程，不另开仓库。协议与客户端同步演进，
  POC 阶段分仓必然漂移；CI 用 path filter 隔离（`android/**` 才触发 Android job）。
- **技术栈**：Kotlin + Jetpack Compose + LiveKit Android SDK，minSdk 26。不用
  Flutter/RN——#30 的核心价值在深度电话集成（ConnectionService/CallKit），跨平台框架
  恰恰在这一层最受限；iOS 后续单独用 Swift 原生实现同一协议。
- **模块划分**（`android/app/src/main/kotlin/ai/bondings/callpilot/`）：
  - `pairing/`：配对码/深链解析、`/api/pair`、凭证存 EncryptedSharedPreferences；
  - `protocol/`：网关 HTTP 客户端 + LiveKit data-packet 信令编解码，独立 adapter，
    便于跟随 #31 协议演进与离线单测；
  - `media/`：LiveKit Room 封装、音频路由（听筒/扬声器/蓝牙）；
  - `call/`：通话前台服务与生命周期状态机（断线 grace、幂等收尾），预留
    ConnectionService 接入位；
  - `ui/`：Compose 三页（配对 / 拨号 / 通话）。
- **v0 范围**：仅出站拨号（与 #31 对等）。不做 FCM、锁屏来电、AI 接管、iOS。
- **开发隔离**：独立 git worktree + 分支 `feat/android-app-poc`；不改 `src/agentcall/**`
  任何文件，Edge 侧需求以 issue 提给 #31。
- **CI**：`.github/workflows/android.yml`（assembleDebug + lint + JVM 单测），
  path-filtered，不拖累 Python 三平台矩阵。

## Consequences

- 协议基线钉在 #31 分支 commit 上，该分支 merge 前存在漂移风险；由契约文档 +
  adapter 层 + merge 后集成联调消化。
- 仓库引入第二语言工具链；通过 path filter 与目录边界把认知/CI 成本限制在 `android/`。
- 真机验收依赖实体 Android 设备（已确认可用）与 LiveKit 凭证（`.env` 的
  `LIVEKIT_URL/API_KEY/API_SECRET`，待填）；外呼验收一律只拨 10000。
