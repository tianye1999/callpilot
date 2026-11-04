# CallPilot Windows

状态：SIM7600G 驱动、真实 COM 口与双向音频均**待硬件验证**。打包与无界面自检由 CI 验证。
Windows 只发**绿色版（便携 ZIP）**：不签名、不上微软商店、不做 setup.exe。用户下载 zip、
解压后双击 `CallPilot.exe` 即用。

## 绿色版安装与使用

CI artifact 只含 `CallPilot-windows-x64.zip`。所有用户数据（`.env`、`data`、`logs`）固定落在
exe 同目录的 `data-root`，不写 `%LOCALAPPDATA%\CallPilot`，也不读任何标记文件。

1. 下载 `CallPilot-windows-x64.zip`，右键文件 → 属性 → 勾选「解除锁定」（若存在该选项）。
2. 解压到可写目录（如桌面或 `D:\CallPilot`，路径可含空格与中文）。
3. 双击 `CallPilot.exe`。首次启动生成 `data-root\.env`，接着按需要填写 AI 凭证与调制解调器厂商。
4. 托盘右键 →「开机自动启动」写入/删除 HKCU Run；状态实时读注册表。若 exe 被移动后 Run 仍指向
   旧路径，托盘会提示，并可在菜单点「开机自启指向旧路径，点击修正」一键修正。
   首次启动向导默认勾选「开机自动启动（推荐）」，完成向导即写入 HKCU Run，与托盘开关同源、状态一致。

只读位置（如光盘、只读共享）无法落数据时，托盘会弹窗报错并退出，不会静默改写到别处。
默认启动托盘，托盘管理 `--service` 后端并按需启动 `--window` 面板，关闭面板不停止服务；
后端自动重启最多五次，退避 1/2/4/8/16 秒。

### 升级（保留配置）

1. 右键托盘 →「Quit CallPilot」退出（后端与窗口一并停止）。
2. 用新版解压覆盖原程序文件，保留 `data-root` 目录不动。
3. 重新双击 `CallPilot.exe`。`data-root` 里的 `.env` 与数据保持不变。

### 卸载

1. 右键托盘 →「退出并关闭开机自启」（删除 HKCU Run 项），随后程序退出。
2. 删除整个 CallPilot 解压目录（含 `data-root`）。

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
绿色版可写数据固定落在 exe 旁 `data-root`；源码后端 `.env` 仍在项目根目录。

## CI 与本地打包

`windows-package` 仅在 main push 和 PR 自动运行，无手动触发入口。
GitHub Actions → CI → 对应成功 run → Artifacts → `CallPilot-windows-x64`，
下载解压 artifact，再解压其中的 `CallPilot-windows-x64.zip`，保留整个 CallPilot 目录，不能只拷 exe。
包内 `selftest.json` 是重定位自检报告；便携冒烟脚本 `test_portable.ps1` 解压到含空格临时目录后
启动后端、校验版本、优雅停机并确认三角色锁释放，再以同版本覆盖程序文件模拟升级并断言
`data-root\.env` 哈希不变，且 `%LOCALAPPDATA%\CallPilot` 未被创建。这不证明 GUI、常驻或真实通话已验收。
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
- 托盘开机自启开关与旧路径一键修正的真机行为（注册表写删、移动 exe 后提示）。
- 绿色版覆盖升级/删除目录卸载的真机验收；代码签名由机主决定证书与发布策略。
