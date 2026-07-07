# 代码全面审查（2026-07-08）

目标：整体架构优雅简洁、代码可读性强。三路并行审查（后端架构 / Web 层与前端 /
测试与工程化），关键发现均已到代码逐条核实。总评：**分层清晰、锁纪律与安全卫生
（textContent 防注入、路径穿越白名单）上乘；真问题集中在个别正确性 bug、
CallSession/server.py 两处职责膨胀、配置多头真相、以及零 lint/CI 的工程化欠账。**

## P0 正确性 bug（尽快修）

1. **豆包 provider 外呼静默失效** — `DoubaoVoiceAgent` 未实现 `say()`（外呼开场白
   必经路径，base 默认 no-op）也不置 `fatal`（断线后主循环无法感知会话已死）。
   豆包模式下外呼对方接起后 AI 永远不开口。修实现，或在 factory/文档明确
   「豆包仅支持来电」。（agents/doubao_agent.py vs base.py:53、call_agent.py:249）
2. **WS 推送可能偶发丢事件** — `EventHub._broadcast` 里 `asyncio.create_task`
   不持引用，task 可能被 GC（asyncio 文档明确要求存引用）。表现为前端偶发漏一条
   转写/短信，极难复现。修：`self._tasks: set` + `add_done_callback(discard)`。
   （events.py:70）
3. **`hangup()` 多条 AT 指令非原子** — ATH 与 AT+QPCMV=0 两条 `_send` 之间不持
   `_serial_lock`，CLCC 轮询可插队。`dial()`/`send_sms` 都包了锁，唯独它漏。
   （modem.py:389）
4. **延迟挂断 Timer 可误伤下一通** — `_tool_hangup` 的 `threading.Timer` 不存
   引用无法 cancel；若窗口内对方先挂断、下一通已开始，Timer 仍触发 stop()。
   修：保存引用并在 start/_shutdown 时 cancel，或校验会话世代。（call_agent.py:485）
5. **布尔配置两处判定不同** — `call_log._TRUTHY` 含 `"on"`、`config._TRUTHY` 不含：
   `RECORDING_ENABLED=on` 在录音层为真、在设置面板为假。统一走 config。
   （call_log.py:44 vs config.py:22）

## P1 架构优雅性（正对本轮目标）

6. **拆分 CallSession（531 行 god object）** — 编排、~50 行提示词构造、4 个工具
   处理、验证码正则、录音收尾、摘要调度混在一类；`_handle_call` 单函数 ~150 行。
   拆出 `prompts.py`（纯函数）与 `CallTools`（持 modem/hub/record），CallSession
   只留线程/循环/编排。（call_agent.py:66-597）
7. **server.py 样板收敛 + 响应 shape 统一** — `service is None → 500` 手抄 6 次、
   JSON 解析 try/except 5 次且已现手抄漂移；成功响应有的带 `ok` 有的不带，前端
   被迫两种判成功方式混用。修：`@web.middleware` 兜异常、`require_service()` /
   `read_json()` helper、统一按 HTTP 状态码判成败。（server.py 全文件）
8. **配置收口 config 注册表（消灭多头真相）** — `CallLogger.from_env`/
   `DialQueue.from_env` 各自实现 env 解析；`MODEM_PCM_PORT/BAUD` 不在注册表
   （app.py:95 直读）；`DASHSCOPE_REALTIME_URL` 源码在用但注册表和 .env.example
   都没有；`AGENT_OUTBOUND_TASK` 默认值两处。全部改走 `config.get_*` 并补注册。
9. **外呼 task 借道全局 env，三处写入** — `dial()`/`batch_dial()`/`DialQueue._dial_next`
   都写 `AGENT_OUTBOUND_TASK`，单通拨号的 task 被意外持久化成全局默认。改为
   `CallSession.start(number, task)` 显式传参；持久化默认主题应是独立用户操作。
   （call_agent.py:752,766、dial_queue.py:238）
10. **Web 层抽象泄漏** — `_hangup`/`_dtmf` 直接 reach into `service.session.*`/
    `service.modem.*`，而 `_dial` 走高层方法；这也导致 test_web_api 的 FakeService
    无法覆盖这些路由（零测试，含 DTMF 白名单校验）。给 service 加 `hangup()`/
    `send_dtmf()`，Web 层只调高层。（server.py:150,162）

## P2 工程化护栏

11. **零 lint/type-check 配置** — 代码里已有大量 `# type: ignore`/`# noqa` 说明
    跑过工具，但没沉淀成配置。dev 依赖加 ruff（近零成本）+ mypy（分阶段），
    pyproject 加 `[tool.ruff]`/`[tool.mypy]`。
12. **无 CI** — 已有 Issue 模板（预期外部贡献）但无 workflows；165 用例 12s
    零硬件是 CI 理想标的。加 `.github/workflows/ci.yml`：ruff + pytest。
13. **`.env.example` 缺 20 个运行时开关** — 含录音、外呼白名单、监听增益等
    隐私/行为关键项；新部署者无从发现。加一条测试断言「每个 editable spec 都
    出现在 .env.example」，让脱节无法回归。
14. **前端空 catch 治理 + i18n key 一致性** — 7 处空 `catch {}` 把「服务出错」
    伪装成「没有数据」；`zh.cfg={}` 与 `en.cfg` 全量的 key 集合已不对称，漏填
    key 会静默露出原始标识符。至少 console.warn；加两套字典 key 一致性测试。

## P3 打磨（择机）

15. audio mode 分派两套 if 链（audio_bridge.py:448、modem.py:296）→ 一张
    `AUDIO_MODES` 映射表两处消费。
16. summarizer 嵌套线程实现超时，超时后残留僵尸请求线程（summarizer.py:161）。
17. 前端运行时状态集中声明；`window._lastTask` 改脚本作用域（index.html:827）。
18. `events._repair_sms_event` 反向 import modem 私有函数，层次倒置（events.py:108）。
19. 杂项：`addSms` 硬编码中文「未知」；`setToast` 正则改 classList；CLCC/DTMF
    等裸数字提常量；`scripts/README.md` 给 20+ 探针脚本建索引；测试 `make_service`/
    `wait_until` 在多文件重复，提进 conftest；aiohttp `NotAppKeyWarning` 40 条噪音。

## 修复期间新发现（2026-07-08 对抗验证产出）

- **modem 既有 ABBA 锁序风险**：`dial()`/`send_sms()`/`hangup()` 持 `_serial_lock`
  期间若 `_send` 因串口异常进入 `_reconnect`（等 `_reconnect_lock`），而读循环线程
  已持 `_reconnect_lock` 在等 `_serial_lock`，则互相死锁。三个方法同一形态（hangup
  是本轮对齐后加入的）。修法：`_send` 触发重连前先释放 `_serial_lock`，或统一锁序。
  列入 P1 跟踪。（modem.py:223/226/393）

## ROI 最高的 5 个测试缺口

1. `Eg25Modem._reconnect()` 状态机直测（真实事故点：桥重插后哑掉）。
2. summarizer 超时路径真正生效的断言。
3. supervisor 重连瞬间来 RING / 用户点外呼的竞态。
4. `resample_pcm` 8k↔24k 往返正确性（音质链路地基）。
5. `update_env_file` 并发写 + BOM/CRLF 容错冒烟。
