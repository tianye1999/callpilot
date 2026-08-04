# 交接文档 — SIMCom SIM7600G + MiniMax 接入

写给接手的人。日期 2026-08-04。初稿：Claude；P0 续调、真机复核与安全修复：Codex。

初稿曾是 untracked；Codex 续调后决定纳入仓库，避免已否定的假设被重复试验。

---

## 0. 一句话现状

CallPilot 原生只支持 Quectel EC20/EG25。本轮把它跑在 **SIMCom SIM7600G** 上，并接入 **MiniMax Realtime**。**AT 控制、拨号、SIM 识别、界面与接收方向已通；发送方向在干净单通基线下连首个 320B 都不接收。DTR/RTS、timeout 后 clear_halt、interrupt-IN 轮询、320B/20ms 分帧均已真机排除为单独解法，下一步应验证官方驱动的接口映射与异步多 URB 传输。**

## 1. 环境

| 项 | 值 |
|---|---|
| 仓库 | `/Users/redtea/Downloads/code/callpilot` |
| 分支 | `local/simcom-integration`（**未推送**；原 18 个提交，Codex 代码提交 `394adab`，本文另做 docs 提交） |
| 基线 | `origin/main` = `c7b4546` |
| 模组 | SIMCom SIM7600G，USB `1e0e:9001`，固件 `LE20B05SIM7600M21-A_250919` |
| SIM | 中国电信，IMSI `46011…`，免费客服号 **10000** |
| Provider | MiniMax Realtime（key 在 `~/Library/Application Support/CallPilot/.env`） |
| venv | `.venv`（uv 建，arm64 CPython 3.12.13），已装 `pyinstaller`、`pypdf` |
| AT 手册 | `/Users/redtea/Downloads/SIM7500_SIM7600 Series_AT Command Manual_V3.00.pdf`（512 页，音频章在 5.2.25 / 5.2.43 / 5.2.45–5.2.47） |

### 质量门（commit 前置条件，项目 CLAUDE.md 规定）

```bash
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy
```

当前状态：**1213 passed / 3 skipped**，ruff 与 mypy 均干净。

### 打包与运行

```bash
bash packaging/build_installer.sh          # 产出 dist/CallPilot.app + .dmg
open dist/CallPilot.app                    # 菜单栏 app，Web 在 http://127.0.0.1:47100
```

- 打包版读 **`~/Library/Application Support/CallPilot/.env`**，不是仓库里的 `.env`。
- 图标 `packaging/CallPilot.icns` 被 `.gitignore` 排除；我用菜单栏 PNG 放大生成了一个占位的。缺它也不再让构建失败（见 `b5fdf25`）。

---

## 2. ⚠️ 操作纪律（我踩过，代价是弄断了用户一通电话）

1. **app 运行时绝不碰 `/tmp/ec20-at`。** 在 app 占用的串口上发 AT 会触发
   `device reports readiness to read but returned no data (multiple access on port?)`，
   模组当场掉线、通话中断。要做 AT 实验必须先把 app 完全停掉。
2. **停 app 用 `launchctl bootout`，不要 `pkill`。** 三个 launchd 单元
   （`com.agentcall.bridge` / `.app` / `.tray`）都是 `KeepAlive=true`，`pkill` 会被
   秒级复活，然后你以为停了、其实在和它抢资源。
   ```bash
   for L in com.agentcall.bridge com.agentcall.app com.agentcall.tray; do
     launchctl bootout "gui/$(id -u)/$L" 2>/dev/null
   done
   ```
   重启单个单元：`launchctl kickstart -k "gui/$(id -u)/com.agentcall.app"`
3. **不要 `rm /tmp/ec20-*`。** 桥在跑时删符号链接会让 app 报
   `could not open port /tmp/ec20-at`，制造假故障。
4. **真机拨号只允许拨本卡运营商免费客服号**（项目 `CLAUDE.md` 硬约束）。本卡是电信 →
   **只能拨 10000**。拨号前核对屏幕号码。`dial_guard` 会拦跨运营商客服号。

---

## 3. 关键技术发现（含对上游结论的两处纠正）

### 3.1 ★ PCM 采样率：`AT+CPCMBANDWIDTH` 是「接收只有噪声」的真因

手册 5.2.46：`AT+CPCMBANDWIDTH=<volte_sample>,<novolte_sample>`，`0`=16K / `1`=8K，
**出厂默认 `0,1` —— VoLTE 通话走 16K**。电信是纯 VoLTE，模组按 16 kHz 出流，而
CallPilot 整条链路按 `MODEM_RATE=8000` 解 → 收到的就是看着像宽带噪声的东西。

**上游据此判定 SIMCom PCM over USB "transmit-only、接收方向不可用"，并做了大量排除
（LE/BE/µ-law/A-law 解码、错位重对齐、立体声解交织、`CSDVC` 1/2/3 路由扫描、`CMIC`
增益）—— 唯独没设过这条命令。这个结论是错的。**

实测（拨 10000，两次独立复现）：

```
设 AT+CPCMBANDWIDTH=1,1 后，interface 4 的 IN 端点：
  192000 字节 / 12.00s = 15999 B/s  →  精确等于 8000Hz × 2 字节
  低/高频带能量比中位数 = 226.88    基频 = 216.2 Hz（播音女声音高）
不设时同一指标 ≈ 1.0（噪声）；按 16kHz 解则基频算出 400 Hz，人声不可能
```

已写进 `modem._enable_simcom_pcm()`（commit `0e16c11`）。

### 3.2 `AT+CPCMFRM` 不能用来降到 8k

手册 5.2.43 明确：**只支持 8k→16k 单向切换**。原代码和测试用 `AT+CPCMFRM=0`
「显式声明 8k」是无效的，已删除并改断言。

### 3.3 `AT+CODECCTL=1` 会让接收方向变差，不要用

手册 5.2.45 说 `1` = host 控 codec（配 `CSDVC=1|3` 开启）。听起来正是「接收方向绑在
模组模拟 codec 上」的解药，**但实测反而变成噪声**（能量比 0.74，对照默认 `CODECCTL=0`
是 27.5）。`CSDVC=3` 得到能量比 300+，是低频伪迹不是语音。结论：保持默认 `CODECCTL=0`。

### 3.4 音频在 interface 4，其他接口通话中零字节

通话中逐个接口读 bulk IN 的结果：interface 4 有 43071 字节/2s，interface 0/1/3/5 无数据。
与上游一致。AT 口是 interface 2（`--probe` 显示 2/3 都应答 AT）。

### 3.5 USB 描述符：没有 UAC，没有同步端点，没有 alt setting

```
6 个接口全部 class=VENDOR；全部只有 alt=0；端点只有 BULK + INTERRUPT
interface 1/2/3/4 结构完全相同（interrupt IN + bulk IN/OUT）
```

所以：当前 PID `9001` 的 composite 里确实没有 USB 声卡（`AT+CUSBAUDIO` 在手册里**根本
不存在**，是未文档化命令，本机读回 `0`）；也不存在「漏选 alt setting」或「漏收同步端点」
的问题 —— 用 bulk 搬字节的机制是对的。

### 3.6 VoLTE-only 卡的注册判定

电信没有 GSM/WCDMA 电路域，`AT+CREG?` 恒回 `0,3`（CS 域被拒），但 `AT+CEREG?` 为 `0,1`、
`ATD10000;` 能正常接通（实测 `VOICE CALL: BEGIN` + `+CLCC: …,0,…` stat=0，通了 13.5s）。
`dial_guard` 原先只看 CS 域，把能打的电话拦死了。已改为看
`SimIdentity.network_attached`（CS 或 EPS 任一已注册），见 `b66c745`。

### 3.7 MiniMax Realtime 的协议契约（全部真机实测）

端点 `wss://api.minimaxi.com/ws/v1/realtime`，说 **OpenAI Realtime beta 协议**，
双向 pcm16 @ 24kHz，模型固定 `abab6.5s-chat`（`?model=` 被忽略）。国内区 key
在国际站 `api.minimax.io` 上回 401。

五个怪癖（照 OpenAI 写法会直接报错，细节见 `agents/minimax_agent.py` 模块 docstring）：

1. `max_response_output_tokens` **必须是字符串**（传数字报 `1000` Go unmarshal）
2. `conversation.item.create` 的 item **必须带 `status:"completed"`**
3. content type 必须 `input_text`（`"text"` 被拒）
4. `say()` 不能用 `response.create` 带 `instructions`（报 `2013`），且上下文 item
   **只能 `role:"user"`** —— `role:"system"` 能建但不算 "chat content"，仍报 `2013`
5. 服务端**静默丢弃** `session.tools` 与 `turn_detection`（回显里没有这两个字段）

REST 面（`/v1/chat/completions`）的 **function calling 是正常的**，TTS
（`/v1/t2a_v2`）能直出 8kHz/mono/16bit PCM。所以「MiniMax 不能用工具」只对 realtime 成立。

---

## 4. 本地提交清单（代码截至 19 个，未推送；本文另做 docs 提交）

上游未合并分支合入（前 4 个不是我写的）：

```
e51d28d fix(types): 修复新版 dashscope 存根导致的 5 处 mypy 报错
846ab30 fix(modem): 模组掉线后自动恢复，不再需要手动重启服务
3da0cb4 feat(sim): 支持非 Quectel 模组与 Web 端 SIM PIN 解锁
45d60b7 feat(audio): SIMCom PCM over USB（下行可用，上行待解）+ 演示卡改联通
a1d3500 Merge remote-tracking branch 'origin/fix/mypy-and-modem-supervisor'
```

本轮新增：

```
3123797 feat(usb): 模组设备自动发现，--vid/--pid 均可省略
f5b5242 refactor(modem): MODEM_USB_VID 收成单一事实来源，厂商私有 AT 按厂商下发
3866387 fix(audio): PCM 走 PTY 时直接用安全波特率，simcom_pcm 起桥前校验通道已开
3793e4e docs: 桥的自动发现说明，simcom_pcm 接口号示例标注为因模组而异
e70f46b refactor(agents): OpenAIVoiceAgent 抽出 provider 接缝，注册 MiniMax 配置项
1b69d45 feat(minimax): 接入 MiniMax Realtime provider（含客户端 VAD 断句）
f128302 feat(web): 设置面板与首启向导支持配置 MiniMax Key
b66c745 fix(sim): VoLTE-only 卡不再被拨号门禁拦死
f5b87c3 fix(meta): provider 显示名收口到 config，补上 minimax 分支
b5fdf25 fix(packaging): 缺 CallPilot.icns 不再让 macOS 构建在最后一步崩掉
581108e fix(macos): 常驻桥的接口映射改为可配，并让菜单栏进程加载 .env
0e16c11 fix(audio): 钉住 PCM 采样率 8k —— VoLTE 默认 16K 是接收噪声的真因
b16dbbe fix(bridge): 数据口链路判死不再连坐控制口
394adab fix(simcom): harden usb pcm and call cleanup
```

新增配置项：`MODEM_USB_VID`、`MODEM_BRIDGE_MAPS`、`MINIMAX_API_KEY` /
`_REALTIME_MODEL` / `_VOICE` / `_REALTIME_URL` / `_RECONNECT_MAX` /
`_VAD_RMS_THRESHOLD`、`AGENT_MODEL_NAME_MINIMAX`。`.env.example` 与
`config.CONFIG_SPECS` 有防脱节测试，改一处要同步另一处。

---

## 5. 遗留问题（按优先级）

### P0 — 发送方向（AI→对方）首包起不接收，约 2 秒后判死 ★ 主要工作

现象（app 拨 10000 时稳定复现）：

```
18:01:17  SIMCom PCM 语音通道已启用 (AT+CPCMREG=1，第 1 次尝试)
18:01:17  音频桥启动 /tmp/ec20-pcm (8kHz mono)
18:01:17  interface 4 USB 写超时，丢弃 512 字节（第 1/10 次）
18:01:19  interface 4 连续 10 次 USB 写超时，判定链路已死   ← 10×200ms = 恰好 2s
```

**读方向已经通了（见 3.1），写方向不通。** 早期某次通话曾以正确速率写进去
约 15 秒（每 5 秒 ~80000 字节 = 16 kB/s）然后停，所以端点不是完全不可写。

**关键线索：有同事用同一版固件在 Windows 上跑通了，方式是「通过 audio 口传 PCM 流」。**
Windows 有 SIMCom 官方驱动，我们是 libusb 从用户态直连 bulk 端点。所以差别在握手，
不在固件。

#### 2026-08-04 Codex 续调结果（以下覆盖上面的旧建议）

在先用 `AT+CHUP` 清空全部历史呼叫、`AT+CLCC` 只返回 `OK` 的干净基线上，重新拨
10000，结果为：

```
18:54:32.117  AT+CPCMREG=1 成功（第 2 次尝试）
18:54:32.145  PCM bridge frame=320B/20ms
18:54:32.377  bulk OUT 首次超时；此前成功 0 bytes / 0 writes
18:54:34.327  连续 10 次超时，只关闭 interface 4
18:54:34.866  AT+CHUP 收尾；随后直接查 AT+CLCC = OK（无遗留呼叫）
```

已逐项验证：

1. **DTR/RTS 不够。** claim interface 2/4 后都成功发送
   `0x21/0x22/wValue=0x03`，写端点仍从首包起 NAK。
2. **timeout 不是 STALL。** timeout 后 `clear_halt` 能返回成功，但原帧重试仍 timeout。
   现代码只在 libusb 明确返回 PIPE/STALL 时 `clear_halt`，普通 timeout 不再误复位端点。
3. **持续轮询 interrupt-IN 不够。** 已发现并轮询 interface 2 的 `0x85`、interface 4
   的 `0x89`，结果不变。
4. **官方 320B/20ms 分帧不够。** Waveshare/Linux 工作样例通过 `/dev/ttyUSB4`
   每 20ms 写约 320B；项目已只对 `simcom_pcm` 对齐该节奏，首包仍 0-byte timeout。
5. **数据口隔离和挂断安全已验证。** interface 4 判死后 `/tmp/ec20-at` 保持在线；
   SIMCom 改用 `AT+CHUP` 清全部 active/held call，`ATH` 仅作 CHUP 失败时回退；
   CLCC 响应加 call generation，hangup 后不再出现迟到的“外呼已接通”。

当前最值得做的下一步：

- **先问同事接口映射和访问方式**（见下方两问）。如果他使用官方虚拟 COM，确认其
  “audio 口”是否真的映射 USB interface 4 / endpoint `0x05`。
- **若接口确认无误，做异步多 URB。** Linux `option` + `usb_wwan` 对 SIMCom 9001
  使用 4 个 OUT URB、每个 4096B，无同步 200ms 超时；当前 PyUSB 是单路同步
  `libusb_bulk_transfer`。优先做独立、可取消的异步诊断，不要直接把 PyUSB 私有结构
  塞进生产桥。`SET_LINE_CODING` 没有依据：Linux usb_wwan 明确认为波特率无意义，
  只发 DTR/RTS 控制请求。

注意：两次“同步写最长等 5 秒”的独占脚本运行时后来发现存在 active + held 历史呼叫，
不是干净证据，不能拿来证明异步 URB 必然无效；上面 18:54 的 app 单通复现才是可信基线。

**还需要向同事确认两件事**（能省很多试错）：
- 他用的是哪个 USB 接口索引？（官方驱动枚举出的 COM 口未必对应 interface 4）
- 是走官方驱动的虚拟 COM 口直接写串口，还是自己写的 USB 程序？前者更印证 DTR 这条。

### P1 — 摘要与收尾裁判在 MiniMax 下不工作

日志：`wrap_up_judge -> 缺少环境变量 DASHSCOPE_API_KEY`。
`summarizer`、DTMF 判官、`prompt_gen`（日志另有
`动态场景提示词未使用: 不支持的动态提示词提供方: minimax`）全部硬绑 dashscope SDK。
MiniMax 的 `/v1/chat/completions` 是 OpenAI 兼容且 function calling 正常，
加一个 OpenAI 兼容文本客户端就能修，同时也能让 `AGENT_PROVIDER=local` 三段式用
MiniMax-M3 当大脑（realtime 端点只给 `abab6.5s-chat`，用不上 M3）。

### P2 — `reg_status` 显示仍是「注册被拒」

拨号门禁已修（看 `network_attached`），但界面上 SIM 卡状态还显示 CS 域的
「注册被拒」，对 VoLTE-only 卡是误导。建议前端改成显示
`network_attached` + 两域明细（`reg_status` / `eps_status` 都已在 `/api/meta` 里）。

### P2 — launchd plist 参数陈旧

`install_launch_agents` 按内容比对写「stale」plist，但**只在菜单栏进程启动时跑一次**。
所以改了 `MODEM_BRIDGE_MAPS` 之后必须重启 tray 才会重写 plist 并重启桥。
我踩过一次：改了配置但 plist 还是旧的 EC20 布局，查了很久。
建议：服务侧配置变更时也触发一次 plist 比对，或在设置面板给出提示。

### P3 — tray_app 遇到桥实例冲突会崩

`launchd-tray.err.log` 里有未捕获的
`RuntimeError: 另一个 ec20_usb_pty 实例正在运行 (pid=…)` 直接把 tray 打挂。
应该降级成告警 + 菜单里显示状态，而不是崩进程。

### P3 — MiniMax 客户端 VAD 阈值会被噪声触发

`MINIMAX_VAD_RMS_THRESHOLD=400` 在噪声底上会误触发断句（写方向不通的那几通里，
AI 因此对着噪声反复应答）。写方向修通、上行电平确定后需要重新标定。

### 未完成的验证

**app 层端到端音频没验过。** 我做的实证都在 AT/USB 层（独立脚本、独占串口）。
写方向修通后需要走一遍完整流程：界面拨 10000 → AI 说话对方能听到 → AI 能听懂 IVR。
`MONITOR_AI_PLAYBACK` 与 `RECORDING_ENABLED` 我已置 `true`，可用双轨录音回放取证。

判断 AI 是否听懂**不能看转写里有没有 `role=user`** —— MiniMax realtime 不发用户侧
转写事件（3.7 第 5 条）。要看它的回答是否针对 IVR 内容。

---

## 6. 复现用的诊断脚本

做 AT/USB 实验前先按第 2 节停掉 app。以下脚本直接用 libusb 独占设备，不经 PTY 桥。

### 6.1 通话中逐接口找音频流 + 定性

```python
"""拨 10000（本卡=中国电信免费客服，符合 CLAUDE.md 硬约束），
逐个接口读 bulk IN，用低/高频带能量比定性：语音 >5，噪声 ≈1。"""
import time
import numpy as np, usb.core, usb.util

AT_IF, AT_OUT, AT_IN = 2, 0x03, 0x84
IFACES = {0:(0x01,0x81), 1:(0x02,0x82), 3:(0x04,0x86), 4:(0x05,0x88), 5:(0x06,0x8a)}
dev = usb.core.find(idVendor=0x1E0E, idProduct=0x9001)
try: dev.set_configuration()
except Exception: pass
usb.util.claim_interface(dev, AT_IF)

def at(cmd, wait=1.2):
    while True:
        try: dev.read(AT_IN, 512, timeout=50)
        except Exception: break
    dev.write(AT_OUT, (cmd+"\r").encode(), timeout=1000)
    out, dl = b"", time.time()+wait
    while time.time() < dl:
        try: out += bytes(dev.read(AT_IN, 512, timeout=200))
        except Exception: pass
        if b"OK\r\n" in out or b"ERROR" in out: break
    return " | ".join(l.strip() for l in out.decode("ascii","ignore").splitlines() if l.strip())

def band_ratio(pcm, rate=8000):
    s = np.frombuffer(pcm[:len(pcm)//2*2], dtype="<i2").astype(np.float64)
    if s.size < 8192: return 0.0
    n = 1 << 13; seg = s[:n]
    spec = np.abs(np.fft.rfft(seg*np.hanning(n)))**2
    f = np.fft.rfftfreq(n, 1/rate)
    lo, hi = spec[(f>=300)&(f<=1000)].sum(), spec[(f>=3000)&(f<=4000)].sum()
    return lo/hi if hi > 0 else 0.0

at("ATE0")
print("ATD ->", at("ATD10000;", wait=3.0))
for i in range(7):
    time.sleep(1.5)
    if ",0,0," in at("AT+CLCC", wait=0.6): print("已接通"); break
else:
    at("ATH"); raise SystemExit("未接通")

print("CPCMBANDWIDTH=1,1 ->", at("AT+CPCMBANDWIDTH=1,1"))   # ★ 必须，否则 VoLTE 走 16K
print("CPCMREG=1         ->", at("AT+CPCMREG=1", wait=2.0))
for ifn, (_out, in_ep) in IFACES.items():
    try: usb.util.claim_interface(dev, ifn)
    except Exception: continue
    buf, t0 = b"", time.time()
    while time.time()-t0 < 2.0:
        try: buf += bytes(dev.read(in_ep, 512, timeout=100))
        except Exception: pass
    usb.util.release_interface(dev, ifn)
    print(f"  interface {ifn}: {len(buf):>7} 字节  能量比={band_ratio(buf):6.2f}")
print("ATH ->", at("ATH"))
usb.util.release_interface(dev, AT_IF); usb.util.dispose_resources(dev)
```

### 6.2 精确测采样率（判断 8k / 16k）

在上面「已接通 + CPCMBANDWIDTH=1,1 + CPCMREG=1」之后，读 interface 4 十几秒，
`len(buf)/elapsed` 应为 **15999 B/s ≈ 8000Hz × 2B**。若得到 ~32000 B/s 说明还在 16K。
再用自相关估基频：人声应落在 165–265 Hz（男声更低）。

### 6.3 AT 手册查页（512 页，别手翻）

```python
import pypdf
r = pypdf.PdfReader("/Users/redtea/Downloads/SIM7500_SIM7600 Series_AT Command Manual_V3.00.pdf")
for i, pg in enumerate(r.pages):
    t = (pg.extract_text() or "")
    if "CPCMREG" in t.upper() and "DEFINEDVALUES" in t.upper().replace(" ", ""):
        print(i+1); print(t[:2000]); break
```

相关页：`CPCMREG` 145、`CPCMFRM` 161、`CODECCTL` 162–163、`CPCMBANDWIDTH` 163–164、
`CSDVC` 164–165。

---

## 7. 我在本轮制造的问题（已修，但接手时值得知道）

1. 在 app 占用的串口上发 AT，**弄断了用户一通电话**（第 2 节第 1 条就是这么来的）。
2. 反复 `pkill` 跟 launchd `KeepAlive` 打架、`rm /tmp/ec20-*` 删掉运行中桥的符号链接，
   造成状态反复抖动和一堆假故障日志。
3. 一度照搬上游「transmit-only」的结论去解释用户听不到声音，没有自己验证 —— 后来被
   同事的 Windows 结果和手册推翻（3.1）。
4. `/api/meta` 的 provider 显示名漏了 minimax 分支（`f5b87c3` 修，并加了不变式测试）。
5. 桥的 `MODEM_BRIDGE_MAPS` 改成可配后忘了菜单栏进程不加载 `.env`，配置不生效
   （`581108e` 一并修）。
6. Codex 在 app 音频失败后相信了逻辑队列的 `active=false`，没有先独占 AT 直查
   `CLCC` 就连续做了两次同步写诊断，形成 active + held 两路 10000。已用
   `AT+CHUP` 同时清掉，并把全局 CHUP + CLCC 世代防竞态写进 `394adab`；这两次
   5 秒同步写结果作废，不能当异步 URB 假设的反证。
