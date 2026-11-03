# CallPilot Windows

状态：SIM7600G 驱动、真实 COM 口与双向音频均**待硬件验证**。打包与无界面自检由 CI 验证。
第二阶段代码已加入随包后端、托盘与 Inno Setup 安装器，Windows CI 首次验收待完成。
不提供 MSI、签名或自动安装驱动，不携带 API 密钥与模型资产。

## 成品安装与升级

CI artifact 同时包含 `CallPilot-<version>-setup.exe`、便携 ZIP 和安装后自检 JSON。
setup 按当前用户安装到 `%LOCALAPPDATA%\Programs\CallPilot`，无需管理员。
可选登录自启写入 HKCU Run；默认启动托盘，托盘管理 `--service` 后端并按需启动
`--window` 面板，关闭面板不会停止服务。自动重启最多五次，退避 1/2/4/8/16 秒。

配置在 `%LOCALAPPDATA%\CallPilot\.env`，数据与日志分别在该目录的 `data` 与 `logs`。
首次配置来自包内 `.env.example`，移除三个 Mac 端口/音频覆盖项，让 Windows 默认值生效；
已有用户配置不覆盖。SIM7600 用户仍须按下文选择厂商并填写 AI 凭证。
升级/卸载先通过旧 EXE 的 `--shutdown` 优雅停止；超时中止，不强杀。
卸载保留用户数据，并提示可手动删除。旧源码计划任务不自动迁移，须先手动注销，
第一阶段薄前端也须先关闭，避免与成品后端争用端口。

完整协议与 CI 验收范围见 [任务 P](../../docs/task-p-windows-installer.md)。

## SIM7600G 驱动与端口

从 [SIMCom 官方产品/下载入口](https://www.simcom.com/product/SIM7600.html)
取得与模组及 Windows 版本匹配的 SIM7600 USB 驱动，必要时向供应商索取官方签名驱动。
不要用 Quectel 驱动替代。设备管理器的“端口 (COM 和 LPT)”应列出类似：

| 描述 | 用途 |
| --- | --- |
| SimTech HS-USB AT Port 9001 (COMx) | AT 控制 |
| SimTech HS-USB Audio 9001 (COMy) | PCM-over-USB 串口，非 UAC 声卡 |
| SimTech HS-USB NMEA 9001 | GNSS，不用作 SIM7600 PCM |
| SimTech HS-USB Diagnostics 9001 | 诊断，不用作 AT |

依据：[SIMCom USB AUDIO Application Note V1.03，第 8–9 页](https://simcom.ee/documents/SIM7X00/SIM7100_SIM7500_SIM7600%20Series_USB%20AUDIO_Application%20Note_V1.03.pdf)。
文档说明 `AT+CPCMREG=1` 启用 Audio 端口 PCM，`=0` 停止，默认 PCM 为 8 kHz、16-bit linear。
自动识别复用项目 SIM7600 预设 VID `0x1E0E` / 工作 PID `0x9001`，
按 pyserial 的 `description` 或 `interface` 中独立 AT / Audio / PCM 词识别角色。
`interface` 是描述，不是数值接口号；描述缺失、多个同角色或 PID 不符时不猜 COM 序号。
`0x9011` 是项目 alternate/recovery PID，不当本表的正常音频工作态；其他 PID 需实际枚举证据后扩表。
[pyserial 字段与跨平台限制](https://pyserial.readthedocs.io/en/latest/tools.html#serial.tools.list_ports.ListPortInfo)。

## 源码后端与常驻

安装 Python 3.12 x64，创建环境并填写 `.env`：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

SIM7600G 关键配置如下。替换模板里遗留的 Quectel/UAC 设置，保留实际 AI 提供方凭证：

```dotenv
MODEM_VENDOR=simcom
MODEM_PORT=auto
MODEM_AUDIO_MODE=nmea
MODEM_PCM_PORT=auto
MODEM_PCM_BAUD=921600
```

PCM 也可以不设（删除该行），随 auto AT 扫描 Audio。显式空值保留为空值。
支持 `MODEM_VENDOR=auto`；同时插 Quectel 与 SIMCom 时沿用预设顺序，Quectel 优先。
SIMCom 预设在未显式配置音频模式时默认 `nmea`。固定端口须同时配置
`MODEM_PORT=COMx`、`MODEM_PCM_PORT=COMy`。Windows 不使用 `/tmp/sim7600-*`、PTY 桥或其波特率回退。
这里 `nmea` 是兼容的代码模式名，不表示把 SIM7600 的 NMEA 口当 Audio 口。

在目标 Windows 上注册源码后端任务（**install 会立即启动服务**）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 install
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 status
```

`restart` 重启，`uninstall` 停止并移除任务；它不是 exe 安装器。
任务登录自启、失败重启；源码日志在 `data\app_console.log`，面板为 http://127.0.0.1:47100。
冻结版可写数据默认在 `%LOCALAPPDATA%\CallPilot`；源码后端 `.env` 仍在项目根目录。

## CI 与本地打包

`windows-package` 仅在 main push 和 PR 自动运行，无手动触发入口。
GitHub Actions → CI → 对应成功 run → Artifacts → `CallPilot-windows-x64`，
下载解压 artifact，再解压其中的 `CallPilot-windows-x64.zip`，保留整个 CallPilot 目录，不能只拷 exe。
包内 `selftest.json` 是重定位自检报告，artifact 的 `installer-selftest.json` 是安装后报告；
安装验收还检查安全后端版本、覆盖升级及卸载保留数据，不证明 GUI、常驻或真实通话已验收。
目标机仍需 Microsoft Edge WebView2 Runtime 与 .NET Framework（pywebview WinForms 要求）。

本地复现（只在 Windows 构建 Windows 产物）：

```powershell
.venv\Scripts\python -m pip install "pyinstaller>=6.11,<7" "pyinstaller-hooks-contrib>=2025.1" "pillow>=11,<13" "pythonnet>=3,<4"
.venv\Scripts\python scripts/windows/generate_icon.py
powershell -ExecutionPolicy Bypass -File scripts\windows\build_app.ps1
$env:CALLPILOT_SELFTEST_REPORT = Join-Path $PWD 'selftest.json'
$p = Start-Process dist\CallPilot\CallPilot.exe -ArgumentList '--selftest' -PassThru
if (-not $p.WaitForExit(60000)) { $p.Kill(); throw 'Selftest timeout' }
if ($p.ExitCode -ne 0) { throw 'Selftest failed' }
Get-Content selftest.json
```

图标由已跟踪 `packaging/CallPilot.icns` 经 Pillow 转换，脚本生成
16/24/32/48/64/128/256 多尺寸 `packaging/CallPilot.ico`，同版 Pillow 可复现。
自检导入关键模块、加载配置注册表和解析默认值，加载 CLR/WinForms/WebView2 程序集，
不读用户 `.env`、不枚举/打开串口、不创建窗口、不探测/启动服务；CI 将副本移到含空格路径并限时 60 秒。

## EC20 附带说明

EC20/EG25 使用 Quectel 官方驱动，`MODEM_VENDOR=quectel`、`MODEM_PORT=auto`、
`MODEM_AUDIO_MODE=uac`。保留原有 Quectel VID 与 AT 描述/第三串口排序回退行为。
不要把这组 UAC 配置套到 SIM7600G。

## 下一阶段验收

- CI 首次真构建、迁移目录启动与 GUI WebView2 页面加载。
- 设备管理器/pyserial 实际 VID、PID、描述、AT/Audio 对照及热插拔。
- SIM7600G 来去电、PCM 双向录音、长通话、挂断/重连恢复（需另行授权硬件操作）。
- 计划任务登录自启、退出/重启、路径含中文与空格。
- MSI/安装器、内置后端、升级/卸载/回滚；代码签名由机主决定证书与发布策略。
