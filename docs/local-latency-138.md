# #138 本地管线低延迟实现与测量（2026-10-29）

## 范围与结论

分支 `product/physical-sim`，未提交、未推送、未构建/安装、未启动真实服务/桥、未拨号。
没有修改 `.env`、`docs/callpilot-product-strategy.html` 或 `.codex_dialog.md`。

开场预合成、无计费 TLS 预热、分段转写抢跑、基准场景和逐轮 PCM 写出埋点已实现。
owner 后续决定覆盖初始的“默认关闭”要求：本产品线提速功能默认开启，**发包前真机验收**为发布闸门，而非默认启用的前置条件。
默认音色为 owner 10-24 选定的 `vits-icefall-zh-aishell3`、speaker `25`。
`LOCAL_TURN_SILENCE` 默认仍为 0.6 秒；`AGENT_PROVIDER`、`LOCAL_LLM_PROVIDER` 默认值本轮不变，留给向导工作包。
显式 `.env` / 进程环境值仍优先于代码默认；已有 `false` 或旧音色不会被本次改动强制覆盖。
注意配置是逐项覆盖：若旧 `.env` 只指定单说话人音色却没指定 speaker，会继承新的 `25`。保留旧音色时必须同时核对其 speaker（例如 chaowen 的 `0`），不能把“显式项保留”理解为整组配置自动迁移。
预合成命中后的离线开场交付已小于 0.5 秒；**真机接通到对方听见首音尚未验收**。
基准不启动设备、不经过 CallSession 半双工门控，不证明重复“喂”的真机现象已经修复。

## 开关与实现

| 开关 | 默认 / 可选值 | 行为与日志 |
| --- | --- | --- |
| `LOCAL_TTS_STREAMING` | `true` / `false` | 默认分句合成；显式 false 保留整句合成路径 |
| `LOCAL_ASR_SEGMENTED` | `true` / `false` | 默认分段识别；显式 false 保留原整段 ASR |
| `LOCAL_FAST_OPENING` | `true` / `false` | 外呼拨号后启动后台准备，复用任务/预设提示词，生成文本并合成 PCM；接通后原 `say()` 时机消费。日志 `[local_opening] prepared duration_ms=...`、`hit=True/False`、`fallback error=...`、`skipped mode=wait` |
| `LOCAL_INBOUND_GREETING` | 空字符串 / 自定义短句 | 开启快速开场后，来电直接合成配置短句；空则用 LLM。加入 `LOCAL_TTS_CACHE_PHRASES` 可复用进程级短句缓存。不含内置话术表 |
| `LOCAL_LLM_PREWARM` | `true` / `false` | DeepSeek 共用连接的纯 TCP/TLS 建连，不发 HTTP、不发 token。日志 `[local_prewarm] connected duration_ms=... billed_tokens=0`、`fallback error=...` |
| `LOCAL_LLM_PREWARM_IDLE` | `15.0` / 有限正秒数 | 空闲超过阈值时换新已握手连接；生成持锁期间跳过刷新；停止时关闭连接并回收 worker |
| `LOCAL_SPECULATIVE_LLM` | `true` / `false` | 依赖 `LOCAL_ASR_SEGMENTED=true` 和可取消 DeepSeek 传输；转写更新后生成，判停确认前只缓冲、不播放、不执行工具。日志 `[local_speculative] started revision=...`、`hit`、`miss reason=changed` |
| `LOCAL_LATENCY_METRICS` | `true` / `false` | `local_turn` / `local_opening` 事件及 `metrics.json.local_pipeline`；PCM 标签跟随实际写出，不把入队当写出 |
| `LOCAL_TTS_MODEL` / `LOCAL_TTS_SPEAKER_ID` | `vits-icefall-zh-aishell3` / `25` | 首次缺失走 local_models 现有下载机制；不更改 `LOCAL_TTS_PEAK` 默认值 |
| `PARALLEL_AGENT_CONNECT` | `true` / `false`，三平台一致 | owner 已决定默认开启；发包前必须真机验收。显式 false 保留串行启动 |
| `LOCAL_TURN_SILENCE`（已有） | `0.6` / 例如 `0.5`、`0.45` | 仅分段识别下生效；总物理静音含切段等待。默认不动 |

开场工具在预合成阶段不提供给 LLM。开场指令或系统任务已变化、准备失败/未就绪都回退原流程，不等待预合成。
`opening_mode=wait` 不生成/播放开场，不改变现有 IVR 策略；没有新增号码分类或关键词判断。
模组自带问候改变开场指令时，旧预合成不会误命中。

抢跑使用一个固定 worker 和最多一个待执行任务，避免继续说话时不断创建后台线程。
恢复说话/转写变化取消旧任务；正式历史只在判停后更新；上下文不匹配走原请求。
已确认但仍等待连接的任务有 `claimed` 保护，不会被下一次抢跑挤掉。
取消只关闭本请求拥有的连接，不能关闭其他有效请求的连接。优化模式建连预算为 3 秒，读取使用整轮剩余预算。
固定 worker 将连接清理移出 ASR 线程。系统 DNS 解析不能由 Python socket timeout 保证硬中断，收尾会等待它返回而非遗留线程。
抢跑的取消请求可能已经消费 token；这与零 token 的连接预热不同。

### 默认开启后的依赖与回退

- `LOCAL_LLM_PROVIDER` 仍默认 `dashscope`；非 DeepSeek 时预热和抢跑不会启动 worker，不报依赖错误，继续原 LLM 请求路径。不能把“开关 true”误认为 DashScope 也获得 DeepSeek 首字收益。
- 显式关闭 `LOCAL_ASR_SEGMENTED` 后，抢跑自动不启用，仍能走整段 ASR/普通 LLM；两种 provider 均有 fake 管线运行测试。
- aishell3 缺失会自动下载所选模型，不下载无关 chaowen。下载/依赖加载失败沿用明确错误、`fatal` 和初始化退出，不会静默换声；此处没有现成的自动换模型兜底，本轮也不添加。
- 模型首次下载、冷加载会影响首通，不能保证首次开箱立即满足 <500 ms；发包前要验收正常下载和离线失败提示。
- aishell3 原始音量偏小；speaker 25、8 kHz 输出和实际通话音量须真机验收。本轮不擅自改变峰值/增益。
- 并行启动也影响非 local provider；Linux 的既有模组问候会随并行路径启用，须验收问候顺序、无重复问候、挂断和媒体资源释放。
- 旧行为回归夹具显式设置七项开关为 false；新产品默认测试清除这些覆盖后运行，避免把测试隔离值误当产品默认。
- 根 `.env.example` 与 Linux 安装覆盖示例均同步并行默认 true，防止新安装清单重新覆盖为串行。

## 测量口径

- 所有实时 API 基准均使用 `build/packaging-venv-arm64/bin/python`，输出确认 `Architecture=arm64`。
- 同一输入：用户提供的 `bench-in.wav`，8 kHz、约 3.6 秒；模型为 `deepseek/deepseek-flash` 和 `vits-piper-zh_CN-chaowen-medium-int8`，本轮没有切换音色。
- 以下数据属于默认值切换之前的实现阶段；不是 aishell3 #25 新默认组合的重新测速。本次默认值变更只跑 fake 回归与质量门，不启动真实模型下载、服务或通话。
- 除特别标明外，每组 3 次；数字为毫秒，中位数（最小–最大）。调用真实 DeepSeek，有少量计费。
- `tts_first_audio` 是判停到 TTS PCM 交付，不是蜂窝远端听见声音；开场模拟假设模型/媒体已就绪且预合成可在接通前完成。
- 首次模型加载约 2.4–3.2 秒单列，不混入判停指标；后续共享模型约 7–14 ms。
- 不同组未控制云端负载，输出文本不完全相同；不能用这些小样本宣称稳定百分比收益或 p95。

### 按实施步骤记录

| 步骤/场景 | N | 判停→LLM 首字 | 判停→TTS 首音 | 模拟接通→首音 |
| --- | ---: | ---: | ---: | ---: |
| 修改前首轮，流式 TTS、非分段 | 1 | 835.3 | 1125.6 | 不适用 |
| 1. 快速开场命中 | 3 | 不适用 | 不适用 | **0.1（0.1–0.1）** |
| 2. 连接预热，非分段 | 3 | 622.5（485.0–1045.5） | 893.0（886.4–1351.4） | 不适用 |
| 3. 抢跑初次接入，旧基准打断策略 | 首轮失败 | 未完成 | `No reply audio generated` | 不适用 |
| 4. 修正基准，分段+抢跑，0.6 s | 3 | 621.6（476.3–977.7） | 990.6（800.8–1263.4） | 不适用 |
| 5. 加逐轮埋点，分段+抢跑，0.6 s | 3 | 7727.9（851.1–18123.5） | 8365.7（1070.5–18449.0） | 不适用 |
| 6. 分段+抢跑，0.5 s | 3 | 822.3（710.4–916.5） | 1184.4（1125.8–2020.5） | 不适用 |
| 6. 分段+抢跑，0.45 s | 3 | 739.8（661.7–812.0） | 1214.2（948.9–1240.5） | 不适用 |

第 1 项预合成准备耗时中位 1517.0 ms，范围 1372.3–1797.4 ms，移到振铃期间而非凭空消失。
第 2 项 TLS 建连 161.0–324.3 ms，未发送 HTTP 请求。
第 5 项 7.7 秒、18.1 秒首字尖峰如实保留；它们发生在等待 LLM 阶段，不能把它们解释成埋点开销，也不能从报告剔除。

### 补充对照与最终组合

| 场景 | N | 判停→LLM 首字 | 判停→TTS 首音 | 模拟接通→首音 |
| --- | ---: | ---: | ---: | ---: |
| 关闭本次优化，流式 TTS、非分段 | 3 | 1875.1（991.6–2205.4） | 2182.2（1302.0–2412.8） | 不适用 |
| 关闭快速开场，正常 `say()` | 3 | 不适用 | 不适用 | 1586.3（1255.7–1665.3） |
| 分段、无抢跑，0.6 s | 3 | 910.2（580.7–927.4） | 1190.1（942.3–1350.7） | 不适用 |
| 预热+抢跑+埋点，0.6 s，首次组合 | 首轮失败 | 连接/请求超时 | 无有效首音样本 | 不适用 |
| 同组合重新运行 | 3 | **307.8（249.3–396.1）** | **561.9（501.6–674.5）** | 不适用 |

组合重跑 TLS 为 164.0–292.2 ms，首句就绪为 319.5（308.0–456.7）ms。
失败记录保留：`[local_prewarm] fallback error=TimeoutError` 与 `DeepSeek 请求失败: timed out`。
不能把重跑成功解释成首次失败已经根治；隧道/上游稳定性仍是尾延迟限制。
Claude 提供的真机 14.3 秒开场、约 5.4 秒每轮是 x86 整句 TTS 的历史现场，不是本次 arm64 同条件对照；本轮未重测真机。

## 分段基准失败原因

实际 VAD：约 1.632 s 吐出 1.152 s 段，识别为“你怎么不说话”；2.400 s 又检测到语音，2.944 s 吐出 0.608 s 段，但转写为空。
旧基准允许第二次 VAD 取消第一轮回复，结果没有后续有效文本触发新回复，因而报无音频。
新基准默认禁用打断以测量生成时延，`--barge-in` 可保留原取消场景。
**这不是对真实半双工取消策略的修复**：真实链路默认没有 interrupt handler 时仍沿用既有取消行为；未偷偷更改默认行为。
最终分段默认场景可重复输出音频；`--barge-in` 在该输入及较慢网络下仍可能合法地产生无音频。

## 埋点字段

开启 `LOCAL_LATENCY_METRICS=true` 后：

```text
metrics.json.local_pipeline.opening.hit
metrics.json.local_pipeline.opening.prepare_ms
metrics.json.local_pipeline.turns[].turn_id
metrics.json.local_pipeline.turns[].stages.endpoint.{t_ms,ms,kind}
metrics.json.local_pipeline.turns[].stages.asr_tail.{t_ms,ms}
metrics.json.local_pipeline.turns[].stages.llm_first_token.{t_ms,ms}
metrics.json.local_pipeline.turns[].stages.first_clause_ready.{t_ms,ms}
metrics.json.local_pipeline.turns[].stages.tts_first_audio.{t_ms,ms}
metrics.json.local_pipeline.turns[].stages.first_pcm_written.{t_ms,ms}
```

`t_ms` 是相对会话开始的时间，`ms` 是相对本轮判停的偏移；抢跑首字/首句允许负值。
旧聚合 latency 为兼容仍截断负值为 0。缺失阶段不伪造为 0。
`first_pcm_written` 只在实际本地下行写出时打一次，正确区分旧轮排队、短写、丢弃、静音填充与 DTMF；不代表对端已听见。
开场的 `kind=say` 以请求时刻为锚；预合成 LLM/TTS 成本见 `opening.prepare_ms`，不伪装成接通后运行。
同时看已有 `startup_marks_ms`、`answered_to_first_audio_ms`、`answered_to_first_playout_ms`、`first_playout_boundary`，区分媒体准备与生成延迟。

## Claude 真机验证顺序

1. 发包前，在授权的真机测试流程中使用 arm64 运行时，核对默认 aishell3 #25 模型准备情况；确认七项提速开关的实际生效值，排除旧 `.env` 显式覆盖，保留当前静音/打断设置。
2. `PARALLEL_AGENT_CONNECT=true` 现为三平台默认，必须验收接通后媒体准备与 agent 启动并行。排查时可显式设 false 对照串行路径；验收未过不得发包。
3. 来电用项目配置的 `LOCAL_INBOUND_GREETING`，相同短句放入 `LOCAL_TTS_CACHE_PHRASES` 后再预加载。若采用模组自带问候，核对预合成回退，避免重复问候。
4. 对真人与 `opening_mode=wait` IVR 分别测试，确认 IVR 开场不抢说；对快速接通、无人接听、预合成超时、挂断中取消分别验收。
5. 默认 `LOCAL_ASR_SEGMENTED=true`、`LOCAL_SPECULATIVE_LLM=true`，保持 `LOCAL_TURN_SILENCE=0.6`；DeepSeek 下确认抢跑实际命中，非 DeepSeek 下确认正常退回原请求。确认工具仅在判停后执行、取消无旧音频/旧工具泄漏、每轮 ID/PCM 写出一致。
6. 真人语料对照 0.5 s，再决定是否试 0.45 s。减少窗口分别最多省 100/150 ms 的判停等待，但同时少了等量抢跑隐藏时间；本次小样本不支持“一定更快”。IVR 菜单、数字/金额中间停顿、慢说话的人容易被误拆轮，默认不降低。
7. 重复“喂”专门看 VAD 取消与半双工输出门控；不能用无打断基准来替代这项现场验收。真机开场 <500 ms 必须用真实写出和录音证据判断。

基准示例（只改进程环境，不写 `.env`）：

```bash
PYTHONPATH=src build/packaging-venv-arm64/bin/python scripts/local_pipeline_bench.py INPUT.wav --runs 5 --segmented --output /tmp/local-turn.wav
LOCAL_FAST_OPENING=true PYTHONPATH=src build/packaging-venv-arm64/bin/python scripts/local_pipeline_bench.py INPUT.wav --scenario opening --runs 5 --output /tmp/local-opening.wav
LOCAL_LLM_PREWARM=true LOCAL_LATENCY_METRICS=true PYTHONPATH=src build/packaging-venv-arm64/bin/python scripts/local_pipeline_bench.py INPUT.wav --speculative --turn-silence 0.5 --runs 5 --output /tmp/local-speculative.wav
```

## 新增/修改测试名

新增 `tests/unit/test_local_fast_opening.py`：

- `test_session_prepares_after_dial_before_answer_and_wait_profile_stays_silent`
- `test_connection_warmup_refresh_loop_stops_without_api_request`
- `test_prepared_opening_is_silent_until_say_and_consumed_once`
- `test_opening_failure_and_changed_instructions_fall_back`
- `test_disabled_opening_never_prepares`
- `test_inbound_configured_phrase_avoids_llm`
- `test_opening_metrics_and_pcm_write_callback_are_distinct`

新增 `tests/unit/test_local_speculative.py`：

- `test_speculative_hit_reuses_inflight_stream_only_after_endpoint`
- `test_resume_cancels_and_text_change_restarts_without_stale_history`
- `test_speculative_tools_wait_for_confirmation`
- `test_stop_cancels_speculation_and_joins_worker`
- `test_speculative_cancel_does_not_block_next_asr`
- `test_speculative_metrics_preserve_token_and_clause_ready_before_endpoint`
- `test_confirmed_pending_job_cannot_be_evicted_by_new_speculation`

现有文件新增/扩展：

- `test_local_llm.py::test_prewarm_connects_without_request_reuses_and_refreshes_idle`
- `test_local_llm.py::test_cancel_waiting_request_does_not_abort_active_socket_and_joins`
- `test_local_llm.py::test_cancel_during_connect_joins_before_return_and_never_posts`
- `test_audio_bridge.py::test_turn_callback_follows_written_spans_not_dropped_or_short_suffix`
- `test_call_metrics.py::test_local_turn_metrics_keep_identity_negative_overlap_and_missing_stages`
- `test_local_pipeline_bench.py::test_segmented_bench_empty_tail_does_not_cancel_measurement`
- `test_local_pipeline_bench.py::test_opening_cli_repeats_and_reports_median_and_range`
- `test_local_segmented.py::test_lower_silence_ends_earlier_without_changing_segment_gate`
- `test_local_config.py::test_local_config_defaults_and_env_sync` 扩展六个配置的默认值及示例同步锁。

独立 review 已完成两轮，连接所有权、取消期间 ASR 阻塞、已确认待执行任务被替换、时间归因问题均已修复并补回归。
测试均用 fake 模型/连接/模组，未为 CI 添加真实模型或 API 依赖；跨平台类型检查覆盖 Windows/Linux。

## 实现阶段质量门（默认值切换前）

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/pytest -q` | 2820 passed、3 skipped，230.45 秒；1098 warnings（主要为 aiohttp AppKey 与 NumPy 警告），无失败 |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/mypy` | 66 source files，无问题 |
| `.venv/bin/mypy --platform win32` | 66 source files，无问题 |
| `.venv/bin/mypy --platform linux` | 66 source files，无问题 |

质量门使用仓库 `.venv`，仅性能基准使用 arm64 解释器；未用 Rosetta 质量门耗时冒充性能基准。
`git diff --check` 通过。Windows/Linux 这里只做类型预检，不等同远端三平台 CI 实跑；按约束没有 push。

## Owner 默认值变更回归

本轮仅修改配置默认值、平台默认函数、示例与文档/测试，不扩展通话状态机或模型下载实现。
原有默认关闭回归改为显式 false；新增 `product_latency_defaults` 清除测试隔离覆盖，以验证真实代码默认。

更新的默认值锁：

- `test_local_config_defaults_and_env_sync`：七项布尔、aishell3 #25、0.6 s 及其他未改项，清环境后读取注册表并与根示例同步。
- `test_speed_switches_allow_explicit_false`：七项开关分别可显式关闭。
- `test_parallel_agent_connect_platform_default`：Linux/macOS/Windows 均为 true。
- `test_parallel_agent_connect_config_follows_platform_and_allows_override`：保留 true/false 覆盖验证。
- `test_linux_install_example_keeps_owner_parallel_default`：安装示例不得重新覆盖为 false。
- `test_aishell3_owner_defaults_keep_peak_optional`：替换旧的“aishell3 仅推荐、不改默认”断言，峰值默认仍为空。

新增运行回归：

- `test_product_defaults_pipeline_and_provider_dependency`：实际默认组合下分别验证 DashScope 和 DeepSeek；前者不预热/抢跑，后者判停前缓冲、判停后出声。
- `test_speculation_without_segmented_asr_falls_back_to_normal_request`：缺少分段依赖时正常响应。
- `test_explicit_false_preserves_whole_asr_whole_tts_and_no_background_work`：旧整段识别/整句合成、无预合成/预热/抢跑 worker、无逐轮事件和 PCM 标签。
- `test_default_aishell3_downloads_missing_assets_and_uses_owner_speaker`：缺失资产选择 aishell3，调用现有下载入口，使用 speaker 25 与 8 kHz 输出（fake sherpa/download）。
- `test_default_model_download_failure_is_explicit_not_silent_voice_fallback`：下载失败报告、fatal、无残留 brain worker，不换声。
- `test_product_defaults_local_parallel_startup_and_cleanup`：默认全开实际 LocalPipelineAgent 配 fake 模组/桥的启动、配置快照和收尾。

独立 review 的默认组合聚焦回归为 188 passed；分段旧注释与 Linux 示例覆盖已同步修正。

### 默认值切换后的最终质量门

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/pytest -q` | **2836 passed、3 skipped**，227.80 秒；1098 warnings，无失败 |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/mypy` | 66 source files，无问题 |
| `.venv/bin/mypy --platform win32` | 66 source files，无问题 |
| `.venv/bin/mypy --platform linux` | 66 source files，无问题 |

最终默认值相关聚焦组合为 210 passed，`git diff --check` 通过。本轮没有真实下载、测速、启动服务/桥或拨号；模型下载与通话链路均使用 fake。未 commit/push、未构建/安装、未改用户 `.env`。
