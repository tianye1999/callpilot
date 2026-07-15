# CallPilot iOS App

#30 的 iOS 实现切片(见 issue #96)。复用 hosted `/v1` 控制面协议(#42/#95),
Edge/cloud 侧无需改动;Android(`../android`)是同协议的活参照实现。

## 现状(Phase 1 前台版,进行中)

已写、待完整 Xcode 环境验证/续建:

- `CallPilot/Protocol/Models.swift` — 平台无关模型(对齐 Android `protocol/Models.kt`)
- `CallPilot/Protocol/HostedCloudClient.swift` — `/v1` 适配器:配对 / 线路状态 /
  inbound-offers 轮询 / claim(对齐 Android `HostedCloudClient.kt`)
- `CallPilot/Call/CallState.swift` — 通话状态机(对齐 Android `CallManager.CallState`)
- `project.yml` — xcodegen 工程声明
- `CallPilot/Info.plist` — 前台版权限(麦克风)

待续建:配对/拨号/来电接听卡/通话页(SwiftUI)、LiveKit 媒体会话、前台轮询接管。
CallKit/PushKit(锁屏系统来电)属 Phase 2。

## 环境前置(硬阻塞)

**必须安装完整 Xcode**(当前机器只有 Command Line Tools,`swiftc` 因 CLT 的 SDK
modulemap 冲突连 `import Foundation` 都编不过;无 iOS SDK / 模拟器 / xcodebuild)。

```bash
# 1. App Store 安装 Xcode(~15GB),然后切换活动开发者目录:
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -version           # 验证

# 2. 装 xcodegen(声明式生成 .xcodeproj,不手写):
brew install xcodegen

# 3. 生成工程(在 ios/ 下):
export DEVELOPMENT_TEAM=<你的 Apple Team ID>   # 本机签名 Team(见 Xcode > Settings > Accounts)
cd ios && xcodegen generate

# 4. 首件事:验证平台无关协议层已就绪(此前被 CLT 挡住):
xcodebuild -project CallPilot.xcodeproj -scheme CallPilot \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' build

# 5. 打开工程续建 UI:
open CallPilot.xcodeproj
```

## 验收(对齐 Android 两幕)

- 模拟器:配对 + 拨号 UI + 外呼媒体建立流程
- 真机 iPhone(iOS 17+):外呼一通 10086 双向通话;前台接管一通来电
- 远程关闭时不影响本地/Android 现有行为
