# CallPilot 架构速览

一页纸说明代码怎么组织、一通电话经过哪些模块，帮助贡献者快速定位要改的文件。
（用户视角的功能介绍见 [README](../README.md)；演进计划见 [roadmap](roadmap.md)。）

## 总览图

```
  电话网 (PSTN / VoLTE)
        │
        ▼
 ┌─────────────────┐  AT 串口 (RING/ATA/ATD/CLCC/短信/DTMF)
 │  EC20/EG25 模组  │◄───────────────────────────► modem.py
 │     (USB)       │                                  │ 事件回调
 │                 │  8kHz PCM 语音                    ▼
 └─────────────────┘◄──────────► audio_bridge.py ◄─ call_agent.py (CallSession)
        ▲                          （重采样）        │        │
        │ macOS 无原生驱动：                          │        │ wss (realtime)
        │ scripts/ec20_usb_pty.py                    │        ▼
        │ USB→PTY 桥出 /tmp/ec20-at                  │   agents/ (Qwen Omni /
        │                                            │   Doubao / OpenAI)
        │                                            │        │
        │              prompts.py ──────────────────►│        │ 云端语音模型
        │              call_tools.py（AI 工具）──────►│
        │              summarizer.py（通话后摘要）◄───┤
        │              call_log.py（录音/事件落盘）◄──┤
        │              remote_dialer.py（远程真人外呼）◄┤
        │                    ▲ LiveKit 音频/数据通道    │
        │                    │                          │
        │           手机浏览器 Web Dialer ─────────────┘
        │              dial_queue.py（批量外呼）─────►│
        │              sms_email_forwarder.py ◄─ 新短信回调
        │                                            ▼
        │                                       events.py (EventHub)
        │                                            │ WebSocket 广播
        │                                            ▼
        │                                      web/server.py (aiohttp)
        │                                            │
        │                          web/static/index.html（仪表盘单页）
        │                                            ▲
        └── app.py（入口/常驻编排，把上面全部装配起来） │
             desktop_app.py（pywebview 薄壳）────────┘ 或直接浏览器访问
```

一通来电的路径：模组上报 `RING` → `modem.py` 回调 → `call_agent.py` 建立
CallSession（`ATA` 接听）→ `audio_bridge.py` 把 8kHz PCM 与云端模型的
16k/24k 音频互转 → `agents/` 经 WebSocket 与 realtime 模型对话（可调
`call_tools.py` 里的工具）→ 全程事件经 `events.py` 推给网页 → 挂断后
`summarizer.py` 出摘要、`call_log.py` 落盘。

首次启动时，`app.py` 仍负责装配服务；Web 端会通过 `/api/meta` 与 `/api/config`
驱动首启向导，完成硬件检查、provider 凭证、机主/人设和测试短信配置后写回 `.env`。

## 模块职责

| 模块 | 职责 |
|------|------|
| `app.py` | 进程入口与常驻编排：韧性启动（模组不在也起 Web）、后台 supervisor 重连、装配以下所有模块 |
| `main.py` | 精简命令行入口：仅来电自动接入（无 Web、无常驻编排），用于最小化 headless 运行 |
| `src/agentcall/modem.py` | EC20/EG25 AT 指令封装：接听/拨号/挂断/CLCC 轮询/短信 (UCS2)/DTMF，串口断线自动重连，通话丢失判定 |
| `src/agentcall/audio_bridge.py` | 模组 8kHz PCM ↔ AI 采样率的双向音频桥；三种模式：`uac_ffmpeg`（macOS 已验证）/ `uac`（PortAudio/WASAPI，Windows 主路径）/ `nmea`（勿在 macOS 用） |
| `src/agentcall/call_agent.py` | CallSession 会话编排：来电/外呼生命周期、模组↔音频桥↔Agent 接线、延迟挂断与世代号防护 |
| `src/agentcall/remote_dialer.py` | #31 远程网页拨号状态机：短期邀请、媒体就绪后才 ATD、单通幂等、全双工 PCM、DTMF、断线宽限与录音审计 |
| `src/agentcall/livekit_media.py` | LiveKit programmatic participant：浏览器音轨 ↔ 8k PCM、有界媒体队列、身份/主题受限的控制数据通道 |
| `src/agentcall/remote_pairing.py` | #31.1 本地手机配对库：一次性短期配对码、最多 5 台设备、长期凭证仅存哈希、原子落盘与撤销 |
| `src/agentcall/web/remote_gateway.py` | 独立公网最小权限网关：固定 PWA 页面、HttpOnly 配对 Cookie、单通短期会话签发；不挂载管理 API |
| `src/agentcall/cloud_credentials.py` | #42 云端 Edge 凭证与 Ed25519 设备密钥的系统钥匙串存储 |
| `src/agentcall/cloud_control.py` | #42 Edge 主动 WSS、心跳、严格命令校验、会话启动 ACK 与云端 enrollment/配对 API 客户端 |
| `cloud/` | 公司托管 Beta 控制面：Worker API、Durable Object Edge 路由、D1 数据、LiveKit 短期 token broker 与固定 PWA |
| `src/agentcall/number_profiles.py` | 本地预设任务库：JSON 兼容加载、稳定 ID、分层匹配、CRUD 校验与原子写入 |
| `src/agentcall/prompts.py` | 通话提示词构造，纯函数、可独立测试 |
| `src/agentcall/call_tools.py` | 通话中 AI 工具（发短信/挂断/读验证码/按 DTMF）的注册与执行 |
| `src/agentcall/agents/` | `base.py` VoiceAgent 抽象接口 + 各云端 realtime 实现 + `factory.py` 按 `AGENT_PROVIDER` 创建。**加新语音 provider 从这里入手** |
| `src/agentcall/web/` | aiohttp 服务（`server.py`）：仪表盘页面、WebSocket 实时推送、短信/外呼/历史/配置 REST API；前端是 `static/index.html` 单文件 |
| `src/agentcall/config.py` | 集中配置注册表：**所有环境变量的默认值只在这里注册**（`.env.example` 有防脱节测试绑定），凭证校验与 `.env` 持久化 |
| `src/agentcall/platforms.py` | 平台差异唯一出处：Windows/macOS/Linux 的默认端口、路径、venv 位置。**业务代码不直接判 `sys.platform`** |
| `src/agentcall/port_detect.py` | `MODEM_PORT=auto` 时按 Quectel VID (0x2C7C) 自动扫描 AT 串口（Windows 主要用） |
| `src/agentcall/events.py` | EventHub：把模组/Agent 线程的事件线程安全地广播给网页 WebSocket |
| `src/agentcall/sms_email_forwarder.py` | 新短信邮件转发：可靠 OTP 提取、UTF-8 邮件构造、TLS-only SMTP、有限重试与有界后台队列；默认关闭且不重放历史短信 |
| `src/agentcall/call_log.py` | 通话记录：按通话建目录，保存事件打点 (`events.jsonl`)、双向录音 WAV、元数据 |
| `src/agentcall/dial_queue.py` | 批量外呼队列 + 号码白名单 |
| `src/agentcall/rate_limit.py` | 进程内滑动窗口频控，用于短信发送闸与远程网页外呼闸 |
| `src/agentcall/repeat_suppression.py` | 文本相似度判重，抑制 IVR 场景里 AI 近似重复应答 |
| `src/agentcall/contacts.py` | 已联系号码策略：从短信历史、来电记录和当前通话对端判定短信回复目标是否允许 |
| `src/agentcall/summarizer.py` | 通话后把整通转写交给文本模型，产出结构化摘要 |
| `src/agentcall/monitor_playback.py` | 通话音频镜像到本机扬声器（监听旁路，仅 macOS，其他平台优雅降级） |
| `src/agentcall/coreaudio.py` | macOS CoreAudio 设备枚举（ctypes 直调，无第三方依赖） |
| `src/agentcall/macos_launchd.py` | 打包版 macOS App 的 launchd agent 生成、安装、卸载与重启管理 |
| `scripts/ec20_usb_pty.py` | macOS USB→PTY 桥：Quectel 无 mac 驱动，用 libusb 把 AT 口暴露为 `/tmp/ec20-at` |
| `scripts/launchd/`、`scripts/windows/` | 常驻安装：macOS launchd 双单元 / Windows 计划任务 (`install.ps1`) |
| `desktop_app.py` | 桌面形态：pywebview 薄窗口包住本地 Web |
| `tray_app.py` | macOS 菜单栏托盘常驻形态：顶栏电话图标（绿=运行/灰=停止）+ 打开控制台/重启服务/退出菜单，产品化默认形态 |
| `packaging/` | PyInstaller 打包（macOS `.app` / Windows `.exe`） |

## 贡献者定向：想改 X 该去哪

- **接新的语音模型**：`agents/` 下实现 `VoiceAgent` 子类 → `factory.py` 注册 → 配置项进 `config.py`。
- **加配置项**：只在 `config.py` 注册表登记默认值，并同步 `.env.example`（有测试强制两者一致）。
- **平台相关行为**：改 `platforms.py`，不要在业务代码里散落 `sys.platform` 判断。
- **网页 UI / API**：`web/server.py`（路由）+ `web/static/index.html`（前端单文件，内置 EN/中文 i18n 字典）。
- **预设任务库**：存储、匹配或 CRUD 规则改 `number_profiles.py`；管理 API/UI 分别在 `web/server.py` 与 `web/static/index.html`。
- **AT 指令 / 模组行为**：`modem.py`；注意串口操作要走 `_serial_lock` 原子块（与 CLCC 轮询并发）。
- **单独学习/验证某个模组能力**：`examples/modem/` 有每个原子能力（原始 AT、设备探测、
  拨号、接听、发短信、收短信、DTMF）的最小独立 demo，基于 `Eg25Modem`，见其 README。
- 每步改动跑对应单测（`tests/unit/`），提交前全量 `.venv/bin/pytest -q`。
