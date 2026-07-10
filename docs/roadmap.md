# CallPilot 需求与路线图（Realtime 优先，单一 SSOT）

> 版本：v2.1（2026-07-07）
> 本文合并原 `01-requirements.md` / `02-task-breakdown.md` / `03-gap-analysis-vs-poc.md`，
> 是当前唯一的计划文档。**本地三段式（VAD→STT→LLM→TTS）方案已整体延后**，本文只覆盖
> 云端 realtime 路线下要做的功能，按优先级组织。三段式方案见文末附录 A（暂缓）。
> codex 评审全过程留存于 `.codex_dialog.md`；旧版三份文档留存于 git 历史（commit 08754fc）。
>
> **状态更新（v0.2，2026-07-08）**：FR-7/9/10/11（桌面 App、批量外呼、通话记录/录音、
> 通话后摘要）、首启向导、DMG 安装包、P0/P1/P2 主线与 §3 多数 P3 打磨项已回填状态；
> provider 由 qwen/doubao 扩展为 **qwen（默认）/ doubao / openai**。

## 0. 发布路线（开源 + 商业化免费）

| 版本 | 目标 | 面向 | 状态 |
|------|------|------|------|
| **v0.1 Developer Preview** | 源码开源，开发者按 README 手动跑通，收集同型号 EC20 硬件反馈 | 会装 Python/填 Key/跑命令的开发者 | 🚧 本轮交付：Apache-2.0、去个人化、双语 README、风险声明、仓库清理 |
| **v0.2 Mac Beta** | 有 `.app`，仍需装依赖或跑安装脚本 | 技术用户 | ✅ 已落地 |
| **v0.3 One-click Mac Beta** | `.dmg` 安装、首次启动向导、内置 runtime、自动 launchd、Developer ID 签名+公证 | 普通用户 | ✅ 已落地（v0.4.0 起 DMG 已签名+公证+staple） |
| **v1.0** | 稳定硬件矩阵、隐私说明、故障恢复、完整文档 | 通用 | 计划 |

v0.1 原则（codex 建议，已采纳）：不追求一键安装，追求"陌生开发者 30 分钟能跑通、能提
issue、能贡献"；先让 3-5 个同硬件开发者复现成功，再进大众分发。

**v0.3 独立 App 状态**：`packaging/build_installer.sh` 已能产出内置 Python runtime、
ffmpeg/libusb、自动 launchd 与首启向导的 `CallPilot.dmg`；固定安装路径通过 `/Applications`
推荐路径与 per-user Application Support 数据目录处理。Developer ID 签名与 notarization
已随 v0.4.0 落地；仍缺硬件兼容矩阵完善。

---

## 1. 项目定位与现状

**AgentCall**：插上 Quectel EC20 4G 模组，来电自动接听并由 AI 对话，支持外呼、短信收发、
AI 工具调用（发短信/挂断/查验证码）。AI 定位为"机主的数字分身"——替接来电、代打外呼。

**当前技术栈（realtime）**：
```
手机来电 → EC20 模组 ──(AT: RING/ATA/CLCC)── Eg25Modem
                │                                │ 回调
            8kHz PCM                      CallAgentService / CallSession（单例，双向互斥）
                │                                │
         FfmpegAudioBridge(UAC, macOS) ── VoiceAgent(qwen | doubao | openai)
                                                 │ 云端端到端语音 realtime
                                          EventHub → aiohttp 网页仪表盘
```
- AI 大脑：Qwen Omni / Doubao / OpenAI 云端 realtime（端到端语音，`server_vad` 云端切句），
  **不加载本地模型**，无预热问题。
- 音频：macOS 上经 `uac_ffmpeg` 模式（ffmpeg 绕过打不开的 PortAudio）；`nmea`/`uac`(PortAudio)
  在本机不可用（见 §6 决策）。
- 单一 `CallAgentService`+单 `CallSession` 处理来电与外呼，`_active` 互斥（同时仅一通）。

**已完成（M0 底座）**：AA 代码迁入并重组为 `src/agentcall/` 包 · USB→PTY 桥生命周期管理
（进程锁/重插重连/日志）· 完整离线单测 + Fake 夹具 · 数字分身 prompt 层 · macOS UAC ffmpeg 音频桥 ·
真机来电已能双向通话（qwen）。**T0.4 真机连续通话验收待补最后 2-3 通**。

---

## 2. 功能需求（FR）

| 编号 | 需求 | 状态 |
|------|------|------|
| FR-1 | 来电自动接听（RING/CLCC→ATA，去重） | ✅ 已实现，真机通过 |
| FR-2 | AI 对话（qwen/doubao/openai 可切换，数字分身人设） | ✅ qwen 真机通；openai 已接入；doubao 待补 |
| FR-3 | 外呼（网页/CLI 发起，45s 接通超时，外呼专属 prompt） | ✅ 已实现 |
| FR-4 | 短信收发（UCS2 中文、PDU 解码、+CMTI 上报） | ✅ 迁移自 AA |
| FR-5 | AI 工具调用（发短信/挂断/查验证码，qwen 原生 function calling） | ✅ 已实现 |
| FR-6 | 网页仪表盘（状态/转写/短信/发短信/外呼） | ✅ 已实现 |
| FR-7 | **交付为桌面 App**（非浏览器网页） | ✅ 已实现（`desktop_app.py` + 托盘 `tray_app.py` + 独立安装包 `packaging/build_installer.sh`；签名公证已落地） |
| FR-9 | **批量外呼**（一次多号码顺序拨打 + 白名单） | ✅ 已实现（`dial_queue.py` + `/api/call/batch_dial`） |
| FR-10 | 通话记录 / 录音 / 拨打历史 | ✅ 已实现（`call_log.py` + `/api/history`） |
| FR-11 | 通话后结构化总结（谁来过/何事/急否/是否回拨） | ✅ 已实现（`summarizer.py`，CallSession 调度） |

> 短信转发到邮箱：应用户要求**延后到未来**，见附录 B。

## 非功能需求（NFR）

- **NFR-1 稳定性**：任一通话异常不得使进程崩溃；通话结束必执行 ATH + 关 PCM 通道；
  USB 桥与 modem 连接支持断连自愈（见 3.1）。
- **NFR-2 非阻塞**：音频主循环（10ms tick）内无 >50ms 同步调用。
- **NFR-3 安全**：工具调用有 policy——发短信白名单+频控、挂断需显式意图+延迟窗、
  查验证码可配开关；工具调用留审计日志；短信内容脱敏可配。

  > ✅ **实现状态（2026-07-08）**：发短信目标限制已改为「只能回复已联系过号码/当前通话对端」
  > （`contacts.py`，Web 与 AI 工具共用策略）；AI 工具与 Web `/api/sms/send` 共用进程内滑动窗口频控
  > `SMS_RATE_LIMIT_PER_HOUR`（0=不限）；`TOOL_QUERY_CODE_ENABLED` 可关闭验证码工具注册；
  > `send_sms`/`hangup_call`/`query_verification_code` 已补 `tool_call` 审计且不落短信全文/验证码值。
  > 挂断工具延迟窗与 `send_dtmf` 审计也已落地。
- **NFR-4 隐私**：录音/转写默认本地存储、可一键清除；`.env` 与各类凭证不入 git。

  > ✅ **实现状态**：本地存储 ✅、`.env` 不入 git ✅、`RECORDING_RETENTION_DAYS` 自动过期清理 ✅；
  > 历史页与 REST API 已支持单条删除及一键清除全部，进行中的通话记录会跳过。
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

**P0-2 通话记录 / 录音 / 延迟打点 / 拨打历史 —— ✅ 已完成**
- 现状：`EventHub` 只持久化短信（`_PERSISTED_TYPES`），通话事件仅在内存 deque，刷新即丢。
- 要做：按通话落 `data/recordings/<ts>/`：events.jsonl（含各环节延迟打点）+ 可选录音 wav；
  网页加拨打历史列表；录音开关与保留期可配（隐私见 NFR-4）。
- 价值：排障/审计/复盘 + 为 §4 Spike 攒录音样本。
- 注：短信历史当前已具备（messages.json + 网页），本条只补通话侧。
- 验收：一通电话后产物齐全可回放；关闭开关后不落音频。

### 3.2 P1 —— 产品化：把"能接电话的脚本"变成"数字分身 App"

**P1-1 打包为桌面 App —— ✅ 已完成**（FR-7）
- 现状：`app.py` 起 aiohttp 后 `webbrowser.open`，用户看到浏览器标签页。
- 要做：pywebview（或等价）把现有 aiohttp 网页套进原生窗口——**后端全复用，成本低**；
  后续可 py2app/PyInstaller 打成可双击 .app。
- 验收：双击启动出现独立应用窗口，非浏览器；桥+服务随之拉起。

> P1-2 短信转发到邮箱：**应用户要求延后**，移至附录 B。

**P1-3 常驻 / 开机自启 / 崩溃重启 —— ✅ 已完成**
- 要做：macOS launchd plist（RunAtLoad/KeepAlive/崩溃重启）+ Linux systemd unit 预留；
  桥与 app 两个单元。
- 验收：重启机器后自动待命接电话；kill 掉 app 后自动重启。

**P1-4 通话后结构化总结 —— ✅ 已完成**（FR-11）
- 现状：通话结束只发 `ended` 事件，无总结。数字分身 prompt 已定位"记要点转告机主"，但无落地。
- 要做：通话结束用一次 LLM 请求（复用 realtime 转写全文）生成结构化摘要（对方/事由/紧急度/
  是否需回拨），落 EventHub + 网页展示。与 P0-2 配套。
- 验收：一通来电结束后，网页/记录里出现该通结构化摘要。

### 3.3 P2 —— 能力增强

**P2-1 批量外呼 —— ✅ 已完成**（FR-9）
- 现状：`service.dial()` 仅单号，`CallSession` 单例互斥拒第二通。
- 要做：外呼队列（号码列表 + 每号可带独立 task）+ 顺序调度（一通结束再拨下一通）；
  外呼白名单（移植 poc `auto_dial_number_is_allowed`）+ 频控。与 P1-4 配套产出"每通结果"。
- 验收：填入多号码，逐个拨打并各自记录结果；白名单外号码被拒。

**P2-2 配置面板 —— ✅ 已完成**
- 要做：网页加配置读写 API 与页面（provider、录音/工具/转发开关、半双工参数等白名单字段），
  写回 .env 保留注释；改 provider 提示重启。
- 验收：面板改一项→.env 变更正确→重启生效。

**P2-3 realtime 连接预热 / 降低首句延迟 —— ✅ 已完成**
- 现状：realtime WS 在通话中才建立，接听→首句约 2-3s。
- 要做：服务空闲时预建/保活连接或至少预热 DNS/TLS，来电时复用（评估空连接成本/超时）。
- 验收：接听→首句延迟实测下降（打点对比）。

**P2-4 通话中断 fallback / 重连 —— ✅ 已完成**
- 现状：realtime WS 中途掉线则整通结束，无兜底。
- 要做：断线检测 + 有限次重连；重连间隙播预置"稍等"提示音。
- 验收：通话中模拟断网，能恢复或优雅提示，不是突然沉默。

**P2-5 provider 凭证启动校验补全 —— ✅ 已完成**
- 现状：`app.py` 只校验 qwen 的 `DASHSCOPE_API_KEY`。
- 要做：按所选 provider 完整校验凭证，缺失时启动即报清晰错误。

**P2-6 硬编码参数化 —— ✅ 已完成**
- `HALF_DUPLEX_HANGOVER_SECONDS=0.5`、工具挂断延迟 `Timer(4.5)` → 提为可配；
  半双工挂尾值按 §4 S2 真机回声结论标定。

**P2-7 AI 下行音频镜像到 Mac 扬声器（本机监听）—— ✅ 已完成**
- poc 有：`O2_MONITOR_AI_PLAYBACK`（"在电脑上听 AI 说话"）+ `O2_HUMAN_OUTPUT`（输出设备）
  + `MONITOR_AI_GAIN`；AgentCall 当前只把 AI 音频写给 modem bridge，无本机监听。
- 要做：`MONITOR_AI_PLAYBACK=true/false` + `MONITOR_OUTPUT_DEVICE`；用**后台队列/线程**把下行
  PCM 同时播到 Mac 指定输出设备，**绝不阻塞通话主循环**（沿用 P0-2 的非阻塞原则）。
- 双重价值：① 运维时人能实时听到 AI 在说什么；② **诊断 P0-0 的关键工具**——把"AI 有没有
  出声"（Mac 扬声器能听到）与"电话端有没有收到"（对端能听到）分离，快速锁定下行故障在
  TTS 侧还是 EC20 侧。
- 验收：开启后 Mac 扬声器与电话下行同步听到 AI；关闭则静默；通话主循环 tick 不受影响。

### 3.4 P3 —— 场景与打磨

- **P3-1 DTMF 按键 —— ✅ 已完成**：EC20 走 `AT+QVTS`，失败回退 `AT+VTS`（打客服菜单导航）。
- **P3-2 barge-in（打断）**：realtime 原生支持，当前被半双工压制；依赖 §4 S2 回声结论决定放开策略。
- **P3-3 音频治理**：上行增益/AGC（当前只有 `MODEM_TX_GAIN` 下行）、削波检测、静音填充参数化。
- **P3-4 通话中短信播报**：通话进行中收到短信时告知正在对话的 AI。
- **P3-5 doubao 能力对齐**：接数字分身差异化 prompt；工具调用（若官方支持）；
  实现 `say()` 外呼开场白——当前未能从现有代码/依赖确认豆包 realtime 协议中
  与 qwen `create_response` 等价的文本指令注入消息格式，`say()` 暂为 no-op，
  **豆包 provider 外呼 AI 不会主动开口**（factory 创建时有 warning 提示），
  外呼请用 qwen；需查阅火山引擎官方协议文档确认消息格式后补齐。
- **P3-6 前端 XSS 加固 —— ✅ 已完成**：index.html 已移除 HTML 注入 API，用户可控内容走 DOM 节点+textContent。
- **P3-7 launchd plist 模板化 —— ✅ 已完成**：install.sh/打包 App 安装时按当前路径生成 launchd 配置，去除硬编码。
- **P3-8 web 挂断按钮/API —— ✅ 已完成**：已加 POST /api/call/hangup → session.stop()。
- **P3-9 外呼收束** —— ✅ 已改为 **LLM 收尾裁判**（`summarizer.judge_wrap_up`，每 ~15s 让 qwen-plus 看转写+目标判「继续/收尾」）：治「打转」（原地重复/菜单绕圈→提前收尾）与「太早撤」（对方正在查询→继续等），非关键词枚举；150s 硬时限保留兜底。原「在 prompt 里强化收束」思路已弃（模型不可靠、且属枚举式微调）。
- **P3-10 动态场景提示词生成 —— ✅ 已完成**（2026-07-09）：按（号码+任务+语言）用轻量文本模型（跟随 `AGENT_PROVIDER`：qwen→qwen-plus、openai→gpt-4o-mini，`PROMPT_GEN_MODEL` 可覆盖）为每通生成一段「场景与开场策略」+ 一句 opening，替代单一通用模板，延续「讲场景不列规则」思路。四条硬约束全部落地：
  1. **不变量固定、不交给生成**：立场（机主名下的事、对方是协助方）、安全边界、可用工具、"办完即收尾" 保持模板原文；只生成"这通什么场景、要不要自我介绍、第一句说什么"那段。代码内**无任何号码→机构类型映射表**，类型判断全交给模型。
  2. **模板永远作默认/兜底**：生成失败/超时/关闭/凭证无效 → `build_instructions` 行为与改动前逐字节一致（回归测试保证）。
  3. **延迟藏进拨号窗口**：拨号即后台线程生成、接通时限时取用（`PROMPT_GEN_WAIT_SECONDS` 默认 3s），(号码+任务+语言) 进程内 64 条 LRU 缓存；生成线程异常绝不冒泡到通话主流程。
  4. **可调试**：每通生成的 scenario/opening 记入 `events.jsonl` 的 `prompt_gen` 事件 + 日志摘要。
  - **关键根因修复**：realtime `create_response(instructions=)` 是**叠加**在 session 指令上、非替换；原模板 outbound 段硬编码"开头自我介绍"，压过动态 opening。改为：scenario 存在时该句条件化为"按场景策略决定要不要自我介绍"，scenario 为空保持原句。真机拨 10000 验证：开场第一句变为"正在帮您查询流量使用情况"，不再"我是X的数字分身"循环、不再主动报时间戳。
  - **隐私 policy（显式接受）**：任务文本会发给轻量模型、scenario/opening 落 `events.jsonl` 明文——与整通语音本就全程发给同厂商 realtime 模型、逐字稿本就落同一录音文件属同一数据流，**不新增泄露面**；`events.jsonl` 为本地录音目录文件，不提交、不外传。
- **P3-14 语音识别型 IVR 长菜单跑偏**（观察项，2026-07-09 真机发现）：对"持续播报长选项列表"的语音识别 IVR（如 10000 念"查话费/查套餐/故障报修等，您请说"），realtime 模型会把菜单播报当成对方在提问、顺着每个选项应答（"帮您反馈故障报修"），偏离原任务；并伴随立场漂移（说成"帮您查"而非"帮机主查"）。与 P3-12 同源（机器 IVR 的轮次兜底行为），提示词层难根治；候选同 P3-12 方案②（手动应答控制）或识别"对方仍在念菜单"时抑制应答。当前不阻塞，先记录。**（2026-07-09 稳定性采集补充：8 轮拨 10000 有 1 轮出现完整请求句原文 ×3（"您好，麻烦帮我查一下这个号码的流量使用情况…"），即语音识别 IVR 反复催话下的真实复读，发生率 1/8；礼貌短语"您好"多次出现属正常应答已从回归断言中豁免（≤4 字）。）**
- **P3-15 开场偶发幻觉编造结果**（观察项，2026-07-09 真机发现，偶发未稳定复现）：一通拨 10000 开场白 AI 直接编造了完整查询结果（"当前套餐剩余可用流量为 45GB，本月已使用 12.3GB"），此时对方（IVR）尚未回答任何内容。同批次另两通开场正常、未编造，属偶发。推测与 create_response 的 opening/scenario instructions 下模型放飞脑补有关。对电话助手是可信度隐患（不得编造未从对方获得的信息）。待复现确认后从提示词层强化"未从对方拿到的数据不得编造"，或收敛开场 instructions 的自由度。当前偶发、不阻塞。**（2026-07-09 下午更新：编造在 qwen「现编提示词」时高发、预置提示词时未见——见 P3-16。已从 common 提示词层加固"不得编造未获得的结果"，B2-4 预调教任务库进一步治本。）** **（2026-07-09 稳定性采集定性：regression_call.py 连拨 10000×8 轮（预设命中），断言"不编造数值"8/8 PASS——加固+预设组合下幻觉 0 复现。转为已缓解，常规回归脚本持续看护，不再单独跟踪。）**
- **P3-16 预调教任务库 —— ✅ 已完成（B2-4，2026-07-09）**：对固定 (号码+任务) 用用户预置的精调提示词，替代每次现场生成（P3-10 动态生成）。`number_profiles.py` 分层匹配 (号码+任务) 精确 > (号码) 通配 > 动态生成兜底；命中跳过轻量模型调用、直接注入 scenario/opening、events 标 source=profile；预设库独立于 `PROMPT_GEN_ENABLED`（可只用预设、关掉现编）。用户可编辑 `data/number_profiles.json`（gitignore），仓库仅占位示例，非枚举/不写死。
  - **关键实测结论（provider 对比 + 立场问题定性）**：qwen-omni 在开场立场上不稳——「现编提示词」时间歇性冒充客服（"是查流量对吧？我帮您操作给您结果"）或冒充机主本人；OpenAI(gpt-realtime-mini) 立场也小漂（"我是田野"），但**明显优势是会真的调用 send_dtmf 按键**（qwen 十几通只说"我按X"从不调工具）。**换模型治标、预设库治本**：同一 qwen 换成 (10000,咨询流量) 预置提示词后，开场"你好，我想查一下这个号码的流量使用情况"立场稳定、不冒充、对方未给数据时诚实追问而非编造。说明立场漂移主因是「现编不稳」而非模型能力。默认仍 qwen（成本），常打号码靠预设库稳住。
  - **B2-5 拨号界面下拉**：从预调教库选预设（选中即精确命中，避免手输"查信用卡账单"vs"查询信用卡账单"对不齐漏配），保留"自定义"手输走动态兜底；`GET /api/number_profiles` 供下拉渲染。
  - **B2-6 完整双语**：label/task/scenario/opening 各字段可为字符串或 {zh,en}；scenario/opening 跟通话语言(AGENT_LANGUAGE)、label/task 跟界面语言、缺失回退；task 双语匹配（中/英输入都命中同一预设）。本地已配 10 个真实公开热线（运营商 10000/10086/10010、银行 95588/95533/95555/95566、12315、12345）双语预设。独立 review APPROVE、零缺陷；补畸形输入不抛异常回归测试。
  - **B2-8 任务库管理 —— ✅ 已完成（#8，2026-07-10）**：新增中英双语「任务库」页面与本地 JSON CRUD API，支持新建/编辑/复制/启停/删除、精确任务与号码通配模式；稳定 profile id 取代易漂移的任务字符串作为拨号选择身份，旧 `preset_task` 与无 id JSON 保持兼容。写入经进程内写锁、字段/冲突校验、`fsync` + 原子替换，管理内容仍只落用户数据目录，不引入数据库或新依赖。
  `summarizer.judge_wrap_up` 收尾裁判把关，要求模型在实质目标未达成前拉回话题/追问结果；
  保持非枚举实现，不列客套话词表。
- **P3-12 抑制对机器 IVR 的重复应答**（结构性，非提示词/非枚举）：真机现象——对方是"一直播报"的自动语音时，server VAD 把它切成很多轮，实时模型每轮都被迫回话、没新内容就复读开场白（"您好我是X想咨询…"连说 5-6 遍）。**提示词层面压不住**（属模型对机器 IVR 的轮次兜底行为，非措辞问题）。候选结构性方案：
  1. **播出前抑制与上一句近乎相同的应答 —— ✅ 方案①已完成**（用文本相似度判重，非关键词表；当前作为轻量缓解）。
  2. **改用手动应答控制 —— ✅ 已完成（默认关，2026-07-09）**：真机试探确认 qwen-omni 认 OpenAI-style `turn_detection.create_response=false`——server VAD 照常断句+ASR，但不自动生成回复，于是走**半自动**（不必自己做端点检测）：`MANUAL_RESPONSE_CONTROL=true` 时注入该字段接管"何时开口"，靠**静默合并 debounce**（纯时序、不看内容、非枚举）——对方一段转写后启动 `MANUAL_RESPONSE_SILENCE_MS`(默认 1000) 定时器，窗口内又来转写则重置（连续播报合并成一段），静默到期才手动 `create_response()`；`MANUAL_RESPONSE_MAX_WAIT_MS`(默认 8000) 防饿死。`in_flight` 由 wire 事件（response.created/done 计数）驱动，AI 回复中不抢答、done 归零补触发，watchdog 防 done 丢失导致永久沉默。真机拨 10000 验证：4000ms 窗口下 IVR 连播 5 段合并成 1 次应答；默认关行为逐字节不变。**静默窗口默认值待真机对话节奏精细标定**（太短 IVR 间隙抢话、太长真人对话变慢），当前默认关、需用户按体验决定是否默认开及最终窗口值。
  3. **调 VAD 转向阈值**减少对连续播报的过度切分——副作用：拖慢真人对话响应。
  - ②已作为长期正解落地（默认关）；①作为默认轻量缓解并存。
- **P3-13 核实 send_dtmf 真机端到端有效**（仍未定论，2026-07-09 凌晨补充证据）：
  - 证据 A（07-08 20:03 通话）：模型确实调用了 `send_dtmf`（QVTS 返回 OK），但数字菜单照样循环、IVR 最终报"未检测到您的操作"——**倾向 QVTS 的音没到对端**。
  - 证据 B（07-09 02:29 通话）：模型只是嘴上说"我按2/按9"、**未调用工具**（另一问题：模型叙述代替工具调用）。
  - 证据 C（07-09 02:32 手动 API 隔离测试）：无定论——该通 IVR 处于语音识别模式，无按键场景。
  - **新假设（待验证）**：UAC 模式下上行音频由宿主机喂给模组，`AT+QVTS` 注入的可能是模组自身音频路径、与 UAC 上行不合流 → 对端听不到按键音。若成立，正解是**带内 DTMF**：在宿主侧把 DTMF 双音直接合成进上行 PCM（audio_bridge 加 tone 生成器），不依赖 QVTS。
  - 复测方法：拨到数字菜单明确报"请按X"时手动 API 发键，观察菜单是否推进；或用带内合成对照测试。
  - **进展（07-09 上午）**：带内 DTMF 已实现并合入（6b5ede4，DTMF_MODE=inband 默认），带内合成独立频谱验证正确、审计事件落 downlink 录音。但真机菜单推进仍未验证成，因为发现更前置的问题——**模型今天所有通话说了 14 次"我按X"却 0 次调用 send_dtmf 工具**（证据 B 现象常态化）：根因是 prompts common 可用工具清单漏列 send_dtmf、引导过弱，已提示词加固（eb70253：补进工具清单 + 明确"必须调用工具真正发送、不是只在话里说按哪个键"）。**仍待数字按键菜单场景真机确认**：① 加固后 AI 是否真调 send_dtmf；② 带内 DTMF 是否让菜单推进。本轮几通 10000 均被路由到语音识别型 IVR（"请说XX"），无按键场景，且模型对语音菜单也会误说"我按X"（把语音菜单当按键，措辞混乱，与 P3-14 同源）。需拨一个已知纯数字按键 IVR 才能收口。

---

### 3.5 代码质量优化（2026-07 三路审查产出）

按优先级的完整清单见 `docs/code-review-2026-07.md`：P0 正确性 bug 5 项（豆包
provider 外呼静默失效、WS 推送偶发丢事件、hangup 无锁等）→ P1 架构优雅性 5 项
（CallSession 拆分、server.py 样板收敛、配置收口注册表等）→ P2 工程化护栏
（ruff/mypy、CI、.env.example 绑定测试已落地）→ P3 打磨 + 5 个高 ROI 测试缺口。

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

- **测试补全**：状态机/音频桥/工具/policy 单测覆盖核心路径；`pytest` 一键跑。
- **真机验收清单**（`docs/acceptance.md`）：来电自动接听×3、qwen 对话 5 轮×3 通、外呼×1、
  中英文短信收发、AI 主动挂断、30 分钟长通话稳定、拔插恢复。

### 5.1 可自主执行的验证手段（无需人工在环）

用真实可交互对端做端到端自测，Claude 可自己发起并从转写/事件判定结果：

- **外呼 10000（电信 IVR）验证双向语音链路**：电信客服 IVR 自带语音识别，能与之**多轮
  对话**（如说"查余额/查流量"，IVR 按语义推进菜单）就证明——① 下行通（IVR 听到了 AI 说话）、
  ② 上行通（AI 听到了 IVR）、③ AI 对话逻辑正常。判定依据：转写里 `上行·用户`(IVR 提示)
  与 `下行·Agent`(AI 应答) 形成连贯推进，且 IVR 响应与 AI 所说一致。
  *触发*：`curl -X POST http://127.0.0.1:47100/api/call/dial -d '{"number":"10000"}'`。
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
- **D3 音频模式（双向均已真机验证）**：
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
- **D7 工具安全 policy（NFR-3）**：发短信目标限制+频控、挂断显式意图+延迟窗、查验证码可配、审计日志。
  **（实现状态见上 NFR-3 审计注：截至 2026-07-08 已落地）**
- **D8 EC20 USB 掉线风暴根因与缓解（2026-07-07 证据链分析）**：
  - **根因**：Mac **系统睡眠**（本机 `pmset sleep=1`）→ 唤醒时 USB 总线重枚举 → EC20 bulk
    endpoint 进入 stall；旧版桥从不调 `dev.reset()`，只能反复重开-秒挂，产生符号链接
    删建风暴（实测峰值 177 次/分钟），且桥与 app 两层自愈互相打架。
  - **已落地缓解（v0.1）**：① launchd plist 用 `caffeinate -s` 包裹两个进程，阻止系统
    睡眠（首要诱因）；② 桥重连前先 `dev.reset()` 清 stall；③ 桥重试指数退避（1→30s），
    连续快速失败（存活 <5s）达阈值（`EC20_BRIDGE_FAIL_THRESHOLD`，默认 6）即 exit(3)
    交给 launchd 冷重启；④ 桥单实例文件锁，防双桥抢设备。
  - 长期项（物理层：换供电/线材、USB 集线器隔离）挂 P3 观察。
  - **新证据（2026-07-08 05:23）**：caffeinate 生效期间、通话进行中仍发生掉线
    （语音 UAC 流 + AT 串口并发时段）——睡眠不是唯一诱因，**物理层嫌疑加重**
    （通话期射频+音频功耗拉高）。建议优先试带独立供电的 USB hub。
    连带发现会话僵尸 bug：串口断死后会话不收尾——已修复（CLCC 消失判定 ~4s
    + 轮询失联 60s 兜底，commit 5c3ccc8），手动兜底仍保留（界面挂断按钮 /
    POST /api/call/hangup）。
  - **韧性实测（2026-07-08 05:54）**：通话中杀掉 USB 桥进程，串口 2s 自愈重连，
    **通话不断、对话持续**——语音走 UAC 声卡独立 USB 接口不经桥，AT 通道
    短暂丢失可无感穿越。只有 USB 总线级 stall（两通道全死）才会断话。

---

## 附录 A：三段式本地大脑（✅ 已落地，2026-07-10）

> **状态更新**：已作为第四 provider `local` 落地（`agents/local_agent.py`），选型改为
> sherpa-onnx 全家桶（VAD/STT/TTS，无 torch）+ dashscope 文本脑（默认 qwen-plus），
> 模型资产 `python -m agentcall.local_models` 下载。真机拨 10000 验收 8/8。
> 原设想的 FunASR torch 栈弃用（依赖数 GB 无法进 DMG）。以下为历史设计记录。

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
