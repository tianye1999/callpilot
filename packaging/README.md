# CallPilot macOS Packaging

Build the unsigned standalone installer on an Apple Silicon Mac:

```bash
bash packaging/build_installer.sh
```

Outputs:

- `dist/CallPilot.app`
- `dist/CallPilot.dmg`

The build bundles the PyInstaller Python runtime, CallPilot code, menu bar icons,
the build-machine `ffmpeg` executable, and `libusb-1.0.0.dylib`. Set
`FFMPEG_PATH` or `LIBUSB_PATH` to override discovery. The bundled `ffmpeg`
binary is redistributed under the license terms of the ffmpeg build you provide;
Homebrew builds install their license metadata under the Homebrew prefix.

Signing and notarization are optional:

```bash
CODESIGN_IDENTITY="Developer ID Application: Example (TEAMID)" \
NOTARY_PROFILE="notarytool-profile" \
bash packaging/build_installer.sh
```

If `CODESIGN_IDENTITY` is unset, the app and DMG are left unsigned.

## First Open

Unsigned builds are blocked by Gatekeeper on first launch. Install from the DMG,
then right-click `CallPilot.app` in `/Applications` and choose **Open**. Confirm
the prompt once; later launches can use the normal double-click/open flow.

On first run, the menu bar app writes per-user launchd agents to
`~/Library/LaunchAgents/` using the app's current install path. Runtime state,
logs, recordings, and `.env` live under:

```text
~/Library/Application Support/CallPilot/
```

Moving the app and opening it again regenerates the launchd plists for the new
bundle path. The menu contains **卸载常驻** to boot out and remove the background
launchd agents.

## macOS App Lifecycle (#140 / Package 2)

以下行为仅适用于打包版 macOS；源码运行及其他平台维持原有桌面启动方式。

- 主动打开先创建 `--window` 进程、本地“正在启动…”页面，再在后台安装/启动
  bridge、app、tray。窗口后台校验 `/api/meta` 的 CallPilot 字段，健康后导航到管理页，
  未完成设置时由现有网页向导接管。30 秒未就绪后收集诊断并显示失败页。
- `LSUIElement` 保持不变。只有窗口进程设置 `NSApplicationActivationPolicyRegular`，
  显示 Dock 图标并可 Cmd-Tab；红色关闭按钮只关闭窗口，菜单栏和电话服务继续运行。
  激活已有窗口使用 `NSRunningApplication`，不再使用 System Events，也不声明 Apple Events 权限。
- rumps delegate 的 `applicationShouldHandleReopen:hasVisibleWindows:` 与用户启动的
  第二实例都向 `shell-requests/open` 投递请求；`--login` 第二实例只让位，不开窗。
  菜单栏主线程消费合并请求并复用子进程；`window.lock` 再防止菜单栏重启后产生重复窗口。
- launchd 的 tray 命令带 `--login`。已完成设置的登录启动不弹窗，未完成设置仍开窗；
  主动打开始终开窗。健康探测与服务安装不阻塞菜单栏主线程。
- 菜单“退出 CallPilot”与窗口 ⌘Q 汇入同一个托盘确认流程。先根据 meta 提示当前通话，
  再明确提示“退出后将不再接打电话”。取消不发任何停止命令；确认依次 bootout app、bridge，
  验证任务不存在后正常关窗（保存几何），最后退出菜单栏。任务已移除，KeepAlive 不能拉回。
  停止/查询失败时保留菜单栏并报错，不把无法查询当成已停止。下次主动打开重新 bootstrap。
- 网页设置的“登录时自动启动 CallPilot”默认开启，存储于用户目录 `startup.json`，
  托盘和服务共用同一份文件。关闭仅移除 `~/Library/LaunchAgents/` 的三个 plist，
  不 bootout 当前任务、不打断通话；手动启动使用用户目录下 `LaunchAgents/` 的运行期 plist。
  开启则重新写入登录目录。设置与安装通过 `startup.lock` 跨进程互斥，避免写回竞态。
- 窗口几何保存至用户目录 `window.json`，使用 Cocoa 全局屏幕坐标和 `visibleFrame`，
  默认 1100×780；显示器移除或分辨率改变后尺寸与位置收回可见区域。
- 失败页“重试”请求托盘重新确保任务已加载；“复制诊断信息”只包含 App/macOS 版本、
  三个单元的有限状态字段、最近日志尾部的安全分类、meta 中硬件连接布尔值。
  **不复制原始日志、环境、provider 配置或完整 meta**，防止未知格式密钥泄漏。
  由于持久日志可能包含旧故障，原因表述为“最近日志记录了…”，不冒充本次确定结论。
- “卸载常驻”菜单的行为不变，本包不替换。

### 官方依据

- [NSApplication.setActivationPolicy](https://developer.apple.com/documentation/appkit/nsapplication/setactivationpolicy(_:))
  与 [regular](https://developer.apple.com/documentation/appkit/nsapplication/activationpolicy-swift.enum/regular)：
  运行时改变激活策略，regular 是可显示 Dock/UI 的普通应用。
- [NSRunningApplication.activate](https://developer.apple.com/documentation/appkit/nsrunningapplication/activate(options:))：
  激活指定应用，不需要通过 Apple Events 控制 System Events。
- [applicationShouldHandleReopen](https://developer.apple.com/documentation/appkit/nsapplicationdelegate/applicationshouldhandlereopen(_:hasvisiblewindows:))：
  接受应用的 reopen 事件。
- 本机 `man launchd.plist` 确认 `AssociatedBundleIdentifiers` 接受 string 或 string array；
  三个 plist 均写入 `["ai.bondings.callpilot"]`。
  [Apple legacy helper 迁移说明](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos)
  另外要求可执行文件的 Team Identifier 与 App 匹配，且 Launch Services 已注册 App。

### Claude 真机验收（本包未执行）

1. 用户双击/Spotlight/启动台冷启动至本地页是否在 1 秒内；健康、端口冲突、服务崩溃、
   USB 异常、重试及复制诊断；耗时必须实测，单元测试不证明 PyInstaller 冷启动速度。
2. Dock 图标与 Cmd-Tab，关窗后的窗口进程退出；窗口在后台/最小化时重复打开，
   reopen 与同时启动第二实例不重复开窗，不弹自动化授权。
3. 通话中/空闲/状态不可用的退出提示，取消完全不变；确认后 app/bridge 从 launchd
   移除、端口与 USB 释放，窗口和菜单栏退出，等待超过 KeepAlive 节流周期仍不复活。
4. 自启开/关分别注销登录；关闭自启后主动打开仍启动全部服务，退出仍全停；
   未完成设置的登录启动仍显示向导。验证多个显示器、负坐标、拔屏后的几何恢复。
5. 在原始 PCM 无读者场景重复停 app 再 SIGTERM bridge，检查无 `U` 态且实例锁可重新获取。
6. 系统登录项名称需用实际安装/签名产物验证。**现有 app/bridge 的首个可执行文件仍是
   `/usr/bin/caffeinate`**（本包保留该包装），其签名归属可能影响关联 App；仅新增 plist 键
   不能证明系统界面已显示 CallPilot。若实机仍显示 caffeinate，需另行确认包装调整方案。
