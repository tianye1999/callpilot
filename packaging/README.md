# CallPilot macOS Packaging

实体卡线打包版要求 **Apple Silicon、macOS 13 或更高版本**。不再为 macOS 12
及以下安装旧式 LaunchAgents；`LSMinimumSystemVersion=13.0` 在启动前拒绝旧系统。
源码开发、Linux、Windows 不使用 SMAppService，原有开发启动方式不变。

## 构建与自检

```bash
# 仅供本地开发验证，不得作为内测发布包。
bash packaging/build_installer.sh

# Claude / 维护者本机执行；身份和凭证不写入仓库。
RELEASE=1 \
CODESIGN_IDENTITY="Developer ID Application: <Name> (<TEAMID>)" \
NOTARY_PROFILE="<keychain-profile>" \
bash packaging/build_installer.sh
```

产物：`dist/CallPilot.app`、`dist/CallPilot-26.10.2.dmg`。文件名版本动态读取
`pyproject.toml`，不是脚本内写死的版本号。`scripts/build_app.sh` 只产出本地 ad-hoc
签名 App，也运行包检查和 `--selftest`，不作为发布替代入口。

构建环境需 arm64 Python、PyInstaller、项目 `[local]` 依赖、
`pyobjc-framework-ServiceManagement`、`dmgbuild>=1.6,<2`，以及 arm64 ffmpeg/libusb。
默认脚本在需要时建立隔离打包 venv；指定 `PYTHON` 或复用项目 `.venv` 时，维护者须
先安装上述依赖。可通过 `FFMPEG_PATH`、`LIBUSB_PATH` 覆盖工具路径。
现有 `packaging/CallPilot.icns` 不受 Git 跟踪，干净 checkout 还需维护者本地提供该品牌图标。
所用 ffmpeg 构建的再分发许可由维护者确认，模型清单来自 `local_models.bundled_assets()`。

每次打包强制检查：

1. `packaging/verify_bundle.py` 核对包内 `CFBundleShortVersionString`、`CFBundleVersion`、
   包内 pyproject 与源码 pyproject 完全相同；核对全部默认模型及包内三个 agent plist。
2. 执行产物 `Contents/MacOS/CallPilot --selftest`，验证 frozen 模块、原生框架和模型，
   **不启动服务、不访问 USB、不注册登录项**；失败立即停止打包。
3. 签名模式执行 App 和 DMG 的 `codesign --verify`；App 带 hardened runtime 与音频权限。
4. 公证模式先把 App zip 提交公证、staple App，再将已 staple 的 App 放入 DMG，
   对 DMG 签名、公证、staple；最终验证两者的 staple 和 DMG Gatekeeper 结果。
5. 输出最终 DMG SHA256 和字节数，留作分发及回读比对。

`RELEASE=1` 缺少任一签名或公证变量立即失败。不设两个变量时仍允许本地未签名包，
日志明确标记签名/公证 **SKIPPED，不代表发布验收通过**。只设签名变量是已签名但未公证包。
公证仅使用 `notarytool --keychain-profile`，不接受仓库中的密码、证书或固定身份。
先在维护者本机用 `xcrun notarytool store-credentials <profile>` 配置；profile 不可用时
停止，不自动运行个人凭证恢复脚本。提交、公证和上传由 Claude 执行。

未签名包首次打开会被 Gatekeeper 拦截：right-click `CallPilot.app`，选择 **Open**。
此方式只供本地调试，不保证 SMAppService 在未签名包下通过系统授权。
签名内测包先从 DMG 拖入 Applications，再启动，不能从只读挂载卷运行卸载。

### DMG 布局

`dmg-background.png` 由 Codex 通过 `~/video-kit/tools/gen.py quick` 生成，包含
CallPilot、向右箭头和 Drag to Applications。构建将图缩放到 660×420，
`dmg-settings.py` 通过 dmgbuild 写入 Finder 背景、窗口尺寸、App/Applications 图标位置。
不依赖 Finder Apple Events 授权。原始生成过程文件在 gitignored `build/dmg-art/`，
发布前仍需挂载最终 DMG 实看箭头、图标、标签和拖拽目标。

## 后台项目与升级

三个固定 label 为 `com.agentcall.bridge`、`com.agentcall.app`、`com.agentcall.tray`。
plist 只放在 `CallPilot.app/Contents/Library/LaunchAgents/`，通过
`SMAppService.agentServiceWithPlistName_` 注册/注销；`BundleProgram` 指向包内 CallPilot，
不再以 `/usr/bin/caffeinate` 作为注册可执行文件。防睡眠改为进程内启动 caffeinate
观察自身 PID，系统项目的归属仍是 CallPilot。图标来自 App 的 `CFBundleIconFile`。

- 登录时只启动 tray；bridge/app 是已 bootstrap 但未运行的 job，tray 通过同一条
  WP6 reconcile 拉起。不能把这种 idle job 当成“未知状态的运行中服务”。
- `reconcile` 保留 `upgrade.lock`、`/api/updates/prepare|resume|check` 租约、忙碌等待、
  `force_restart_labels` / `before_restart` / `verify_bridge`。先注销/停 app，再停 bridge，
  确认旧 job 消失；注册并启动新桥，校验新版本及实例锁后，才注册/启动服务。
  系统查询异常、停止失败或桥版本验证失败均停止后续步骤，不混跑新旧版本。
- app/bridge 的 `KeepAlive=False`，崩溃及非零退出均由 tray 每 30 秒通过同一安全
  reconcile 恢复，避免 App 被覆盖后 launchd 单独拉起新版与另一旧进程混跑。
  自动恢复/登录遇到忙碌或租约争用直接返回，下轮再试；主动升级仍等待空闲。
  这样后台恢复线程不会一直等通话结束而挡住用户退出。托盘自身继续使用 `SuccessfulExit=False`。
  正常配置重启也由服务清理后通知托盘，不自行 exec 新包。恢复耗时为下次巡检加桥验证，
  不承诺即时恢复；覆盖 App 后须主动打开新版，后台巡检不擅自发起版本交接。
- 自启默认开，设置保存在 `startup.json`。开关只 enable/disable tray 下一次登录的
  launchd 调度，不注销正在运行的 app/bridge，不中断通话。主动打开临时 enable tray，
  安装完成后恢复关闭状态。包内 agent 仍归 SMAppService 管理。
- 需要系统批准时不绕过用户决定，启动失败页和窗口设置均有“登录项设置”，调用
  `SMAppService.openSystemSettingsLoginItems()`，批准后点重试。
- 首次迁移仅允许三个精确 label；校验旧 plist 的 Label，拒绝符号链接及错配文件，
  bootout 且确认任务不存在后才删除。扫描 `~/Library/LaunchAgents/` 和旧版关闭自启
  使用的 Application Support/CallPilot/LaunchAgents；不按 `com.agentcall.*` 通配删除。
  每次删除写日志，重复运行幂等。服务迁移仍要先通过通话/队列及租约保护。
- 旧托盘沿 WP6 `replace` 协议交接 singleton；同版本同路径也通过 `lifecycle` 标记
  区分，防止把安装权交回 WP6。来自旧 launchd job 的实例先用 Launch Services
  `open -n` 启动独立继任者并正常退出，继任者拿到锁后再清理旧 tray plist，避免自杀。
  不支持安全租约/交接的更旧版本明确提示用户空闲时退出再打开，不强杀迁移。

官方依据：Apple 的 [helper 迁移说明](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos)、
[SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice)、
[register](https://developer.apple.com/documentation/servicemanagement/smappservice/register())。
注册属于每个用户；多用户 Mac 必须分别验收。拖入废纸篓后系统的清理时机、名称与图标
必须用已签名产物实测，单元测试不能证明系统 UI 已清理或不留历史记录。

## 卸载

窗口设置提供 **卸载 CallPilot…**；菜单不再提供“卸载常驻”。普通浏览器不暴露这个
原生入口，pywebview 还校验当前页与受信本地服务同源。原生确认框包含
“同时删除通话录音、短信和设置（不可恢复）”，每次均默认不勾选；取消没有停止动作。

执行者是 Launch Services `open -n CallPilot.app --args --uninstall-worker` 创建的
独立实例，不是托盘、服务或托盘的普通子进程。执行顺序：

1. 原生确认；检查 App 标识与数据目录。通知 tray 暂停新安装，等待已开始的安装结束，
   然后按 WP6 顺序获取 upgrade/startup 锁。通话/队列忙、状态未知或租约不可得时拒绝卸载。
2. 持有升级租约，先注销并停止 app，再注销并停止 bridge，严格确认 job 已移除。
3. 正常关闭窗口并保存几何，要求 tray 按 `replace` 正常退出；持有真正的 tray singleton
   锁直到卸载结束，再注销 tray。清理残留的白名单旧 plist。
4. 使用 `NSFileManager.trashItemAtURL` 把 App 移到废纸篓，不使用 `rm -rf` 删除 App。
5. 只有明确勾选才永久删除标准 `~/Library/Application Support/CallPilot/` 中的录音、
   短信数据库和设置，以及 CallPilot 自己的两项云端 Keychain 凭证；依赖在移动 App 前加载。
   不追踪/删除外部路径的导出或自定义录音目录，拒绝被覆盖或符号链接重定向的顶层数据目录。
6. 独立实例结束；任何失败显示“卸载未完成”，保留错误日志，不把停止失败当成功。
   若移到废纸篓后可选清理失败，App 已移走但数据可能仍在，需按报错核查，不能宣称全部清理。

直接拖 App 到废纸篓不执行上述数据删除：后台注册由系统管理，默认保留用户资料。
这不是“删除一切数据”的承诺。菜单退出仍先停 app/bridge、关窗、再退出托盘；下次主动
打开重新注册。窗口开关、几何、通知及 WP6 更新入口沿用既有行为。

## 实体卡线内测发布

**本节是 Claude / 维护者的执行清单，本工作包不执行 tag、push、公证或上传。**

1. 确认发布提交在 `product/physical-sim` 上（本线代码永不合入 main），且 **该分支最新提交的
   GitHub Actions CI 全绿**，不是旧提交绿、不是只看本机。保留 SHA 和 CI run 链接；任何平台/Python 矩阵未通过都停止发布。
2. 本机五门：`pytest -q`、`ruff check .`、`mypy`、`mypy --platform win32`、
   `mypy --platform linux`，使用项目 `.venv/bin/`。完成下方签名真机清单。
3. 从 pyproject 读取版本，确认产物版本一致；经 owner 授权后在已验收提交上创建 annotated
   tag `sim-v<版本>`（本版 `sim-v26.10.2`），再由 Claude 推送 tag，禁止重打已有 tag。
4. 在维护者本机用上面的 `RELEASE=1` 命令构建签名、公证包。检查 `--selftest`、模型/版本、
   签名、App 与 DMG staple、SHA256 和大小的记录；挂载的 App 也要验签与 staple。
5. 上传至 **内部分发位置（见维护者本地说明）**，保留所有历史版本，不覆盖旧文件。
   公开仓库不填写具体平台、文件夹或内部链接。下载回读，核对 SHA256，确认权限可供员工使用。
6. 下载回读通过后，最后才写云端 `app_releases`。按本机维护者说明选择数据库；确保
   migration `0009_app_releases.sql` 已应用。使用本机生成、不入库的 SQL 文件填入真实下载
   URL，字段说明与模板如下（时间单位是 epoch 毫秒，SQL 字符串中的单引号要写成两个）：

```sql
INSERT INTO app_releases
  (product, platform, version, published_at, notes_zh, notes_en, download_url, created_at)
VALUES
  ('physical-sim', 'macos', '<版本>', <发布时间毫秒>,
   '<中文说明>', '<English notes>', '<本机填写已验收的 HTTPS 下载地址>', <创建时间毫秒>);
```

```bash
# DB_NAME、RELEASE_SQL 仅来自维护者本地说明；SQL 含内部 URL，不放入仓库。
cd cloud
npx wrangler d1 execute "$DB_NAME" --remote --file "$RELEASE_SQL"
npx wrangler d1 execute "$DB_NAME" --remote \
  --command "SELECT product, platform, version, published_at FROM app_releases WHERE product='physical-sim' AND platform='macos' ORDER BY published_at DESC LIMIT 3"
```

7. 回读 release API / 客户端“关于”和更新提示，确认版本、说明及跳转。只读检查通过才通知员工。
   重试遇到主键冲突先回读，禁止随手 `INSERT OR REPLACE`。撤回时经授权删除此次精确主键行，
   让上一条有效发布恢复为最新；历史 DMG 和 tag 保留，问题版本不得原地覆盖。

## Claude 真机验收

- 已签名公证 DMG 中的 App 拖到 Applications，Finder 背景、箭头、图标位置正确；离线首次打开。
- 系统通知与“系统设置 → 登录项”显示 **CallPilot 名称和图标**，而不是 caffeinate。
  首次批准、拒绝、再次批准及启动失败页跳转均实测。
- 登录冷启动桥先于服务；自启默认开，关掉不打断通话，注销/重新登录后不启动；手动打开可用，
  开启后重新登录可用。测试普通非零退出和崩溃恢复，不允许重复桥或服务。
- 从 WP6 同版本同路径覆盖升级，包含旧自启开/关两种目录；通话和队列忙时保持原版本，
  空闲后租约保护迁移，三个旧 plist 清理且有日志，重开不重建旧文件，其他 label 不受影响。
- **直接拖废纸篓**：观察后台服务、登录项以及通知记录何时由系统清理；等待并重新登录验证
  不复活，区分注册清理与系统保留的历史 UI 项。默认资料保留。此项未过不能声称“卸干净”。
- **设置卸载入口**：取消无变化，默认保留资料，勾选后不可恢复清理；确认空闲/忙碌/查询失败、
  窗口拒绝关闭、注销失败、移动失败都可见报错。独立执行者不能先被自己的 stop 杀掉。
- 卸载后等待超过恢复周期并重新登录：没有 bridge/app/tray，USB/端口释放，App 在废纸篓；
  默认保留资料后重装可恢复，选择清理后重装回到首次设置。多用户机器分别检查注册状态。
