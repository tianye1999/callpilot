# CallPilot 新手 FAQ

安装到第一通电话之间最常见的问题。运行期故障速查见 [README 排障表](../README.md#排障)；
没覆盖到的问题请开 [issue](https://github.com/tianye1999/callpilot/issues)。

## 安装与首次启动

### Q1：DMG 打不开，提示「已损坏」或「无法验证开发者」？

官方 [GitHub Release](https://github.com/tianye1999/callpilot/releases/latest) 的 DMG
已用 Developer ID 签名、完成公证并 staple，正常双击打开即可，**不需要**右键绕过之类的操作。
如果仍提示无法打开，最常见原因是下载不完整或经第三方转存破坏了签名——重新从官方 Release
下载一次；还不行就带上 macOS 版本开 issue。

### Q2：首启向导「硬件检测」一直是红色未就绪？

向导检查三项：USB 在线、AT 口连接、串口端口。红色提示**不阻塞**后续配置，可以先填 API Key
再回头重试。排查顺序：

1. EC20/EG25 模组和 USB 转接板是否插稳（转接板供电不足是常见原因，换个 USB 口试试）；
2. SIM 是否插好；
3. 打包版（DMG）自带 USB 桥，插上即可；源码路径需要先跑
   `scripts/ec20_usb_pty.py`（见 README 开发者路径）。

### Q3：我需要准备什么硬件？SIM 卡有什么要求？

**Quectel EC20 或 EG25** 4G 模组 + 带 SIM 槽的 USB 转接板 + 天线，整套约 ¥100–200。
SIM 需要**开通语音 + 短信业务**（纯流量卡不行）；VoLTE / CS 语音是否可用取决于运营商。
详见 [README 硬件准备](../README.md#硬件准备)。

### Q4：UAC 音频模式需要手动配置吗？

不需要手动发 AT 命令。服务每次启动会自动下发 `AT+QCFG="USBCFG",…`（启用 UAC 复合设备）和
`AT+QPCMV=1,2`（打开语音通道）。注意：**如果模组此前从未启用过 UAC**，USB 配置变更要重插一次
USB（或重启模组）才会枚举出 UAC 声卡。macOS 上音频模式用默认的 `uac_ffmpeg` 即可。
开发者可用 `scripts/uac_check.py` 和 `scripts/eg25_probe.py` 自检。

### Q5：DashScope API Key 去哪开通？有免费额度吗？

在 [阿里云百炼控制台](https://bailian.console.aliyun.com/?tab=api#/api-key) 创建 API Key
（首启向导里的「获取 Key」链接同款）。新账号开通模型服务后一般有免费额度，具体以控制台
当前政策为准；可用模型见 [Omni 实时语音模型列表](https://help.aliyun.com/zh/model-studio/omni-voice-list)。
Key 填在首启向导或设置页即可，源码路径也可写 `.env` 的 `DASHSCOPE_API_KEY`。

### Q6：macOS 弹出麦克风 / 自动化权限请求，要允许吗？

要。麦克风权限用于采集通话对方的语音（UAC 声卡在系统里是输入设备），拒绝会导致 AI 听不到
对方、通话无声；自动化（Apple 事件）用于唤起控制台窗口等日常操作。

## 运行与排障

### Q7：电话接通了但完全没有声音？

macOS 上 `MODEM_AUDIO_MODE` 必须是 `uac_ffmpeg`（默认值）。想在电脑上旁听，需在设置里打开
「本机监听」。更多情况见 [README 排障表](../README.md#排障)。

### Q8：收到的短信不显示 / 收不到新短信？

升级到 **v0.5.4 及以上**：v0.5.3 修了启动补收 SIM 已存短信 + 去重，v0.5.4 修了 SIM 存储满后
收不到新短信（入库后自动删除 SIM 副本，`SMS_DELETE_AFTER_INGEST` 默认开启）。

### Q9：怀疑打包版没跑到最新功能？

菜单栏图标 → 打开控制台，核对页面显示的版本号；或跑打包自检：
`/Applications/CallPilot.app/Contents/MacOS/CallPilot --selftest`，它会验证关键模块是否
真的进了 bundle。

### Q10：怎么快速验证整条链路？

- **来电方向**：用自己手机拨模组 SIM 的号码，接通后应听到 AI 应答——这是最简单的全链路检查。
- **外呼方向**：建议拨运营商免费客服热线（如 10000/10086/10010）做测试，不产生话费；
  预调教任务库里已内置这些热线的查话费/查流量预设。
