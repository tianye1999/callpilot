# CallPilot 需求与路线图（Realtime 优先，单一 SSOT）

> 版本：v2.1（2026-07-07）
> 本文合并原 `01-requirements.md` / `02-task-breakdown.md` / `03-gap-analysis-vs-poc.md`，
> 是当前唯一的计划文档。**本地三段式（VAD→STT→LLM→TTS）方案已整体延后**，本文只覆盖
> 云端 realtime 路线下要做的功能，按优先级组织。三段式方案见文末附录 A（暂缓）。
> codex 评审全过程留存于 `.codex_dialog.md`；旧版三份文档留存于 git 历史（commit 08754fc）。

## 0. 发布路线（开源 + 商业化免费）

| 版本 | 目标 | 面向 | 状态 |
|------|------|------|------|
| **v0.1 Developer Preview** | 源码开源，开发者按 README 手动跑通，收集同型号 EC20 硬件反馈 | 会装 Python/填 Key/跑命令的开发者 | 🚧 本轮交付：Apache-2.0、去个人化、双语 README、风险声明、仓库清理 |
| **v0.2 Mac Beta** | 有 `.app`，仍需装依赖或跑安装脚本 | 技术用户 | 计划 |
| **v0.3 One-click Mac Beta** | 真正 `.pkg` 安装、首次启动向导、内置 runtime、自动 launchd、Developer ID 签名+公证 | 普通用户 | 计划 |
| **v1.0** | 稳定硬件矩阵、隐私说明、故障恢复、完整文档 | 通用 | 计划 |

v0.1 原则（codex 建议，已采纳）：不追求一键安装，追求"陌生开发者 30 分钟能跑通、能提
issue、能贡献"；先让 3-5 个同硬件开发者复现成功，再进大众分发。

**v0.3 独立 App 尚缺**（当前 `.app` 是本地仓库薄壳）：内置 Python runtime、固定安装路径、
自动建 venv/装依赖、自动装卸 launchd、首启向导（检测 EC20 / 填 Key / 设机主名 / 选音色 /
测麦克风与下行 / 测试短信）、硬件兼容矩阵、Developer ID 签名与 notarization。

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
| FR-9 | **批量外呼**（一次多号码顺序拨打 + 白名单） | ⬜ P2（见 3.3） |
| FR-10 | 通话记录 / 录音 / 拨打历史 | ⬜ P0（见 3.1） |
| FR-11 | 通话后结构化总结（谁来过/何事/急否/是否回拨） | ⬜ P1（见 3.2） |

> 短信转发到邮箱：应用户要求**延后到未来**，见附录 B。

## 非功能需求（NFR）

- **NFR-1 稳定性**：任一通话异常不得使进程崩溃；通话结束必执行 ATH + 关 PCM 通道；
  USB 桥与 modem 连接支持断连自愈（见 3.1）。
- **NFR-2 非阻塞**：音频主循环（10ms tick）内无 >50ms 同步调用。
- **NFR-3 安全**：工具调用有 policy——发短信白名单+频控、挂断需显式意图+延迟窗、
  查验证码可配开关；工具调用留审计日志；短信内容脱敏可配。
- **NFR-4 隐私**：录音/转写默认本地存储、可一键清除；`.env` 与各类凭证不入 git。
- **NFR-5 可移植**：平台相关代码（USB 桥、音频后端）隔离在明确边界；不新增 Mac 专属强依赖。
- **NFR-6 可测试**：Fake modem/bridge/agent 夹具支撑无硬件单测。
- **NFR-7 生产数字实测**：延迟/识别率等以真机实测为准，不引用估计值下结论。

---

## 3. 待办需求（按优先级）

### 3.1 P0 —— 紧接 M0 收尾，直接影响真机稳定性与可调试性

**P0-0 下行到电话端可闻 —— ✅ 已验证通过（2026-07-07）**
- 真人拨打 AI 号码，`uac_ffmpeg` 模式下电话端**清晰听到 AI**、音质佳。下行链路成立。
- 结合此前上行已证实（拨 10000 IVR 被准确转写），**双向语音在 uac_ffmpeg 模式下均通**。
- 结论：`uac_ffmpeg` 为确定可用的音频路径（双向）；nmea 崩 USB，不用。

**P0-1 modem 连接断连自愈**（✅ 已完成，commit 3fab59d）
- USB 桥重插后 `/tmp/ec20-at` 指向新 `/dev/ttysNNN`、app 旧 fd 变死句柄；已实现读循环/发送
  失败触发重连（指数退避、多线程串行化、初始化期防死锁），真机验证桥重连后自愈。

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

> P1-2 短信转发到邮箱：**应用户要求延后**，移至附录 B。

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

**P2-7 AI 下行音频镜像到 Mac 扬声器（本机监听）**
- poc 有：`O2_MONITOR_AI_PLAYBACK`（"在电脑上听 AI 说话"）+ `O2_HUMAN_OUTPUT`（输出设备）
  + `MONITOR_AI_GAIN`；AgentCall 当前只把 AI 音频写给 modem bridge，无本机监听。
- 要做：`MONITOR_AI_PLAYBACK=true/false` + `MONITOR_OUTPUT_DEVICE`；用**后台队列/线程**把下行
  PCM 同时播到 Mac 指定输出设备，**绝不阻塞通话主循环**（沿用 P0-2 的非阻塞原则）。
- 双重价值：① 运维时人能实时听到 AI 在说什么；② **诊断 P0-0 的关键工具**——把"AI 有没有
  出声"（Mac 扬声器能听到）与"电话端有没有收到"（对端能听到）分离，快速锁定下行故障在
  TTS 侧还是 EC20 侧。
- 验收：开启后 Mac 扬声器与电话下行同步听到 AI；关闭则静默；通话主循环 tick 不受影响。

### 3.4 P3 —— 场景与打磨

- **P3-1 DTMF 按键**：EC20 走 `AT+VTS`（打客服菜单导航）。
- **P3-2 barge-in（打断）**：realtime 原生支持，当前被半双工压制；依赖 §4 S2 回声结论决定放开策略。
- **P3-3 音频治理**：上行增益/AGC（当前只有 `MODEM_TX_GAIN` 下行）、削波检测、静音填充参数化。
- **P3-4 通话中短信播报**：通话进行中收到短信时告知正在对话的 AI。
- **P3-5 doubao 能力对齐**：接数字分身差异化 prompt；工具调用（若官方支持）。
- **P3-6 前端 XSS 加固**：index.html 少量 innerHTML 拼接改全 DOM 节点+textContent（codex review P2）。
- **P3-7 launchd plist 模板化**：install.sh 安装时替换当前项目路径，去除硬编码（codex review P2）。
- **P3-8 web 挂断按钮/API**：当前无法从网页结束进行中的通话（真机自测发现：AI 与 IVR 互致问候死循环时只能杀进程）。加 POST /api/call/hangup → session.stop()。
- **P3-9 外呼收束 prompt 调优**：带明确 task 的外呼完成后 AI 未主动调用 hangup_call 工具（对方持续说话时无限应答）。需在外呼指令中强化"目的达成即道别挂断"。

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

- **测试补全**：状态机/音频桥/工具/policy 单测覆盖核心路径；`pytest` 一键跑（当前 29 绿）。
- **真机验收清单**（`docs/acceptance.md`）：来电自动接听×3、qwen 对话 5 轮×3 通、外呼×1、
  中英文短信收发、AI 主动挂断、30 分钟长通话稳定、拔插恢复。

### 5.1 可自主执行的验证手段（无需人工在环）

用真实可交互对端做端到端自测，Claude 可自己发起并从转写/事件判定结果：

- **外呼 10000（电信 IVR）验证双向语音链路**：电信客服 IVR 自带语音识别，能与之**多轮
  对话**（如说"查余额/查流量"，IVR 按语义推进菜单）就证明——① 下行通（IVR 听到了 AI 说话）、
  ② 上行通（AI 听到了 IVR）、③ AI 对话逻辑正常。判定依据：转写里 `上行·用户`(IVR 提示)
  与 `下行·Agent`(AI 应答) 形成连贯推进，且 IVR 响应与 AI 所说一致。
  *触发*：`curl -X POST http://127.0.0.1:8000/api/call/dial -d '{"number":"10000"}'`。
- **发短信"查余额"到 10000 验证短信收发**：发出后 10000 会回一条余额短信 →
  验证 `send_sms`(发) + `+CMTI→on_sms→sms_in`(收) + 中文 UCS2 编解码 全链路。
  判定依据：EventHub 出现 `sms_out`(status=sent) 且随后收到 `sms_in`(余额内容)。
- 说明：这两条把大量"电话/短信行为"验证从"必须人工在环"降为"Claude 可自证"，仅音质主观
  好坏、真人对话自然度仍需人耳确认。

- **部署文档**（`docs/deploy-macos.md`）：从零到可接电话（Python/依赖/USB 桥/常驻）；
  key 管理规范；Linux 差异预留。

---

## 6. 关键决策与现状事实（保留）

- **D1 以 AA 为基座**：AA 的 modem/音频/会话编排真机已跑通，整体迁入重组，未反向改造 poc。
- **D2 realtime 优先，三段式延后**：当前只走云端 realtime；本地三段式作为未来可选 provider（附录 A）。
- **D3 音频模式（截至目前的实测状态，注意下行未验证）**：
  - 已证实：`uac`(PortAudio/sounddevice) 在本机**打不开** EC20 声卡（AUHAL -66740/-9986，
    coreaudiod 异常时更甚）；`nmea`(USB 串口 PCM) 在本机会**触发 USB 接口崩溃重枚举**
    （18:01/18:25 多次复现），无法持续通话。
  - 已证实：`uac_ffmpeg`（ffmpeg 绕过 PortAudio）**稳定不崩**，且**双向语音均通**——上行
    (拨 10000 IVR 被 qwen 准确转写) 与下行(真人拨 AI 号码清晰听到 AI，2026-07-07 验证) 都通过。
  - 现结论：`uac_ffmpeg` 为 macOS 上**确定可用的默认路径**；`uac`(PortAudio)/`nmea` 保留为
    诊断/备用（nmea 崩 USB，勿用）。Windows/Linux 原生串口另行验证。
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

---

## 附录 B：暂缓的短信转发到邮箱（未来）

应用户要求延后。未来实现时：EC20 已收到短信（`+CMTI→on_sms→EventHub sms_in`），只需把
`sms_in` 事件接到一个 SMTP mailer 即可（比 poc 简单——poc 要读 iOS chat.db，本项目模组直接
给到短信事件）；验证码 OTP 抽取逻辑可移植 poc 的 `sms_forward/body.py`。收件邮箱、SMTP 凭证、
转发开关走 .env（凭证勿入 git）。验收：真机收一条含验证码短信 → 指定邮箱收到含号码/正文/
验证码的邮件。
