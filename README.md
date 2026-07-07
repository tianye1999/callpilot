# AgentCall

在电脑上运行 AI Agent，当 **Quectel EG25-G** 模组（插 SIM 卡）有来电时自动接听，并通过实时语音模型（**通义千问** 或 **豆包**）与对方对话，包括自我介绍当前模型名称。

## 整体架构

```
手机来电 → 运营商 → EG25 模组(RING) → Python 监听 AT 串口
                                              ↓ ATA 接听
                                    USB 声卡 UAC (8kHz PCM)
                                              ↕ 重采样
                              千问/豆包 Realtime API (16k/24k PCM)
                                              ↕
                                    Agent 语音回复 → 对方听到
```

### 关键模块

| 模块 | 作用 |
|------|------|
| `src/modem.py` | 串口 AT 指令：监听 `RING`、 `ATA` 接听、 `ATH` 挂断、 `AT+QPCMV` 启用 UAC |
| `src/audio_bridge.py` | EG25 USB 声卡 8kHz ↔ AI 模型 16k/24k 音频桥接 |
| `src/agents/qwen_agent.py` | 通义千问 Qwen-Omni Realtime（DashScope WebSocket） |
| `src/agents/doubao_agent.py` | 豆包端到端实时语音（火山引擎 Realtime Dialogue） |
| `src/call_agent.py` | 来电触发 → 接听 → 启动 Agent 会话 |

## 硬件与系统准备

### 1. EG25 模组连接

- USB 连接电脑，通常会出现：
  - **AT 串口**（Windows 设备管理器 → 端口 → 如 `COM3`）
  - **USB Audio**（启用 UAC 后显示为 `EG25-G` 声卡）
- SIM 卡需支持 **VoLTE 语音**（仅数据卡无法接电话）

### 2. 启用模组 UAC 语音（首次）

程序启动时会自动发送：

```text
AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1
AT+QPCMV=1,2
```

通话音频格式：**8kHz、16-bit、单声道 PCM**（Quectel 规范）。

### 3. 确认设备

```powershell
cd d:\Documents\AgentCall
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# 查看音频设备，确认 EG25 声卡 index / 名称
python main.py --list-audio
```

## 配置 API

复制 `.env.example` 为 `.env` 并填写：

### 通义千问（推荐，接入简单）

1. 登录 [阿里云百炼 / DashScope](https://help.aliyun.com/zh/model-studio/)
2. 创建 API Key，开通 **Qwen-Omni Realtime** 或 `qwen3-omni-flash-realtime`
3. 配置：

```env
AGENT_PROVIDER=qwen
DASHSCOPE_API_KEY=sk-xxx
QWEN_REALTIME_MODEL=qwen3-omni-flash-realtime
AGENT_MODEL_NAME=通义千问 Qwen3-Omni
MODEM_PORT=COM3
MODEM_AUDIO_KEYWORD=EG25
```

### 豆包（火山引擎实时语音）

1. 登录 [火山引擎豆包语音控制台](https://www.volcengine.com/docs/6561/1594356)
2. 开通 **端到端实时语音大模型**，获取 App ID、Access Key
3. 配置：

```env
AGENT_PROVIDER=doubao
DOUBAO_APP_ID=你的AppId
DOUBAO_ACCESS_KEY=你的AccessKey
AGENT_MODEL_NAME_DOUBAO=豆包实时语音大模型
```

## 运行

### 方式一：一键启动 + 网页仪表盘（推荐）

```powershell
python app.py
```

启动后会自动打开浏览器 `http://127.0.0.1:8000`，网页仪表盘实时显示：

- **当前来电**：来电号码与状态（振铃中 / 通话中 / 已结束）
- **对话内容**：用户与红茶助手的语音转写，实时气泡滚动
- **短信**：收到的短信列表 + 发短信入口（号码 + 内容，支持中文）

端口可用环境变量覆盖：`WEB_HOST`（默认 `127.0.0.1`）、`WEB_PORT`（默认 `8000`）。
短信收发记录持久化在 `data/messages.json`，刷新或重启后仍可见。

### 方式二：纯命令行（无界面）

```powershell
python main.py --port COM3 --provider qwen
```

流程：

1. 服务启动，监听 `RING`
2. 有来电 → 自动 `ATA` 接听
3. 建立 Realtime 会话，Agent 按 system prompt **先自我介绍模型名称**
4. 持续双向语音对话，直到对方挂断（`NO CARRIER`）
5. 收到短信自动读取并打印（`AT+CMGF=1` 文本模式，中文自动 UCS2 解码）

## 常见问题

### 听不到声音 / 无法说话

- 确认 `AT+QPCMV=1,2` 成功，且 Windows 识别到 EG25 USB 声卡
- 用 `--list-audio` 检查 `--audio-keyword` 是否匹配设备名
- 部分 EG25 固件需升级到较新版本才稳定支持 UAC

### 只有数据没有语音

- 检查 SIM 是否开通 VoLTE
- 发送 `AT+CLCC` 确认模组处于 voice call 状态

### 延迟较高

- 电话链路本身是 8kHz 窄带，Realtime 模型通常用 16k/24k，重采样会有额外延迟
- 可缩短 Agent 回复长度（已在 system prompt 中限制）
- 生产环境可考虑模组侧 16k PCM（`AT+EN_PCM16K`）+ 更贴近 16k 的模型

### 豆包协议报错

火山 Realtime Dialogue 使用二进制 WebSocket 协议，若官方字段有更新，请以[官方文档](https://www.volcengine.com/docs/6561/1594356)为准微调 `src/agents/doubao_agent.py`。也可参考开源项目 [RealtimeDialog-doubao](https://github.com/SUAT-AIRI/RealtimeDialog-doubao)。

## 扩展建议

- **只播报固定开场白**：可在 `CallSession._handle_call` 里接听后先 TTS 播放 WAV，再进入 Agent
- **接入文本 LLM + 工具调用**：在 Agent 层增加 function calling，实现查天气、查订单等
- **来电白名单**：在 `on_ring` 回调里过滤 `+CLIP` 号码
- **日志与录音**：将 `pcm_8k` 落盘便于调试

## 许可证

MIT
