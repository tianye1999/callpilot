# AgentCall — EC20 4G 模组 AI 电话助手

插上 Quectel EC20/EG25 模组，来电自动接听，由 AI 完成对话；支持外呼、短信收发、
AI 工具调用（发短信/挂断/查验证码）与网页仪表盘。

- 需求与架构决策：`docs/01-requirements.md`
- 里程碑与任务拆解：`docs/02-task-breakdown.md`

## 架构一览

```
手机来电 → EC20 模组 ──(AT: RING/ATA/CLCC)── Eg25Modem
                │                                │ 回调
            8kHz PCM                      CallAgentService / CallSession
                │                                │
         ModemAudioBridge(UAC) ──────── VoiceAgent (qwen | doubao | local*)
         SerialPcmAudioBridge(NMEA)              │
                                          EventHub → aiohttp Web 仪表盘
```

\* `local`（本地流水线 VAD→FunASR→DeepSeek→sherpa-onnx）为 M2 交付，见任务拆解。

## 环境准备

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # 填入 DASHSCOPE_API_KEY 等
```

## macOS 运行（EC20 经 USB→PTY 桥）

macOS 不会为 Quectel 厂商串口创建 /dev/cu.*，需先跑 USB→PTY 桥：

```bash
# 终端 1：把 AT 口(interface 2)与 PCM 口(interface 3)桥接为 PTY
.venv/bin/python scripts/ec20_usb_pty.py --map 2:/tmp/ec20-at --map 3:/tmp/ec20-pcm

# 终端 2：启动服务（网页版，自动打开 http://127.0.0.1:8000）
MODEM_PORT=/tmp/ec20-at .venv/bin/python app.py
```

CLI 模式（无网页）：

```bash
.venv/bin/python main.py --port /tmp/ec20-at --audio-mode uac --audio-keyword Interface --provider qwen
```

Windows/Linux 有原生串口（如 `COM3` / `/dev/ttyUSB2`），直接在 `.env` 里配置
`MODEM_PORT`，无需桥。

## 配置

所有配置经 `.env`（参考 `.env.example`）：模组串口/波特率、音频模式
（`uac`=USB 声卡 / `nmea`=NMEA 串口 PCM）、`AGENT_PROVIDER` 与各家凭证、
Web 端口等。**`.env` 含明文 key，已 gitignore，勿提交勿外发。**

## 测试

```bash
.venv/bin/pytest
```

单测基于 `tests/fakes/` 的 FakeModem / FakeAudioBridge / FakeAgent，无需硬件。

## 目录

```
src/agentcall/          核心包（modem / audio_bridge / call_agent / events / web / agents）
scripts/                EC20 调试工具与 USB→PTY 桥
tests/                  单测与夹具
docs/                   需求、任务拆解、Spike 报告
data/                   运行时数据（日志/短信记录，gitignore）
```
