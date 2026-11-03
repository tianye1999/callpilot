# Task P: Windows 第二阶段

本地任务记录，关联用户任务 P；未创建远端 issue 或评论。
状态：308331a 已由机主推送；本轮 CI 修正与本地回归完成，Windows 再验 / Claude 审查待完成。
本轮修正未提交、推送或安装。

## 运行约定

- 同一安装目录中的 CallPilot.exe 默认运行托盘；`--service` 为后端，
  `--window` 为按需面板，`--shutdown` 为安装器停止协议。
- Windows 通过现有 tray_app.py 入口分发；Mac 的入口、selftest 与 launchd 分支不变。
- 后端默认监听 127.0.0.1:47100。
- 默认用户根目录 `%LOCALAPPDATA%\CallPilot`，`.env` 在根目录，业务数据在
  `data`，日志在 `logs`；延续第一阶段的 config.py 约定和路径覆盖环境变量。
- 首次启动从冻结资源 `.env.example` 创建 `.env`，不覆盖既有配置。
  Windows 首次生成时删除模板的 MODEM_PORT / MODEM_AUDIO_MODE / MODEM_PCM_PORT
  三项覆盖（模板写死 Mac 路径与音频模式），让已有平台/厂商默认和 PCM 自动配对生效。
- 每个角色持有一个进程生命周期文件锁；重复托盘/面板启动转为打开请求。
- 后端退出后托盘最多自动重试五次，退避 1/2/4/8/16 秒；耗尽后保留托盘，
  菜单手动重启可以重置预算。退出托盘请求停止窗口与后端，不强杀。
- `--shutdown` 发送停止文件，最多等待 45 秒直到三类锁全部释放；失败返回非零。
  锁避免 PID 复用误杀；未响应的进程应由用户排查，安装器不覆盖运行文件。

## 安装器

- Inno Setup，当前用户、无需管理员，默认 `%LOCALAPPDATA%\Programs\CallPilot`。
- 可选登录自启使用 HKCU Run，值为带引号的绝对 EXE 路径和 `--login`。
  不创建高权限计划任务，不依赖源码目录或 venv；不承诺登录前启动。
- 升级继承安装目录和任务选择；PrepareToInstall 调用旧 EXE 的停止协议。
  关闭 Restart Manager 自动结束/重启功能，超时中止，不强杀。
- 卸载同样先停止；清除安装文件、快捷方式与自启值，保留用户目录并提示手动删除。
- 第一阶段薄窗口没有停止协议，旧源码计划任务也不由本安装器管理；迁移前须手动
  关闭旧窗口并注销开发版任务，不把旧入口可执行成功误当作完整交接。

## CI 验收

- 版本唯一来源 pyproject.toml，经 ISCC `/DAppVersion` 传入。
- PyInstaller 后先做含空格目录重定位 selftest，再编 setup.exe。
- 静默当前用户安装到临时含空格目录；检查 HKCU Run 和开始菜单快捷方式。
- 已安装 EXE 做 selftest；以 `--service --hardware-selftest` 启动 HTTP 后端，
  轮询真实 `/api/meta` 校验版本；shutdown 后等正常退出。
- 安全模式不加载用户 .env，不构造 CallAgentService，不启动任何模组/音频/云连接，
  禁用 meta 的 USB 扫描；使用真实 build_app 路由而非另造版本端点。
  此模式不证明正常硬件服务链路可用。
- 再次启动安全后端，覆盖安装检查优雅退出、自启取消、配置哈希不变；静默卸载
  检查程序目录消失，用户配置哈希不变。
- ZIP 和 setup.exe 并存，上传安装后 selftest JSON；目标通常 4-8 分钟，job 上限
  15 分钟，实际耗时必须由首次 CI 证明。

## 审查与剩余门槛

### 308331a 的 CI 修正

已只读核对 CI run 41732750940，未触发 workflow 或修改远端。
Windows 3.12/3.13 超时栈均指向
`test_early_window_spawn_failure_does_not_prevent_tray_startup`。
原 Windows 分发只判断 win32，绕过测试的 Mac 胶水桩而进入真实 pystray。
现在仅 win32 + frozen 分发；windows_runtime 明确不支持源码入口，副作用前报错，
源码后端继续使用 app.py。新增非冻结不分发、冻结分发和 runtime 拒绝源码回归；
原托盘测试加禁止未打桩 Windows runtime 的护栏。

卸载检查原来把进程退出码等同于全部删除完成，但 Inno 官方说明临时 clone
可能仍在清理原始 uninstaller。检查改为最多等 30 秒，所有检查进程显式使用安装目录
之外的 cwd；失败打印残留路径/大小/属性，并上传单独的 uninstall.log。
旧 CI 没有残留清单，无法追溯具体残留文件，所以这里不声称已证明它只有 unins000.exe，
也不添加无证据的递归 UninstallDelete。实际清理结果仍待下一次 Windows CI。
Windows frozen selftest 的默认报告移到用户目录，导入前 cwd 也切到用户目录，
防止无报告覆盖参数或依赖的相对路径写入污染安装目录。

meta 成功与失败路径均取消完整 JSON 输出，只打印 responded/versionMatches 布尔值。
selftest 报告改为结构化默认值、导入模块名列表与异常类型，不保存原始 stdout/stderr
或 traceback/异常消息；用含令牌哨兵的成功和失败路径回归证明不进入报告。
该失败 CI run 的 artifacts 列表为空（原上传步骤未执行），没有可下载的旧 Windows
artifact 可扫描；新的 ZIP/setup 内 selftest JSON 与 installer-selftest.json 均来自同一
受限报告生成器。未删除或更改已公开的旧 CI 日志。

依据：[Inno Uninstaller Exit Codes](https://jrsoftware.org/ishelp/topic_uninstexitcodes.htm)。

本轮修正最终验证（下方旧结果为第一轮历史记录）：

```text
.venv/bin/pytest -q --timeout 600
4182 passed, 3 skipped, 1816 warnings in 314.60s (0:05:14)
build/venv312/bin/python -m pytest -q
4182 passed, 3 skipped, 1816 warnings in 218.95s (0:03:38)
.venv/bin/ruff check .
All checks passed!
.venv/bin/mypy
Success: no issues found in 87 source files
.venv/bin/mypy --platform win32
Success: no issues found in 87 source files
actionlint
exit 0, no output
```

2026-11-04 本地最终验证末尾原文：

```text
.venv/bin/pytest -q --timeout 600
4177 passed, 3 skipped, 1816 warnings in 285.34s (0:04:45)
build/venv312/bin/python -m pytest -q
4177 passed, 3 skipped, 1816 warnings in 218.30s (0:03:38)
.venv/bin/ruff check .
All checks passed!
.venv/bin/mypy
Success: no issues found in 87 source files
.venv/bin/mypy --platform win32
Success: no issues found in 87 source files
actionlint
exit 0, no output
```

本机无 pwsh，PowerShell Parser 两项跳过；未执行任何 Windows 安装或应用服务进程。
安全模式单测用真实 meta handler，但用假 Runner/Site，绝不在本机监听应用端口。

Claude 审查停止竞态、锁持有期、预算、Inno 编译及 PowerShell 的含空格路径。
推送后由 CI 证明 Windows 动态依赖、ISCC、安装/覆盖/卸载和后端版本；本机不运行
安装脚本或服务。Windows 托盘菜单、窗口聚焦、登录启动、真实模组和无模组普通启动
仍需真机验收；签名/SmartScreen 信任和证书由机主决定。

参考：[Inno Registry](https://jrsoftware.org/ishelp/topic_registrysection.htm)、
[CloseApplications](https://jrsoftware.org/ishelp/topic_setup_closeapplications.htm)、
[pystray usage](https://pystray.readthedocs.io/en/latest/usage.html)。
