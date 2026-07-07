# AgentCall 需求与路线图（Realtime 优先，单一 SSOT）

> 版本：v2.0（2026-07-07）
> 本文合并原 `01-requirements.md` / `02-task-breakdown.md` / `03-gap-analysis-vs-poc.md`，
> 是当前唯一的计划文档。**本地三段式（VAD→STT→LLM→TTS）方案已整体延后**，本文只覆盖
> 云端 realtime 路线下要做的功能，按优先级组织。三段式方案见文末附录 A（暂缓）。
> codex 评审全过程留存于 `.codex_dialog.md`；旧版三份文档留存于 git 历史（commit 08754fc）。

---

## 1. 项目定位与现状

**AgentCall**：插上 Quectel EC20 4G 模组，来电自动接听并由 AI 对话，支持外呼、短信收发、
AI 工具调用（发短信/挂断/查验证码）。AI 定位为"机主（田野）的数字分身"——替接来电、代打外呼。

**当前技术栈（realtime）**：
```
手机来电 → EC20 模组 ──(AT: RING/ATA/CLCC)── Eg25Modem
                │                                │ 回调
            8kHz PCM                      CallAgentService / CallSession（单例，双向互斥）
                │                                │
         FfmpegAudioBridge(UAC, macOS) ── VoiceAgent(qwen | doubao)
                                                 │ 云端端到端语音 realtime
                                          EventHub → aiohttp 网页仪表盘
```
- AI 大脑：Qwen Omni / Doubao 云端 realtime（端到端语音，`server_vad` 云端切句），
  **不加载本地模型**，无预热问题。
- 音频：macOS 上经 `uac_ffmpeg` 模式（ffmpeg 绕过打不开的 PortAudio）；`nmea`/`uac`(PortAudio)
  在本机不可用（见 §6 决策）。
- 单一 `CallAgentService`+单 `CallSession` 处理来电与外呼，`_active` 互斥（同时仅一通）。

**已完成（M0 底座）**：AA 代码迁入并重组为 `src/agentcall/` 包 · USB→PTY 桥生命周期管理
（进程锁/重插重连/日志）· 27 个单测 + Fake 夹具 · 数字分身 prompt 层 · macOS UAC ffmpeg 音频桥 ·
真机来电已能双向通话（qwen）。**T0.4 真机连续通话验收待补最后 2-3 通**。

---

## 2. 功能需求（FR）

| 编号 | 需求 | 状态 |
|------|------|------|
| FR-1 | 来电自动接听（RING/CLCC→ATA，去重） | ✅ 已实现，真机通过 |
| FR-2 | AI 对话（qwen/doubao 可切换，数字分身人设） | ✅ qwen 真机通；doubao 待补 |
| FR-3 | 外呼（网页/CLI 发起，45s 接通超时，外呼专属 prompt） | ✅ 已实现 |
| FR-4 | 短信收发（UCS2 中文、PDU 解码、+CMTI 上报） | ✅ 迁移自 AA |
| FR-5 | AI 工具调用（发短信/挂断/查验证码，qwen 原生 function calling） | ✅ 已实现 |
| FR-6 | 网页仪表盘（状态/转写/短信/发短信/外呼） | ✅ 已实现 |
| FR-7 | **交付为桌面 App**（非浏览器网页） | ⬜ P1（见 3.2） |
| FR-8 | **短信转发到指定邮箱**（含验证码提取） | ⬜ P1（见 3.2） |
| FR-9 | **批量外呼**（一次多号码顺序拨打 + 白名单） | ⬜ P2（见 3.3） |
| FR-10 | 通话记录 / 录音 / 拨打历史 | ⬜ P0（见 3.1） |
| FR-11 | 通话后结构化总结（谁来过/何事/急否/是否回拨） | ⬜ P1（见 3.2） |

## 非功能需求（NFR）

- **NFR-1 稳定性**：任一通话异常不得使进程崩溃；通话结束必执行 ATH + 关 PCM 通道；
  USB 桥与 modem 连接支持断连自愈（见 3.1）。
- **NFR-2 非阻塞**：音频主循环（10ms tick）内无 >50ms 同步调用。
- **NFR-3 安全**：工具调用有 policy——发短信白名单+频控、挂断需显式意图+延迟窗、
  查验证码可配开关；工具调用留审计日志；短信内容脱敏可配。
- **NFR-4 隐私**：录音/转写默认本地存储、可一键清除；`.env`/SMTP 凭证不入 git。
- **NFR-5 可移植**：平台相关代码（USB 桥、音频后端）隔离在明确边界；不新增 Mac 专属强依赖。
- **NFR-6 可测试**：Fake modem/bridge/agent 夹具支撑无硬件单测。
- **NFR-7 生产数字实测**：延迟/识别率等以真机实测为准，不引用估计值下结论。

---

## 3. 待办需求（按优先级）

### 3.1 P0 —— 紧接 M0 收尾，直接影响真机稳定性与可调试性

**P0-1 modem 连接断连自愈**
- 现状：`CallAgentService.start()` 只 `modem.connect()` 一次。USB 桥重插后 `/tmp/ec20-at`
  symlink 指向新的 `/dev/ttysNNN`，app 旧 fd 变死句柄，读循环抛 SerialException 后线程退出不重连。
- 要做：读循环捕获断连→重开串口+重发初始化+重启监听线程；或加 supervisor 周期性健康检查。
- 验收：真机拔插 EC20 后，无需手动重启 app 即恢复接听能力。

**P0-2 通话记录 / 录音 / 延迟打点 / 拨打历史**
- 现状：`EventHub` 只持久化短信（`_PERSISTED_TYPES`），通话事件仅在内存 deque，刷新即丢。
- 要做：按通话落 `data/recordings/<ts>/`：events.jsonl（含各环节延迟打点）+ 可选录音 wav；
  网页加拨打历史列表；录音开关与保留期可配（隐私见 NFR-4）。
- 价值：排障/审计/复盘 + 为 §4 Spike 攒录音样本。
- 注：短信历史当前已具备（messages.json + 网页），本条只补通话侧。
- 验收：一通电话后产物齐全可回放；关闭开关后不落音频。

### 3.2 P1 —— 产品化：把"能接电话的脚本"变成"数字分身 App"

**P1-1 打包为桌面 App**（FR-7）
- 现状：`app.py` 起 aiohttp 后 `webbrowser.open`，用户看到浏览器标签页。
- 要做：pywebview（或等价）把现有 aiohttp 网页套进原生窗口——**后端全复用，成本低**；
  后续可 py2app/PyInstaller 打成可双击 .app。
- 验收：双击启动出现独立应用窗口，非浏览器；桥+服务随之拉起。

**P1-2 短信转发到邮箱**（FR-8）
- 现状：EC20 已收到短信（`+CMTI→on_sms→EventHub sms_in`）并展示/持久化，但不转发。
- 要做：把 `sms_in` 事件接到 SMTP mailer；验证码 OTP 抽取逻辑可移植 poc `sms_forward/body.py`；
  收件邮箱/SMTP 凭证/转发开关走 .env（凭证勿入 git）。
- 说明：**比 poc 简单**——poc 要读 iOS chat.db（Mac 专属），本项目模组直接给到短信事件。
- 验收：真机收一条含验证码短信，指定邮箱收到含号码/正文/验证码的邮件。

**P1-3 常驻 / 开机自启 / 崩溃重启**
- 要做：macOS launchd plist（RunAtLoad/KeepAlive/崩溃重启）+ Linux systemd unit 预留；
  桥与 app 两个单元。
- 验收：重启机器后自动待命接电话；kill 掉 app 后自动重启。

**P1-4 通话后结构化总结**（FR-11）
- 现状：通话结束只发 `ended` 事件，无总结。数字分身 prompt 已定位"记要点转告机主"，但无落地。
- 要做：通话结束用一次 LLM 请求（复用 realtime 转写全文）生成结构化摘要（对方/事由/紧急度/
  是否需回拨），落 EventHub + 网页展示。与 P0-2 配套。
- 验收：一通来电结束后，网页/记录里出现该通结构化摘要。

### 3.3 P2 —— 能力增强

**P2-1 批量外呼**（FR-9）
- 现状：`service.dial()` 仅单号，`CallSession` 单例互斥拒第二通。
- 要做：外呼队列（号码列表 + 每号可带独立 task）+ 顺序调度（一通结束再拨下一通）；
  外呼白名单（移植 poc `auto_dial_number_is_allowed`）+ 频控。与 P1-4 配套产出"每通结果"。
- 验收：填入多号码，逐个拨打并各自记录结果；白名单外号码被拒。

**P2-2 配置面板**
- 要做：网页加配置读写 API 与页面（provider、录音/工具/转发开关、半双工参数等白名单字段），
  写回 .env 保留注释；改 provider 提示重启。
- 验收：面板改一项→.env 变更正确→重启生效。

**P2-3 realtime 连接预热 / 降低首句延迟**
- 现状：realtime WS 在通话中才建立，接听→首句约 2-3s。
- 要做：服务空闲时预建/保活连接或至少预热 DNS/TLS，来电时复用（评估空连接成本/超时）。
- 验收：接听→首句延迟实测下降（打点对比）。

**P2-4 通话中断 fallback / 重连**
- 现状：realtime WS 中途掉线则整通结束，无兜底。
- 要做：断线检测 + 有限次重连；重连间隙播预置"稍等"提示音。
- 验收：通话中模拟断网，能恢复或优雅提示，不是突然沉默。

**P2-5 provider 凭证启动校验补全**
- 现状：`app.py` 只校验 qwen 的 `DASHSCOPE_API_KEY`。
- 要做：按所选 provider 完整校验凭证，缺失时启动即报清晰错误。

**P2-6 硬编码参数化**
- `HALF_DUPLEX_HANGOVER_SECONDS=0.5`、工具挂断延迟 `Timer(4.5)` → 提为可配；
  半双工挂尾值按 §4 S2 真机回声结论标定。

### 3.4 P3 —— 场景与打磨

- **P3-1 DTMF 按键**：EC20 走 `AT+VTS`（打客服菜单导航）。
- **P3-2 barge-in（打断）**：realtime 原生支持，当前被半双工压制；依赖 §4 S2 回声结论决定放开策略。
- **P3-3 音频治理**：上行增益/AGC（当前只有 `MODEM_TX_GAIN` 下行）、削波检测、静音填充参数化。
- **P3-4 通话中短信播报**：通话进行中收到短信时告知正在对话的 AI。
- **P3-5 doubao 能力对齐**：接数字分身差异化 prompt；工具调用（若官方支持）。

---

## 4. 真机 Spike（前置验证，非三段式）

每项产出报告到 `docs/spikes/`，为上面的需求提供实测依据：

- **S1 音频质量**：EC20 8k 采集/回放的语音清晰度与 realtime ASR 识别率；采样率/增益调优。
  *部分已有结论*：本机 macOS 仅 `uac_ffmpeg` 可用，NMEA over USB 崩溃、PortAudio UAC 打不开。
- **S2 回声与半双工**：AI 播放期间上行回采强度，标定半双工挂尾参数，判定 barge-in 可行路线
  （裸 VAD / 能量门限 / AEC）。*已观察到*：真机来电出现"AI 重复自我介绍"，疑回声/半双工截断。
- **S3 端到端延迟**：接听→首句、speech_end→回复各环节打点（realtime 首音频约 291ms 实测），
  识别预热/连接优化空间（喂给 P2-3）。

---

## 5. 测试与部署

- **测试补全**：状态机/音频桥/工具/policy 单测覆盖核心路径；`pytest` 一键跑（当前 27 绿）。
- **真机验收清单**（`docs/acceptance.md`）：来电自动接听×3、qwen 对话 5 轮×3 通、外呼×1、
  中英文短信收发、AI 主动挂断、30 分钟长通话稳定、拔插恢复。
- **部署文档**（`docs/deploy-macos.md`）：从零到可接电话（Python/依赖/USB 桥/常驻）；
  key 与 SMTP 凭证管理规范；Linux 差异预留。

---

## 6. 关键决策与现状事实（保留）

- **D1 以 AA 为基座**：AA 的 modem/音频/会话编排真机已跑通，整体迁入重组，未反向改造 poc。
- **D2 realtime 优先，三段式延后**：当前只走云端 realtime；本地三段式作为未来可选 provider（附录 A）。
- **D3 音频模式（实测结论）**：macOS 上 `uac_ffmpeg`（ffmpeg 绕过 PortAudio）为唯一可用路径；
  `uac`(PortAudio/sounddevice) 打不开 EC20 声卡（AUHAL -66740/-9986）；`nmea`(USB 串口 PCM)
  会触发接口崩溃重枚举。Windows/Linux 原生串口另行验证。
- **D4 UAC 下行时钟**：ffmpeg 播放侧按 100ms 实时节奏喂养、空闲补静音，避免 underrun
  与输出流迟建（经 codex review）。
- **D5 每通电话重启语音通道**：挂断会 `AT+QPCMV=0`，每通接听前须重发 `initialize_for_voice`，
  否则第二通起双向无声。
- **D6 barge-in 延后**：依赖 S2 回声实测，v1 维持半双工抑制。
- **D7 工具安全 policy（NFR-3）**：发短信白名单+频控、挂断显式意图+延迟窗、查验证码可配、审计日志。

---

## 附录 A：暂缓的三段式本地大脑（未来）

未来若要"本地可控、隐私优先、可离线"的 AI 大脑，再启用本地三段式方案：把
iphone-call-ai-poc 的 Silero VAD → FunASR → DeepSeek → sherpa-onnx 包装成 `LocalPipelineAgent`，
实现 `VoiceAgent` 接口作为第三 provider `local`，与 qwen/doubao 并存可切换。

关键约束（codex 已评审，勿忘）：`send_audio()` 必须非阻塞（后台 worker 跑 STT→LLM→TTS，
utterance 合并语义）、TTS 需 PCM adapter、DeepSeek function calling 需从零实现、需模型资产
安装与预热。**届时 STT 预热、模型安装、keepalive 等问题随之回归**。

完整设计与任务拆解见 git 历史 `docs/01-requirements.md` / `docs/02-task-breakdown.md`
（commit 08754fc）与 `.codex_dialog.md` 的三轮评审记录。
