# AgentCall 需求文档 — EC20 模组 AI 电话助手

> 版本：v1.0（2026-07-07）
> 状态：已评审（Claude 设计 + codex 三轮评审，verdict: PASS with notes，见 `.codex_dialog.md`）
> 源项目：`/Users/tianye/temp/AA`（EC20 模组控制）、`/Users/tianye/iphone-call-ai-poc`（本地 AI 对话流水线）

---

## 1. 背景与动机

现有两个 POC 各自验证了半边能力：

| 项目 | 已验证 | 局限 |
|------|--------|------|
| **AA**（temp/AA） | EC20 模组 AT 控制（RING→ATA 自动接听、CLCC、挂断、短信收发）、8kHz PCM 音频桥（UAC/NMEA 双模式）、云端 Realtime agent（千问/豆包）、aiohttp 仪表盘 | agent 只能用云端端到端语音 API（依赖 DASHSCOPE key、成本、不可定制大脑）；缺测试；temp 目录非正式仓库 |
| **iphone-call-ai-poc** | 本地 AI 大脑四件套：Silero VAD → FunASR STT → DeepSeek LLM → sherpa-onnx TTS；工程基建（模型预热、LLM keepalive、会话日志、配置面板、健康检查） | 电话通道靠 iPhone Continuity + Mac 虚拟声卡 + 截图点接听按钮，链路脆弱且 Mac 强绑定 |

**AgentCall 项目目标**：把两者合并为一个正式项目——EC20 硬件通道（干净的 RING→ATA 事件，替代截图点按钮）+ 本地可控的 AI 大脑（替代/并存云端 Realtime），形成可长期演进的 AI 电话助手。

## 2. 目标与非目标

### 目标
1. 插上 EC20 后，来电**自动接听**并由 AI 完成对话，全程无人值守。
2. AI 大脑支持三种 provider 可配置切换：`qwen`（云端 Realtime）、`doubao`（云端 Realtime）、`local`（本地 VAD+FunASR+DeepSeek+sherpa 流水线，**本项目新增的核心**）。
3. 支持 AI 工具调用：发短信、挂断、查验证码（带安全 policy）。
4. 支持外呼（dial）与短信收发。
5. Web 仪表盘：实时通话状态、对话转写、短信、预热进度、配置。
6. 本地 provider 端到端延迟（用户说完 → 听到 AI 首帧声音）p95 < 2.5s（流式版；整句版容忍 < 4s）。*目标值来源：poc Mac 实测各环节延迟（stt 0.6s + llm 1.2s + tts 0.7s，2026-07-06），待 Spike S3 在本项目真机实测校准。*

### 非目标（本期不做）
- 多路并发通话（EC20 单模组单通道）。
- Linux/树莓派部署（架构上预留，本期只交付 macOS 运行；不引入新的 Mac 专属依赖）。
- 声音克隆 / 多音色（poc 的 ZipVoice 不迁移）。
- iPhone Continuity 链路的任何部分（PhoneCap / AIToPhone / auto_answer 截图 / FaceTime 监听全部废弃）。

## 3. 系统架构

### 3.1 分层与数据流

```
手机来电 → 运营商 → EC20 模组
    │ USB (macOS 无原生驱动，经 ec20_usb_pty.py 桥接出 /tmp/ec20-at)
    ▼
[模组控制层] Eg25Modem —— AT 命令：RING/CLIP/CLCC 检测、ATA 接听、ATH 挂断、CMGS/CMGR 短信
    │ on_ring / on_hangup / on_sms 回调
    ▼
[会话编排层] CallAgentService / CallSession —— 状态机：ringing→answered→active→ended
    │                                          半双工防回环、工具注册、事件发布
    ▼
[音频桥] ModemAudioBridge(UAC) / SerialPcmAudioBridge(NMEA) —— 8kHz int16 mono，20ms/块，重采样
    │ send_audio(pcm) ↑ / on_audio_out(pcm) ↓
    ▼
[Agent 层] VoiceAgent 抽象 ← factory(AGENT_PROVIDER)
    ├─ QwenVoiceAgent   （云端 Realtime，16k in / 24k out，现状保留）
    ├─ DoubaoVoiceAgent （云端 Realtime，现状保留，无工具）
    └─ LocalPipelineAgent（★ 新增：Silero VAD → FunASR → DeepSeek → sherpa-onnx）
         内部状态机：listening / processing / speaking / stopping
         非阻塞：send_audio() 只喂 VAD + 入队；后台 worker 跑 STT→LLM→TTS
    ▼
[基础设施] EventHub（事件/持久化）→ aiohttp Web（仪表盘/WS 推送/API）
           预热(warmup) / keepalive / call_log / config 面板
```

### 3.2 目录结构（codex 评审建议：包结构，不平铺）

```
AgentCall/
├── app.py                      # Web 模式入口
├── main.py                     # CLI 模式入口
├── pyproject.toml / requirements.txt
├── src/agentcall/
│   ├── modem.py                # ← AA src/modem.py
│   ├── audio_bridge.py         # ← AA src/audio_bridge.py（+UAC playback-active 修正）
│   ├── call_agent.py           # ← AA src/call_agent.py（参数化硬编码）
│   ├── events.py               # ← AA src/events.py
│   ├── web/                    # ← AA src/web/（+warmup 进度、config 面板）
│   └── agents/
│       ├── base.py             # ← AA（接口扩展：状态语义）
│       ├── factory.py          # ← AA（+local provider）
│       ├── qwen_agent.py       # ← AA 原样
│       ├── doubao_agent.py     # ← AA 原样
│       └── local/              # ★ 新增，模块 vendor 自 poc
│           ├── pipeline_agent.py   # 新写：LocalPipelineAgent 状态机 + worker
│           ├── vad.py              # ← poc vad.py（Silero，支持 8k/16k）
│           ├── stt.py              # ← poc stt_local.py（FunASR）
│           ├── tts.py              # ← poc tts.py（sherpa-onnx，剥离 wav 落盘播放）
│           ├── tts_adapter.py      # 新写：文本→PCM chunk adapter
│           ├── llm_client.py       # ← poc openai_client.py 精简（DeepSeek）
│           ├── tool_loop.py        # 新写：function calling 循环 + policy
│           ├── prompts.py          # ← poc outbound_agent.py（persona/收束逻辑）
│           └── warmup.py           # ← poc llm_warmup.py + warmup_state.py
├── scripts/
│   ├── ec20_usb_pty.py         # ← AA scripts/（+生命周期管理）
│   └── install_models.py       # 新写：模型资产下载/校验/预热自检
├── tests/
│   ├── fakes/                  # fake modem / fake bridge / fake agent
│   └── unit/
├── docs/
└── data/                       # 日志、录音、messages.json（gitignore）
```

### 3.3 采样率链路（决策 D3）

```
modem 8kHz ──→ VAD 直接跑 8k（Silero 原生支持，省一次上采样）
                 └─ speech segment 上采样 16k ──→ FunASR
DeepSeek 文本 ──→ sherpa TTS 24kHz ──→ 重采样 8k ──→ modem
```
重采样实现待 Spike S1 对比（AA 现有线性插值 vs `scipy.signal.resample_poly` vs `soxr`）后定。

## 4. 功能需求

| 编号 | 需求 | 说明 | 验收 |
|------|------|------|------|
| FR-1 | 来电自动接听 | RING/CLCC 检测→ATA，去重防重复接听 | 真机来电 3 次连续成功 |
| FR-2 | AI 对话 | 三 provider 可切换；local 为核心交付 | 真机通话轮次 ≥ 5 轮无卡死 |
| FR-3 | provider 配置切换 | `AGENT_PROVIDER=qwen\|doubao\|local`，启动时校验凭证/模型资产 | 三种配置分别启动成功，缺凭证时启动即报错 |
| FR-4 | 工具调用 | send_sms / hangup_call / query_verification_code；local 用 DeepSeek function calling | 真机验证三工具各 1 次；v1（M2）无工具时 AI 不得口头承诺发短信 |
| FR-5 | 外呼 | Web API / CLI 发起 dial，45s 接通超时 | 真机外呼 1 次成功 |
| FR-6 | 短信收发 | UCS2 中文、PDU 解码、+CMTI 上报（AA 现有能力回归） | 中英文短信收发各 1 次 |
| FR-7 | Web 仪表盘 | 通话状态、转写气泡、短信、预热进度条、配置面板 | 手工核对 |
| FR-8 | 会话记录 | events.jsonl + 分轮 wav 落盘，可配置关闭/保留期 | 通话后检查产物完整 |

## 5. 非功能需求

| 编号 | 需求 | 指标 |
|------|------|------|
| NFR-1 | 延迟 | local 流式版 speech_end→首帧下行 PCM p95 < 2.5s（M2 整句版 < 4s）；**所有数字须真机实测打点，不得引用估计值做结论**（S3 产出延迟预算表） |
| NFR-2 | 稳定性 | 主循环任何一轮 agent 异常不得导致进程崩溃；通话结束必须执行 ATH+音频通道关闭；USB 桥支持重插恢复 |
| NFR-3 | 非阻塞 | `CallSession` 音频主循环（10ms tick）内不得有 >50ms 的同步调用；STT/LLM/TTS 全部在 worker 线程/任务中执行 |
| NFR-4 | 安全 | 工具 policy 层：send_sms 白名单+频控；hangup 需显式意图+延迟窗；query_code 默认关闭；全部工具调用留审计日志，短信内容脱敏可配 |
| NFR-5 | 隐私 | 录音/转写默认本地存储，提供一键清除；`.env` 明文 key 不入 git |
| NFR-6 | 可移植 | 不新增 macOS 专属依赖；平台相关代码（USB 桥、音频设备）隔离在明确边界内 |
| NFR-7 | 可测试 | fake modem/bridge/agent 夹具支撑无硬件单测；核心状态机测试覆盖 |

## 6. 关键设计决策（含 codex 评审修正）

| # | 决策 | 理由 |
|---|------|------|
| D1 | **以 AA 为基座**整体迁入（vendor 拷贝，非依赖引用），poc 只搬平台无关模块 | AA 的 CallSession 与 modem 事件深耦合且真机已跑通；poc 的 hot_daemon（4883 行）混杂 Mac 逻辑，反向改造成本高。**codex 修正**：不是"丢弃 poc Mac 层"这么粗——精确保留 vad/stt_local/tts/llm_warmup/warmup_state/call_log/config_manager/outbound_agent(prompts)，丢弃 auto_answer/call_event_monitor/phonebridge/launch_agent/hot_daemon 播放注入段 |
| D2 | **LocalPipelineAgent 实现 VoiceAgent 接口**，作为第三 provider 注册进 factory | 与 qwen/doubao 并存可切换、可对比。**codex P1 修正**：`send_audio()` 严禁同步跑 STT→LLM→TTS（会卡死 `call_agent.py` 主循环）——只喂 VAD+入队，后台 worker 生成回复经输出队列推 PCM；补内部状态机 listening/processing/speaking/stopping、speech_end 去重、stop 时 flush/cancel、LLM/TTS 失败兜底话术 |
| D3 | **VAD 跑 8k**（`input_rate=8000`），segment 升 16k 喂 FunASR | Silero 原生支持 8k，省一次全程上采样；识别率和 endpoint 准确性由 S1 验证，不行则回退 16k 全链路或换 FunASR 8k 电话模型 |
| D4 | **思考期不冻结收音**：processing/speaking 期间继续喂 VAD，新 utterance 入队 | 冻结会丢字。**coalescing 语义（codex 定稿）**：worker 每次发起 LLM 请求前 drain 队列合并全部积压 utterance；LLM 请求已发出后到达的 utterance 排下一轮（不做取消/重启，那是 M4 barge-in 的事）。半双工抑制仅在 speaking 生效 |
| D5 | **TTS PCM adapter 新写** | poc 的 `SherpaOnnxTts.synthesize_chunks_to_wavs()` 落盘 wav + afplay 播放，AA 需要内存 PCM chunk。adapter：切句→合成 samples→int16 PCM→重采样 8k→按 chunk 推送 |
| D6 | **工具调用循环从零实现** | poc 的 `generate_phone_agent_reply()` 无 tools/tool_calls 支持。需：AA tool spec → OpenAI-compatible tools、解析 tool_calls、dispatch、二次请求生成口头确认；外加 policy 层（NFR-4） |
| D7 | **UAC playback-active 修正** | codex 发现：AA 的 `ModemAudioBridge.pending_output_bytes()` 恒为 0 且 `_drain_agent_audio()` 先写后判，UAC 下 speaking 判定低估播放尾音。M2 用「已写入字节数 ÷ (8000×2 B/s)」估算播放结束时刻，做 playback-active 时间窗；S2 真机验证 |
| D8 | **barge-in 推迟到 M4** | 依赖 S2 回声实测结论：若 AI 下行明显回采到上行，裸 VAD 打断会自触发，需先解决回声抑制或能量门限；v1 维持半双工 |

## 7. 风险与前置 Spike

| 风险 | 影响 | 对策（Spike） |
|------|------|--------------|
| 8kHz 电话音质下 FunASR 识别率不达标 | local provider 核心价值受损 | **S1**：用 modem 真实录音样本测 8k VAD endpoint + 16k 上采样识别率 + 8k 电话模型对比 + 三种重采样对比；不达标回退方案：换模型 / 云端 STT |
| EC20 线路回声导致半双工失效或打断误触发 | 对话体验差 / barge-in 不可行 | **S2**：AI 播放期间采集上行做回声测定；标定半双工 hangover 参数；验证 D7 的 UAC 判定 |
| 本地流水线延迟超预算（poc 数据是 Mac + Continuity 链路，非本链路） | 不满足 NFR-1 | **S3**：本链路分环节打点实测（VAD end→STT→LLM 首 token→TTS 首块→modem 写入），冷/热对比，产出延迟预算表 |
| macOS 无 EC20 原生驱动，USB→PTY 桥是单点 | 桥挂了全链路瘫 | M0 做桥生命周期管理：自动拉起、进程唯一、stale claim 清理、重插恢复（codex 提醒：不做会被环境问题反复打断验收） |
| 模型资产下载大（FunASR ~840MB+291MB）且首次加载慢 | 安装体验差、预热失败无提示 | M2 前置 `install_models.py`：下载/校验/缓存/预热自检/失败降级提示 |
| DeepSeek API 网络抖动 | 回合超时 | 沿用 poc 的超时预算（connect 2s / read 6s / total 8s）+ fallback 话术 + keepalive |

## 8. 参考

- 源码分析报告：本会话两份 Explore 深读报告（AA / iphone-call-ai-poc，含文件行号）
- codex 评审对话：`/Users/tianye/AgentCall/.codex_dialog.md`（3 轮，PASS with notes）
- 延迟基线：poc `README.md` TURN_1_TIMING 实测（2026-07-06，Mac 链路，仅作参考基线，本链路以 S3 为准）
- 任务拆解：`docs/02-task-breakdown.md`
