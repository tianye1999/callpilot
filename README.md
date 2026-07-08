# CallPilot

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Platform: macOS | Windows (beta)](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20(beta)-000000.svg?logo=apple&logoColor=white)](#hardware--platform-support)
[![Status: Developer Preview](https://img.shields.io/badge/Status-Developer_Preview-orange.svg)](docs/roadmap.md)

**Your calls, handled by AI.** An open-source AI phone agent that runs on a
Quectel EC20/EG25 4G modem: it auto-answers incoming calls and talks to the
caller with a realtime voice AI, places outbound calls, sends/receives SMS,
navigates IVR menus (DTMF), and records + summarizes every call — all on your
own hardware and API keys.

> **Status: Developer Preview (v0.2).** Runs on macOS with a Quectel EC20 today.
> This release targets developers who can read a README, install Python, provide
> an API key, and run commands. It is **not** a one-click app yet — the goal is
> reproducibility and hardware feedback. See [Roadmap](docs/roadmap.md).

[English](#english) · [中文](#中文)

---

## English

### What it is

CallPilot bridges a cellular modem to a cloud realtime voice model, so an AI
"assistant" answers and makes phone calls on your behalf:

```
Phone call → EC20 modem ──(AT: RING/ATA/CLCC)── CallPilot
                │ 8kHz PCM                         │
          Audio bridge ────── VoiceAgent (Qwen Omni / Doubao / OpenAI realtime)
                                    │
             EventHub → web dashboard (served as a desktop app or browser)
```

- **AI brain:** cloud realtime speech-to-speech (Alibaba **Qwen Omni** by default,
  ByteDance **Doubao** or **OpenAI Realtime** optional). No local ML models to install.
- **Telephony:** hardware AT events from an EC20/EG25 modem — clean `RING → ATA`,
  not screen automation.
- **Features:** auto-answer, outbound dialing (single + batch with whitelist),
  SMS send/receive (Chinese UCS2), AI tool-calling (send SMS / hang up / read OTP /
  **DTMF keypad**), per-call recording + latency metrics + LLM summary, live
  transcript, local speaker monitoring, bilingual (EN/中文) desktop UI.

### Hardware & platform support

| Item | Status |
|------|--------|
| Quectel EC20 (this build tested against `EC20CEFAGR08A03M4G`) | ✅ verified |
| macOS (Apple Silicon & Intel via Rosetta) | ✅ verified |
| Windows 10/11 (official Quectel driver, native COM port) | 🧪 full support implemented, **awaiting hardware reports** |
| Linux (native serial port) | ⚠️ code paths exist, **not verified** |
| Audio: `uac_ffmpeg` (ffmpeg via UAC sound card) | ✅ verified — **macOS only** |
| Audio: `uac` (PortAudio/WASAPI) | 🧪 the Windows path, awaiting verification (broken on macOS) |
| Audio: `nmea` (USB serial PCM) | ❌ crashes USB on macOS — do not use |
| SIM | needs voice + SMS service; VoLTE/CS voice depends on carrier |

macOS has **no native serial port** for Quectel vendor interfaces, so a
USB→PTY bridge (`scripts/ec20_usb_pty.py`) exposes `/tmp/ec20-at`.

### Get the hardware

You need a **Quectel EC20 or EG25** 4G modem (this build is verified against
`EC20CEFAGR08A03M4G`). The common mini-PCIe module also needs:

- a **USB adapter board with a SIM slot** (turns the mini-PCIe module into a USB device),
- a **4G antenna**,
- a **SIM with voice + SMS service** (verified on China Telecom; VoLTE / CS voice depends
  on your carrier).

A full EC20 module + adapter kit is roughly **¥100–200 / $15–30** — search AliExpress
or Taobao for "EC20 USB adapter".

### Requirements

- Python 3.12+, a working `ffmpeg` on PATH, an EC20/EG25 modem with an active SIM.
- **macOS:** `brew install libusb` — the USB→PTY bridge's `pyusb` needs this system library.
- A **DashScope API key** (for Qwen). Get one at
  <https://dashscope.console.aliyun.com/>. International users go through Alibaba Cloud's
  **Model Studio** (different endpoint — override with `DASHSCOPE_REALTIME_URL` if needed).
  (Doubao / OpenAI credentials optional.)

### Quick start (macOS)

```bash
git clone https://github.com/tianye1999/callpilot.git callpilot && cd callpilot
bash scripts/setup.sh         # one command: checks Python 3.12+/ffmpeg, creates .venv + .env

# terminal 1 — USB→PTY bridge (exposes /tmp/ec20-at)
.venv/bin/python scripts/ec20_usb_pty.py --map 2:/tmp/ec20-at

# terminal 2 — the service (opens http://127.0.0.1:47100)
.venv/bin/python app.py
```

<details>
<summary>Manual setup (what <code>setup.sh</code> does)</summary>

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # then edit .env (see below)
```

</details>

Minimum `.env`:

```ini
DASHSCOPE_API_KEY=sk-your-key
MODEM_PORT=/tmp/ec20-at
MODEM_AUDIO_MODE=uac_ffmpeg
MODEM_AUDIO_KEYWORD=Interface
OWNER_NAME=Your Name        # shown to callers; blank = neutral "the owner"
AGENT_LANGUAGE=en           # language the AI speaks on calls & summaries (zh|en); default zh
```

Then call the modem's SIM number — the AI should auto-answer. All settings are
editable live in the **Settings** panel of the UI.

### Quick start (Windows) — awaiting hardware reports

Windows needs **no USB bridge**: install the official Quectel EC20 Windows
driver and the modem shows up as native COM ports. `MODEM_PORT=auto` (the
Windows default) scans for the Quectel AT port by USB VID; audio uses
`MODEM_AUDIO_MODE=uac` (PortAudio/WASAPI — `uac_ffmpeg` is macOS-only).

```powershell
git clone https://github.com/tianye1999/callpilot.git callpilot; cd callpilot
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1   # checks Python/ffmpeg, creates .venv + .env
.venv\Scripts\python app.py
# auto-start at logon (Task Scheduler):
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 install
```

<details>
<summary>Manual setup (what <code>setup.ps1</code> does)</summary>

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env             # then edit .env
```

</details>

Details in [`scripts/windows/README.md`](scripts/windows/README.md). This path
is code-complete and CI-tested but **not yet verified on real hardware** — if
you have an EC20 on Windows, please [report back](.github/ISSUE_TEMPLATE)!

### Desktop app (optional)

```bash
# macOS
.venv/bin/pip install pyinstaller   # pywebview is already a core dependency
bash scripts/build_app.sh          # → dist/CallPilot.app
# Windows
powershell -ExecutionPolicy Bypass -File scripts\windows\build_app.ps1   # → dist\CallPilot\CallPilot.exe
```

The current `.app` is a **thin window over your local checkout** (it launches the
service from this repo). A self-contained installer is v0.3 on the roadmap.

### Verify it works, without a human on the line

- **Dial your own mobile**: the simplest check — pick up and you hear the AI talk.
- **Dial your carrier's customer-service hotline** (an IVR that speaks back): if the AI
  holds a coherent multi-turn exchange with the voice menu, both audio directions work.
  `10000` is the China Telecom IVR; use whatever hotline your carrier provides.
- **SMS `查余额` (balance) to `10000`** (China Telecom): you should receive a reply SMS —
  proves send + receive + Chinese decoding.

### Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| App can't open `/tmp/ec20-at` | USB bridge not running, or modem replugged (bridge auto-reconnects; the service also re-opens the serial port) |
| Modem drops off USB repeatedly | #1 cause: **system sleep** re-enumerates USB and stalls the modem's endpoints. The launchd plists wrap both processes in `caffeinate -s`; if you run manually, `caffeinate -s .venv/bin/python ...` or set `pmset -a sleep 0`. The bridge also does a `dev.reset()` on reconnect and backs off 1→30 s; after 6 fast failures it exits so launchd can cold-restart it |
| No audio at all on macOS | `MODEM_AUDIO_MODE` must be `uac_ffmpeg`; `PortAudio`/`nmea` don't work here |
| PortAudio `-9986 / -66740` | stuck `coreaudiod`: `sudo killall coreaudiod` |
| Can't hear the AI in the room | enable **Monitor on this Mac** in Settings; raise `MONITOR_UPLINK_GAIN` for the caller side |
| Caller (non-AI) voice too quiet | raise `MONITOR_UPLINK_GAIN` (default 8, we ran 15 on real hardware) |
| Second call is silent | fixed — the voice channel is re-armed per call |

### Safety, privacy & legal

**Read before using on a real line.**

- **Not for emergency calls.** Do not rely on CallPilot for 110/119/911 or any
  life-safety communication.
- **Recording laws vary by jurisdiction** — call recording is **on by default**
  and stored locally only; disable it in Settings or with
  `RECORDING_ENABLED=false`. You are responsible for obtaining any consent the
  law requires.
- **Anti-harassment / telemarketing rules apply** to outbound and batch dialing.
  Use the dial whitelist and dial your own numbers for testing.
- **You bear all carrier charges and API costs.**
- **Your API keys stay in your local `.env`** (git-ignored). Never commit them.
- Provided **as-is, no warranty** (Apache-2.0).

### Contributing

Developer Preview wants hardware reproduction reports. If you have an EC20, please
open an issue with your modem firmware, macOS version, and what worked / didn't.
Tests: `.venv/bin/pytest` (no hardware needed — uses fake modem/bridge/agent).

License: [Apache-2.0](LICENSE).

---

## 中文

### 这是什么

CallPilot 把 4G 模组接到云端实时语音大模型，让 AI「助理」替你接打电话：插上
Quectel EC20/EG25，来电自动接听并与对方对话，可外呼、收发短信、按 IVR 菜单键、
每通电话录音+延迟打点+AI 摘要——全部跑在你自己的硬件和 API Key 上。

- **AI 大脑**：云端端到端实时语音（默认阿里 **Qwen Omni**，可选字节 **Doubao**），
  无需安装本地模型。
- **电话通道**：EC20/EG25 模组的硬件 AT 事件（`RING → ATA`），非屏幕自动化。
- **能力**：自动接听、外呼（单个+批量带白名单）、中文短信收发、AI 工具调用
  （发短信/挂断/查验证码/**DTMF 按键**）、通话录音+摘要、实时转写、本机监听、
  中英双语桌面界面。

### 硬件与平台支持

| 项 | 状态 |
|----|------|
| Quectel EC20（本版本对 `EC20CEFAGR08A03M4G` 验证） | ✅ 已验证 |
| macOS（Apple Silicon 与 Intel/Rosetta） | ✅ 已验证 |
| Windows 10/11（Quectel 官方驱动，原生 COM 口） | 🧪 已完整支持，**待硬件复现反馈** |
| Linux（原生串口） | ⚠️ 代码路径存在，**未验证** |
| 音频 `uac_ffmpeg`（ffmpeg 走 UAC 声卡） | ✅ 已验证——**仅 macOS** |
| 音频 `uac`（PortAudio/WASAPI） | 🧪 Windows 主路径，待验证（macOS 上不可用） |
| 音频 `nmea`（USB 串口 PCM） | ❌ macOS 会崩 USB，勿用 |
| SIM 卡 | 需语音+短信服务；VoLTE/CS 语音取决于运营商 |

macOS 没有 Quectel 厂商串口的原生设备，需先跑 USB→PTY 桥（`scripts/ec20_usb_pty.py`）
暴露出 `/tmp/ec20-at`。

### 硬件准备

需要一个 **Quectel EC20 或 EG25** 4G 模组（本版本对 `EC20CEFAGR08A03M4G` 验证）。
常见的 mini-PCIe 模组还需要：**带 SIM 卡座的 USB 转接板**（把模组变成 USB 设备）、
**4G 天线**、一张**开通语音+短信的 SIM**（中国电信已验证；VoLTE 取决于运营商）。
模组+转接板全套约 **¥100–200**，淘宝搜「EC20 USB 转接板」。

### 前置

- Python 3.12+、PATH 里有 `ffmpeg`、一张有效 SIM 的 EC20/EG25 模组。
- macOS 还需 `brew install libusb`（USB→PTY 桥的 pyusb 依赖系统库）。
- **DashScope API Key**（Qwen 用），申请：<https://dashscope.console.aliyun.com/>。

### 快速开始（macOS）

```bash
git clone https://github.com/tianye1999/callpilot.git callpilot && cd callpilot
bash scripts/setup.sh         # 一条命令：检查 Python 3.12+/ffmpeg，创建 .venv + .env

# 终端 1 — USB→PTY 桥
.venv/bin/python scripts/ec20_usb_pty.py --map 2:/tmp/ec20-at

# 终端 2 — 服务（打开 http://127.0.0.1:47100）
.venv/bin/python app.py
```

<details>
<summary>手动步骤（即 <code>setup.sh</code> 做的事）</summary>

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # 编辑 .env
```

</details>

`.env` 最小配置见上方英文段。之后拨打模组 SIM 卡号码即可，AI 应自动接听；
所有配置都能在界面「设置」面板里实时修改。

### 快速开始（Windows）—— 待硬件复现反馈

Windows **不需要 USB 桥**：装 Quectel 官方 EC20 Windows 驱动后模组直接暴露原生
COM 口。`MODEM_PORT=auto`（Windows 默认）按 USB VID 自动扫描 AT 口；音频用
`MODEM_AUDIO_MODE=uac`（PortAudio/WASAPI，`uac_ffmpeg` 仅 macOS）。

```powershell
git clone https://github.com/tianye1999/callpilot.git callpilot; cd callpilot
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1   # 检查 Python/ffmpeg，创建 .venv + .env
.venv\Scripts\python app.py
# 开机常驻（计划任务）：
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 install
```

<details>
<summary>手动步骤（即 <code>setup.ps1</code> 做的事）</summary>

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env             # 编辑 .env
```

</details>

详见 [`scripts/windows/README.md`](scripts/windows/README.md)。该路径代码完备且
过 CI，但**尚未真机验证**——如果你有 EC20 + Windows，欢迎提 issue 反馈！

### 桌面 App（可选）

```bash
# macOS
.venv/bin/pip install pyinstaller pywebview
bash scripts/build_app.sh          # → dist/CallPilot.app
# Windows
powershell -ExecutionPolicy Bypass -File scripts\windows\build_app.ps1   # → dist\CallPilot\CallPilot.exe
```

当前 `.app` 是**你本地代码仓库的薄壳窗口**（从本仓库拉起服务）。真正独立的安装包
是路线图 v0.3。

### 无需真人也能自测

- **拨 `10000`**（电信 IVR）：若 AI 能与语音菜单连贯多轮对话，说明双向语音都通。
- **发「查余额」短信到 `10000`**：会收到回复短信，验证发+收+中文编解码全链路。

### 排障

| 现象 | 可能原因 / 解决 |
|------|----------------|
| 打不开 `/tmp/ec20-at` | 桥没跑或模组重插（桥会自动重连，服务也会重开串口） |
| 模组反复从 USB 掉线 | 首要诱因是**系统睡眠**导致 USB 重枚举、端点 stall。launchd plist 已用 `caffeinate -s` 包裹进程；手动运行请加 `caffeinate -s` 前缀或 `pmset -a sleep 0`。桥重连时会先 `dev.reset()` 并指数退避（1→30s），连续快速失败达阈值后退出交给 launchd 冷重启 |
| macOS 完全没声音 | `MODEM_AUDIO_MODE` 必须是 `uac_ffmpeg` |
| PortAudio 报 `-9986 / -66740` | coreaudiod 卡死：`sudo killall coreaudiod` |
| 电脑上听不到 AI | 设置里开「本机监听」；对方声音小就调大 `MONITOR_UPLINK_GAIN` |
| 第二通电话没声音 | 已修复（每通电话重新启用语音通道） |

### 安全、隐私与合规

**上真机前务必阅读。** 不用于紧急电话；通话录音**默认开启**、仅存储在本地，可在
设置面板或 `RECORDING_ENABLED=false` 关闭——是否录音、是否需征得对方同意由你按
当地法律负责；外呼/批量呼叫须遵守反骚扰与营销合规；运营商资费与 API 费用由你自行
承担；API Key 只存于本地 `.env`（已 git 忽略），切勿提交。本软件按「原样」提供，
不作任何担保（Apache-2.0）。

### 贡献

Developer Preview 阶段最需要同型号硬件的复现反馈。有 EC20 的话，欢迎带上模组固件、
macOS 版本、以及哪里成功/失败开 issue。测试：`.venv/bin/pytest`（无需硬件）。

许可证：[Apache-2.0](LICENSE)。
