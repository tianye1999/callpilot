# 实体卡 Mac 设置向导（#140 工作包 4）

本分支为 `product/physical-sim`。六步保持：硬件、模型、个性化、通话录音、自测、完成。
本次不包含通知发送，不构建、安装或启动真实服务，不拨号。

## 默认配置与 Key

- `AGENT_PROVIDER=local`、`LOCAL_LLM_PROVIDER=deepseek`。
- `LOCAL_LLM_MODEL=deepseek-flash`，避免把旧的 `qwen-plus` 发到 DeepSeek。
- 显式设置的 provider/model 不被自动迁移；显式 DashScope 且未设置模型名时仍回退 `qwen-plus`。
- 向导默认显示员工自己的 DeepSeek Key；Qwen/OpenAI/实验性豆包放进高级折叠区。
- `POST /api/config/validate_key` 新增 `provider=deepseek`，只做
  `GET {LOCAL_LLM_BASE_URL}/user/balance`，不消耗生成 token。
- 返回 `valid`、`format`、`invalid`、`balance`、`network`，前端分别提供 zh/en 白话说明。
  不返回 Key 或原始网络异常。验证成功后向导通过现有 `/api/config` 保存 `DEEPSEEK_API_KEY`。
- 本地音色分别写 `LOCAL_TTS_MODEL=vits-icefall-zh-aishell3` / `LOCAL_TTS_SPEAKER_ID=25`，
  或 `vits-piper-zh_CN-chaowen-medium-int8` / `0`；云端保持原有音色列表。

官方依据：[余额接口](https://api-docs.deepseek.com/api/get-user-balance/)、
[当前模型名](https://api-docs.deepseek.com/quick_start/pricing)。

## 模型打包

`packaging/agentcall.spec` 调用 `bundle_data_files()`，只收 `required_files`。
默认构建输入为 `data/models`，可用 `AGENTCALL_BUILD_MODELS` 指向准备好的构建资产目录。
缺少任意文件直接构建失败，不在构建过程中偷偷下载。

| 资产 | 文件 | 本机清单字节数 |
| --- | --- | ---: |
| Silero VAD | `silero_vad.onnx` | 643,854 |
| Paraformer int8 | `model.int8.onnx`、`tokens.txt` | 243,446,974 |
| chaowen | `zh_CN-chaowen-medium.onnx`、`tokens.txt`、`lexicon.txt` | 20,717,786 |
| aishell3 | `model.onnx`、`tokens.txt`、`lexicon.txt`、`phone.fst`、`date.fst`、`number.fst`、`new_heteronym.fst` | 32,761,116 |
| 合计 | 13 个文件 | 297,569,730 |

共约 283.8 MiB（十进制 297.6 MB），不包含 Paraformer 非 int8 模型、归档或额外音色。
Mac 包内位置为 `Contents/Resources/models/`，运行时通过 PyInstaller `_MEIPASS/models` 链接读取。

`models_dir()` 顺序：显式 `LOCAL_MODELS_DIR` > 完整用户数据目录模型 > 完整包内模型 > 用户数据目录下载兜底。
完整性按当前选定音色的资产清单判定；包内目录永不写入，显式指向包内缺失资产也会报只读错误。
`--selftest` 在打包 Mac 中检查全部 13 个文件及 sherpa-onnx/UserNotifications 模块，不启动服务。

本次没有构建新 App。现有旧 `dist/CallPilot.app` 的磁盘占用约 173 MiB，仅作体积估算基线；
加上模型约 457 MiB，依赖与架构变化会改变最终值，DMG 压缩大小另计。
`build_app.sh` 构建完成会输出新 App 及 `Contents/Resources/models` 的实测 `du -sh`。

## 自测接口与生命周期

`POST /api/setup/test_call` 请求仅需 `{ "token": "..." }`，不接受客户端指定拨号目标。
`GET /api/meta` 增加 `setup_call_token`、`setup_call`。
令牌沿用短信向导的生成/刷新方式，但使用独立槽位，并在任何拨号操作前原子消费。
同令牌重复请求返回 403；忙线、模组未就绪、未知运营商、缺凭证、录音不可用返回 409；受理返回 202。

号码仅取当前 `modem.sim_identity.service_number`，继承 SIM 识别及已有热线覆盖设施。
在远程会话锁、来电占线锁下检查所有占线者和拨号队列，然后占用当前会话。
不使用普通拨号队列、不持久化任务、不自动重试；自测传 `dial(retry=False, deadline=...)`，
禁止普通 `_send()` 的串口重连后重发 ATD，并在锁内拒绝过期的拨号操作。

先收到至少 300ms、RMS >= 30 的 8kHz s16le 对方音频，再等待 1s 停顿。
不播放主动问候，不让模型继续接收菜单或调用工具。只提交一次描述性的线路测试任务，
不写固定话术；本地自测关闭额外慢网占位语，不影响普通通话。

生成完成采用可选的 `VoiceAgent.set_output_complete_handler()`：
本地等 TTS 队列结束，Qwen 把完成标记排在 PCM 后，OpenAI 使用 response.done 并区分失败。
还须等已排队字节实际写出、输出队列排空才结束，不能用流式中途静音推断生成完成。

拨号起设置 45 秒独立线程看门狗；不依赖 asyncio 取消或模型加载完成即可发起物理挂断。
结束时先挂断再停模型/音频桥；本地模型线程完成回收前不接纳下一通；迟到音频被本次专属栅栏丢弃。
**物理边界**：真实 AT 端口仍有串口互斥和固件响应耗时。45 秒保证触发挂断，不等同于
USB/固件卡死时也能数学保证蜂窝侧已经断线；Claude 必须测实际 AT 发出与线路结束时间。

每次自测显式 `recording_enabled=True`，不修改全局录音配置。结果：

- `received.ok`：有非空对方转写，或累计至少 2400 个足够能量的 8kHz 样本。本实现主要走能量证据。
- `sent.ok`：已成功写出 PCM、无串口/UAC/ffmpeg 写失败、上行累计非零字节。
- 两项包含原因码，前端 zh/en 翻译；录音使用原有 `buildRecPlayers()` 与历史录音 API。
- 结果还含 `state`、`status`、`number` 和录音成功落盘后的 `call_id`。
- 录音未落盘时返回 `recording_error=true` 并显示磁盘/权限提示，不给无效回放入口。
- **已知限制**：实验性豆包尚无主动 say/完整生成结束协议，自测在拨号前返回 `provider_unsupported`，
  界面说明换模型或跳过。没有为本包猜测豆包协议。

## 通知授权选择

选择 **窗口进程**，不是后台 aiohttp 服务或托盘 IPC。窗口进程已有 Cocoa 主循环与
`ai.bondings.callpilot` bundle 身份，而且用户点击就在这个窗口中；不需要新增 HTTP 权限入口、
跨进程授权状态文件或托盘轮询协议。托盘无需改动，实际通知发送留给工作包 5。

窗口暴露 `notification_status()`、`request_notifications()`、`open_notification_settings()`。
只接受同源本地 CallPilot 页面，且要求 frozen Mac、macOS 13+、正确 bundle ID。
普通浏览器、开发窗口与 Linux/Pi 没有该桥或能力检查失败，因此隐藏整个通知区域。

查询使用 `UNUserNotificationCenter.getNotificationSettingsWithCompletionHandler_`，不会弹框。
仅「允许通知」点击调用 `requestAuthorizationWithOptions_completionHandler_`（alert/sound/badge）。
pywebview 工作线程等待异步 completion，不阻塞 Cocoa 主线程；结果读自通知中心，不相信网页缓存。
未允许时提供打开系统通知设置的按钮；系统没有公开的单 App 深链 API，用户需选择 CallPilot。
入口只在完成页，构造和首次启动没有授权调用，不发送测试通知。

官方依据：[Apple 授权说明](https://developer.apple.com/documentation/UserNotifications/asking-permission-to-use-notifications)。
打包依赖增加仅 Darwin 的 `pyobjc-framework-UserNotifications`，Windows/Linux 不导入框架。

## 离线验证记录

2026-10-29 最终代码快照：

| 质量门 | 结果 |
| --- | --- |
| `.venv/bin/pytest -q` | 2952 passed、3 skipped，253.50 秒 |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/mypy` | 68 个源文件通过 |
| `.venv/bin/mypy --platform win32` | 68 个源文件通过 |
| `.venv/bin/mypy --platform linux` | 68 个源文件通过 |

pytest 有 1193 条 warning，主要是 aiohttp 字符串 AppKey 提示及既有 NumPy 数值警告。
`bash -n scripts/build_app.sh packaging/build_installer.sh` 通过。
保留原有 CRLF 文件格式，`git -c core.whitespace=cr-at-eol diff --check` 通过。

主要新增测试（参数化覆盖多个分支）：

- `test_physical_sim_defaults`、`test_explicit_provider_and_model_are_preserved`
- `test_deepseek_balance_validation`、`test_deepseek_errors_never_echo_secret`、`test_deepseek_format_rejected_without_http`
- `test_models_dir_resolution_and_readonly_bundle`、`test_bundle_manifest_only_required_files`、`test_bundle_path_requires_frozen_runtime`
- `test_setup_call_token_consumed_once`、`test_setup_call_uses_sim_number_not_dial_queue`、`test_setup_call_rejects_without_start`
- `test_selftest_modem_dial_never_reconnects_or_retries`、`test_setup_call_result_evidence`
- `test_setup_call_hard_deadline_forces_hangup_without_retry`、`test_setup_call_watchdog_hangs_up_while_modem_operation_blocks`
- `test_setup_call_never_dials_after_watchdog_expired`、`test_setup_call_listens_then_speaks_once_and_records`
- `test_setup_call_reports_recording_failure_without_staying_running`
- `test_notification_bridge_queries_without_requesting`、`test_notification_bridge_rejects_external_page_and_browser`
- `test_notification_status_comes_from_native_center`
- `test_setup_local_defaults_voices_key_and_call_results_in_node`
- `test_output_complete_follows_all_synthesized_pcm`、`test_output_completion_reports_response_failure`
- `test_uac_observer_counts_failure_after_successful_write`

还更新了既有默认值、env 同步、桌面窗口暴露接口及缺凭证测试。
独立只读复核发现的生成完成、写失败统计、截止窗口竞态已修正并回归。

Playwright 使用 `http://setup.invalid/`，全部 HTTP/WebSocket 都被内存 mock 拦截，未连接本机服务。
桌面及 390x844 窄屏验证了六步流转、本地音色、进行中按钮禁用、双向结果、录音控件和浏览器隐藏授权块；
窄屏无横向溢出，控制台 0 错误、0 警告。截图位于 `output/playwright/setup140-*.png`。
这只证明页面行为和布局，不代表真实录音听感、模型在线调用或 macOS 授权弹窗通过。

## Claude 真机验收

1. 使用更新后的构建依赖运行完整打包；运行打包版 `--selftest`，保存 App/模型/DMG 大小与签名检查结果。
2. 干净用户首次打开，确认没有通知/麦克风等权限弹窗，没有模型下载或包内写入。
3. 六步向导走 zh/en，验证 Key 格式错误、401、余额不足、断网和正确 Key；核对保存后重开与显式旧配置保留。
4. 对照 SIM 识别日志和屏幕号码，仅拨本卡免费客服号一通；确认忙线拒绝、不重拨、先听菜单、只说一句。
5. 关闭全局录音后自测仍可回放，下一通普通电话仍不录音；核对双向结果与实际听感。
6. 模拟慢模型与通话迟迟不结束，测从拨号至挂断请求及实际断线的时长；超过 45 秒实际断线不能直接签收。
7. 完成页点击授权，弹窗身份应是 CallPilot；允许、拒绝、系统设置修改后返回均显示真实状态。
8. 验证浏览器/Pi 隐藏授权区域，不发送任何真实通知。本轮不视为生产验收完成。
