# KinyAI SIM Assistant iOS App

#30 的 iOS 实现切片(见 issue #96)。复用 hosted `/v1` 控制面协议(#42/#95),
Edge/cloud 侧无需改动;Android(`../android`)是同协议的活参照实现。

## 当前配置

- 主屏名称: `KinyAI SIM`; App 内中文名称: `KinyAI-SIM助手`。
- 版本: `0.7.5 (6)`。Bundle ID `ai.bondings.callpilot.sim`（实体 SIM 线独立的
  App Store Connect 记录，不与 `ai.bondings.callpilot` 共用）；target/module/product
  `CallPilot` 保持不变。
- 新配对默认中转: `https://sim-relay.bondings.ai`。不会覆盖已保存配对的网关。
- 配对输入兼容 Android: `https://<网关>/remote_dialer.html#pair=XXXX-XXXX`
  或裸配对码。链接只取 HTTPS origin 和 fragment 配对码;手动网关仍可编辑。
- 已有配对、拨号、短信、通话记录、设置、LiveKit 媒体及 CallKit/PushKit 来电。
- 来电 offer 文案不假设 AI 已接听,同时适用于电脑端先响手机和转接场景。

## 生成工程

需要完整 Xcode、iOS SDK 和 XcodeGen,仅 Command Line Tools 不够。
工程由 `project.yml` 生成;`.xcodeproj` 已被 Git 忽略,不要手改或强行加入仓库。

```bash
cd ios
xcodebuild -version
xcodegen --version
# 保留占位符,不要将本机 Team ID 注入生成文件或 project.yml。
DEVELOPMENT_TEAM='${DEVELOPMENT_TEAM}' xcodegen generate
```

签名构建时由本机在 xcodebuild 命令行传入 `DEVELOPMENT_TEAM`,不将实际值入库。
下面的验证禁用签名,不需要证书、钥匙串或真机。

## 本地验证

```bash
# 编译 App 和完整 XCTest 包,不启动 App、不读取配对钥匙串。
xcodebuild -project CallPilot.xcodeproj -scheme CallPilot \
  -configuration Debug -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .validation/DerivedData \
  CODE_SIGNING_ALLOWED=NO DEVELOPMENT_TEAM= build-for-testing

# Release iOS 编译,不签名、不安装、不归档上传。
xcodebuild -project CallPilot.xcodeproj -scheme CallPilot \
  -configuration Release -sdk iphoneos -destination 'generic/platform=iOS' \
  -derivedDataPath .validation/DerivedData \
  CODE_SIGNING_ALLOWED=NO DEVELOPMENT_TEAM= build

# macOS 独立 XCTest runner:不需要模拟器 runtime,不启动 App/PushKit/Keychain。
python3 run-core-tests.py
CORE_TEST_CONFIGURATION=Release python3 run-core-tests.py
```

独立 runner 使用同一份协议、解析器、状态及内容模型源码和 XCTest 文件,
涵盖中英本地化、显示名、配对链接、推送请求字段及 CallKit 源码配置检查。
只编译显式列出的纯逻辑源码,不包含 `AppModel` 或 `CredentialStore`。
HTTP 测试使用现有 URLProtocol mock;测试缓存使用临时目录。
资源及构建产物位于被忽略的 `.validation/`。文本生成和读取显式使用 UTF-8。

它不等同于 iOS 全量运行验收:媒体会话/LiveKit、SwiftUI 行布局和 Tab 壳测试
仅随 `build-for-testing` 编译,不在独立 runner 中运行。需要完整 App 托管测试时,
可在允许读取模拟器配对钥匙串的环境使用相同 scheme 的 `xcodebuild test`,
并指定已安装 runtime 的具体模拟器 destination。缺 runtime 时不得把仅编译当作测试通过。

## 推送环境核对

- `CallKitCoordinator` 在 `#if DEBUG` 下选择 `sandbox`,否则 `production`。
  XcodeGen 的 Debug 配置定义 `DEBUG`,Release 不定义;TestFlight 应使用 Release。
- `HostedCloudClient` 使用设备 Cookie 和 Origin,以 `PUT /v1/device/push-token`
  上报 `{token, environment}`;解除注册为相同路径的 `DELETE`。
  与 `cloud/src/index.ts` 路由和 `cloud/src/schemas.ts` 当前定义一致。
- `CXProviderConfiguration()` 使用系统默认 App 名称,`remoteHandle` 为 `KinyAI SIM`。
- `aps-environment` 源 entitlement 仍为 `development`;实际分发签名 entitlement
  属于签名/导出阶段验证,无签名编译不能证明 TestFlight APNs 投递成功。

## 待授权运行验收

- iPhone 配对链接粘贴、手动网关兜底、中英界面及大字体布局。
- 真实 SIM 外呼、来电先响手机及 AI 转接的双向音频。
- 分发签名产物的 APNs entitlement、TestFlight 注册及锁屏 VoIP 来电投递。
