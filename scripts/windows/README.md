# Windows 部署

> **状态：待硬件验证。** 本目录下的脚本与流程未在 Windows 真机 + EC20 上跑过
> （开发机为 macOS），代码路径按官方文档与跨平台约定编写。遇到问题欢迎提 issue 反馈。

Windows 比 macOS 简单：Quectel 有官方 Windows 驱动，装完后 AT 口直接以
`COMx` 串口暴露，**不需要** macOS 那样的 USB→PTY 桥；音频（EC20 UAC 声卡）
走 WASAPI，PortAudio 直接可用，也不需要 ffmpeg。

## 三步部署

1. **装 Quectel 官方驱动**
   前往 Quectel 官网（<https://www.quectel.com/download-center>）下载
   *EC20 Windows USB Driver* 并安装。插上模组后，设备管理器应出现若干
   `Quectel USB ...` 串口（含 AT Port）与一个 USB 音频设备。

2. **装 Python 依赖**（Python 3.12+）

   ```powershell
   git clone https://github.com/tianye1999/callpilot.git callpilot; cd callpilot
   python -m venv .venv
   .venv\Scripts\pip install -e ".[dev]"
   copy .env.example .env   # 然后编辑 .env 填入 DASHSCOPE_API_KEY 等
   ```

   `.env` 里 `MODEM_PORT` 设为 `auto`（Windows 上的默认值即 auto）：启动时
   按 Quectel VID 自动扫描 COM 口，无需手查设备管理器；`MODEM_AUDIO_MODE`
   默认 `uac`。

3. **注册开机常驻**（登录自启 + 失败自动重启，日志在 `data\app_console.log`）

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 install
   ```

   其余子命令：`uninstall` / `status` / `restart`。

装完后浏览器访问 <http://127.0.0.1:47100>。

## 可选：桌面壳打包

```powershell
.venv\Scripts\pip install pyinstaller pywebview
powershell -ExecutionPolicy Bypass -File scripts\windows\build_app.ps1
# 产物：dist\CallPilot\CallPilot.exe
```
