# Raspberry Pi 5 上的 CallPilot Edge（SIM7600 / Linux）

本指南对应 #127 Phase 1：在 Pi 上运行完整 `app.py`（Web 仪表盘、通话服务、可选云连接），
以 Linux 原生串口驱动 SIM7600，目标是来电 AI 接听。代码与本地自动化检查不等于真机验收；
部署、重启与双向通话仍须由真机执行者确认。

不使用 SIM 卡上网，Pi 通过有线网络或 Wi-Fi 联网。不安装 macOS USB→PTY 桥，
也不改变 macOS / Windows 的默认行为。按 owner 决定，每通后自动重启 SIM7600 的 #121
自愈提前纳入 Phase 1；VoLTE-only 外呼注册判定仍留 Phase 2，不承诺稳定外呼。

## 硬件与系统

- Raspberry Pi 5，64 位 Debian 12 Bookworm / Raspberry Pi OS Bookworm（aarch64）。
- 可靠的 Pi 电源（推荐官方 5V / 5A 电源）、散热、存储卡或 SSD。
- SIM7600G USB 转接板、配套天线与支持数据传输的 USB 线；按转接板要求独立供电，
  需要时使用有源 USB Hub。不要仅凭 Pi 开机成功判断模组通话峰值供电充足。
- 已开通语音、短信与 VoLTE 的国内实体 SIM 卡。插拔 SIM 与接线前先断电。
- 已授权的测试来电端、可用的实时语音 AI 凭证。

已验证的模组 USB 组合为 `1e0e:9001`，固件 `SIM7600G_V2.0.2`。内核 `option`
驱动直接提供 tty：接口 `00` DIAG、`01` NMEA、`02` AT、`03` PPP、`04` PCM，
接口 `05` 为 `qmi_wwan`。常见编号是 `ttyUSB2`（AT）和 `ttyUSB4`（PCM），但编号并非稳定标识。
本规则面向一台 Pi 连接一个该型号模组；多个同 VID/PID 模组会争用同名链接，不在本阶段支持范围内。

## 前置条件

以下操作在 **Pi** 上以将来运行服务的普通用户执行。不要使用 `sudo bash install.sh`，
不要以 root 创建仓库、uv Python 或虚拟环境。系统安装步骤会单独提示 sudo。

```bash
sudo apt update
sudo apt install git ffmpeg libportaudio2 libusb-1.0-0 usbutils psmisc pipx
pipx install uv
pipx ensurepath
```

uv 官方安装器默认将可执行文件安装到 `~/.local/bin`；pipx 通常也在该目录暴露命令。
SSH 非交互会话可能不加载交互 shell 的 PATH 配置，不能仅依赖重新登录或 `pipx ensurepath`。
在执行安装脚本的同一 shell 中先运行 `export PATH="$HOME/.local/bin:$PATH"`，
确认 `uv --version` 和 `ffmpeg -version` 可用。安装器优先使用 PATH 中的 uv；找不到时，
还会探测可执行的 `$HOME/.local/bin/uv`，并打印实际使用的路径。
两处都找不到时只给安装指引，不会自动安装 uv。

依赖说明：`libportaudio2` 供现有
`sounddevice` 导入使用；系统 Python 3.11 不满足项目要求，安装器用 `uv python install 3.13`
安装用户态 Python，再用 `uv python find 3.13` 定位，不替换系统 Python。
没有 uv 时安装器只打印[官方安装指引](https://docs.astral.sh/uv/getting-started/installation/)并退出，
不会自动下载执行安装脚本。

准备当前版本的仓库；下文 `/path/to/callpilot` 是占位路径。仓库须由服务用户可读写，且登录后路径
始终可用。安装器支持包含空格的路径，但拒绝非 ASCII 字符与 systemd/替换元字符，以免错误展开。
确认系统有 `dialout` 组、systemd 和 udev。首次依赖安装需要联网，aarch64 的依赖 wheel 是否齐全
仍要在目标机实际安装确认；失败时保留报错，不绕过依赖或修改系统 Python。

## 安装与配置

先确认没有进行中的通话。重复安装会重载规则并触发 USB 串口事件，因此也应安排在空闲窗口。

```bash
cd /path/to/callpilot
export PATH="$HOME/.local/bin:$PATH"
bash scripts/linux/install.sh
```

安装器复用 `PYTHON=<uv 解释器> bash scripts/setup.sh` 创建/复用 `.venv` 并安装依赖。
已有 `.env` 原样保留；不存在时由原 setup 复制根目录 `.env.example`，**它不是 Pi 配置**，
必须完成下一步再启动或重启 Pi。旧的 Python 3.11 虚拟环境会被拒绝，需人工移走后重跑，不会自动删除。

sudo 步骤只安装这两个规则和一个 unit、追加用户的 dialout 组权限、重载/触发 udev、enable 服务：

- `/etc/udev/rules.d/77-callpilot-mm-ignore-sim7600.rules`：仅忽略此 VID/PID，不全局禁用 ModemManager。
- `/etc/udev/rules.d/99-callpilot-sim7600.rules`：接口 `02` → `/dev/callpilot-sim7600-at`，
  接口 `04` → `/dev/callpilot-sim7600-pcm`，串口归 `dialout`、权限 `0660`。
- `/etc/systemd/system/callpilot-edge.service`：普通用户、仓库工作目录、`.venv/bin/python app.py`，
  等待网络、失败后 5 秒重启、输出到 journald。停止时发 SIGINT，复用入口已有的清理逻辑。

默认只 **enable 开机启动，不立即启动**；这不停止已经运行的旧服务。
`--now` 会执行 `systemctl enable --now`，仅在 `.env` 已准备好时使用；它不会重启已运行的服务。
安装器重复运行不会覆盖 `.env`，不会重建有效 `.venv`，但会重新执行依赖安装及部署文件更新。
仓库迁移路径后必须重跑安装器并安排服务重启，不能只移动文件。

编辑仓库 `.env`，将现有同名键改为下列值（不要用 Linux 示例覆盖整个文件，也不要追加重复键）：

```dotenv
MODEM_VENDOR=simcom
MODEM_PORT=/dev/callpilot-sim7600-at
MODEM_AUDIO_MODE=nmea
MODEM_PCM_PORT=/dev/callpilot-sim7600-pcm
MODEM_PCM_BAUD=115200
MODEM_PCM_PIN_8K=true
MODEM_RESET_AFTER_CALL=true
WEB_OPEN_BROWSER=false
WEB_HOST=127.0.0.1
WEB_PORT=47100
```

这份覆盖清单也位于 [`scripts/linux/.env.example`](../scripts/linux/.env.example)，安装结束会打印。
现有 provider 凭证、通话策略、云功能配置仍按需保留；例如默认 `AGENT_PROVIDER=qwen` 时需填写
`DASHSCOPE_API_KEY`。云连接为可选，不必为了本地来电验收打开 `REMOTE_CLOUD_ENABLED`。
不要将 `.env`、凭证、录音、完整日志或身份信息加入仓库或报告。

`MODEM_PCM_BAUD=115200` 只是此 Linux 配置的覆盖，项目原默认 `921600` 不变；Linux USB tty 的
波特率不决定 PCM 字节率。音频仍是 **8kHz、s16le、单声道、20ms / 320B**。

新开关均为 bool：

| 配置项 | Linux 默认 | macOS 默认 | Windows 默认 | 用途 |
| --- | --- | --- | --- | --- |
| `MODEM_PCM_PIN_8K` | true | false | false | SIM7600 每通在 `CPCMREG=1` 前发 `CPCMBANDWIDTH=1,1` |
| `MODEM_RESET_AFTER_CALL` | true | false | false | 仅 SIM7600：本通成功启用过 PCM 时，收尾后重启模组；修改需重启 Edge |
| `WEB_OPEN_BROWSER` | true | true | true | 源码入口启动时打开浏览器；frozen 原有不打开逻辑不变 |

根 `.env.example` 将 PCM 与通话后重启开关保留为注释，避免覆盖平台默认值。
显式环境变量或 `.env` 设置可以覆盖默认。
带宽命令失败仅告警，不阻断原有 CPCMREG 重试；这不代表失败后采样率已正确，必须看真机音频。
systemd unit 另外强制设置 `WEB_OPEN_BROWSER=false`，其环境优先于 dotenv，因此即使旧 `.env`
写了 true，服务也不会尝试启动浏览器。

```bash
chmod 600 .env
sudo systemctl start callpilot-edge.service
```

也可在配置好后重跑 `bash scripts/linux/install.sh --now`。已运行服务应用 `.env` 变更时，
在无通话窗口显式执行 `sudo systemctl restart callpilot-edge.service`。
不要同时启动手工 `app.py` 和 systemd 服务，否则串口和监听端口会冲突。

## 分层验证

### 1. 串口链接与权限（Pi 上）

```bash
lsusb -d 1e0e:9001
ls -l /dev/callpilot-sim7600-at /dev/callpilot-sim7600-pcm
readlink -f /dev/callpilot-sim7600-at
readlink -f /dev/callpilot-sim7600-pcm
udevadm info --attribute-walk --name=/dev/callpilot-sim7600-at
id -nG
```

确认链接最终对应接口 `02` / `04`、目标 tty 为 `dialout` / `0660`。追加用户组后，交互 shell
需要重新登录；systemd 的 `SupplementaryGroups=dialout` 在服务下次启动时生效。
无通话时拔插或重新枚举后再检查链接，不以某个 `ttyUSB` 编号固定不变作为验收标准。

### 2. 服务与 API

```bash
systemctl is-enabled callpilot-edge.service
systemctl status callpilot-edge.service --no-pager
journalctl -u callpilot-edge.service -b -n 100 --no-pager
```

应为 enabled / active，日志没有反复崩溃重启，且无需桌面会话。系统服务日志查询权限不足时加 sudo。
在**当前电脑**打开 SSH 隧道（`SSH_TARGET` 是待替换的 SSH 配置别名或目标占位符，无设备真实地址）：

```bash
EDGE_SSH_TARGET='SSH_TARGET'
ssh -N -L 47100:127.0.0.1:47100 "$EDGE_SSH_TARGET"
```

保持隧道终端运行，再在当前电脑访问 `http://127.0.0.1:47100/api/meta` 或仪表盘：

```bash
curl --fail --silent --show-error http://127.0.0.1:47100/api/meta
```

若当前电脑该端口已被占用，把 `-L` 的第一个端口换成空闲端口，并同步修改访问 URL。
确认 JSON 中 `hardware.modem_connected=true`、`hardware.port` 指向 AT 稳定链接，provider 凭证已配置；
若仍需设置，先通过隧道完成现有向导。HTTP 200、进程 active、USB 枚举都**不能证明通话音频正常**。
现有 `hardware.usb_online` 只探测 Quectel VID，在 SIM7600 上可能为 false；本阶段不改该字段，
不要用它判定 SIM7600 离线，应结合原生 tty、`modem_connected` 与实际通话。
不将完整 API 响应公开，报告只摘取脱敏状态。

管理服务保持回环监听；不要改成对所有网卡开放，不要将管理端口转发到公网或云隧道。

### 3. 来电与开机恢复（真机验收）

由真机执行者从获准测试端拨入一次，确认 AI 接听、对方能听到 AI、AI 能听懂对方、挂断正常。
日志应显示本通先钉定 `CPCMBANDWIDTH=1,1`、再成功 `CPCMREG=1`，无串口写超时；
记录脱敏的命令顺序、PCM 字节率与双向可闻结论，不用本机单测结果替代。
supervisor 无活跃通话时尝试 `CPCMREG=1` 返回 ERROR 属预期，重点检查接通后的 per-call 成功。

### 4. 每通后重启自愈（#121，真机验收）

已实测的跨通劣化可能在第二通 `CPCMREG=1` 首次就回 OK，但 PCM 上行零字节、下行写超时，
因此不能只靠原有「CPCMREG 重试才判劣化」哨兵。Linux 默认启用 `MODEM_RESET_AFTER_CALL`：
仅当本通接听/拨号后成功启用过 PCM，挂断与关闭语音通道完成后发送一次 `AT+CRESET`。
未启用 PCM 的未接通呼叫、启动期 supervisor 单独启用 PCM 都不会触发。
该策略仅对 SIM7600 生效，其他厂商不受影响；原有哨兵写入机制保留不变。

**每次符合条件的通话结束后，约 25 秒内无法接打电话。** 这是模组重启、USB 重枚举和重新注册
造成的计划内中断，不是无缝恢复承诺。恢复速度取决于供电、USB 和网络，可能更久。
重启期 API 的 `hardware.modem_connected` 应为 false，外呼返回 `MODEM_OFFLINE`，
外呼队列暂停推进，恢复后继续。不要在此窗口用另一个进程打开 AT/PCM 口。

```bash
journalctl -u callpilot-edge.service -b --no-pager | grep -E '#121|phase=|重连 worker'
```

同一轮应按序看到 `SIM7600 #121 post-call reset` 的 `phase=reset_sent`、`phase=ports_gone`、
`phase=ports_back`、`phase=ready`；每条 `elapsed_seconds` 都从发起本轮恢复计时，最后一条为总耗时。
只有 AT 与 PCM 链接均消失、均稳定恢复，重新连接、SIM 身份刷新以及 SIM/网络就绪检查完成后
才重新标记在线。刚重连时带宽钉定可能 ERROR，仅告警；接通时仍会重新钉定 8k。
`CRESET` 响应不确定也不会重发，而是继续等待实际重枚举，避免重复重启。

主动恢复窗口约 90 秒，失败会记录错误并交回现有看门狗；窗口内不会等看门狗长阈值才首次重连。
若某个底层重连调用超过期限仍不返回，协调线程会退出，但保留串口恢复占用，阻止看门狗重复开口；
该调用退出后释放占用再交回看门狗。永久卡死的驱动调用仍需人工排查，不能靠创建更多线程解决。
本检查接受 LTE 或 CS 注册就绪，但不改变既有外呼门禁的 CS 注册策略。

请在完整 Edge 下连续完成至少两通双向音频测试，逐通确认仅一次重启、四阶段日志、
中断窗口的 API/外呼门禁和恢复后下一通音频；不能用此前的手工脚本结果代替这轮验收。

保存配置并在空闲窗口由真机执行者安排重启 Pi，验证不登录桌面也能自动恢复服务、串口连接和 API，
再确认来电。断电重启、实际双向音频、aarch64 依赖安装和真实 udev 匹配都需要真机证据，
不在本地代码交付时标为已通过；VoLTE-only 外呼门禁仍留 Phase 2。

## 排障

| 现象 | 检查与处理 |
| --- | --- |
| 插拔后零 USB 事件、`lsusb` 无设备 | `sudo journalctl -k -f` 观察重新插线；优先更换确认支持数据的 USB 线。仅充电线是已验证原因，不先改驱动；同时检查端口、转接板和供电。 |
| 串口占用或 AT 响应被截走 | 检查 `systemctl status ModemManager`，用 `sudo fuser -v /dev/callpilot-sim7600-at /dev/callpilot-sim7600-pcm` 找持有者，确认忽略规则已安装。空闲时重插模组使规则重新生效，不全局禁用 ModemManager，也不盲目杀进程。 |
| 稳定链接缺失 | 检查 VID/PID 和 `udevadm info --attribute-walk` 的接口号。规则分两步匹配设备与接口；不要把不同祖先上的 VID/PID 和接口 ATTRS 合并到同一规则。 |
| `Permission denied` | 检查 tty 权限及 dialout 组、重新登录或在空闲时重启服务；不要用 `chmod 666` 或 root 运行绕过。 |
| `CREG: 0,3` 且 `CEREG: 0,1` | 在此已验证卡/网络组合上属于 VoLTE-only 正常态：CS 拒绝但 LTE 已注册，不据此断言 SIM 不可用。仍需实际来电确认 IMS；当前外呼守卫只看 CS 注册，可能拦截外呼，Phase 2 再处理。 |
| 通话接通但噪声、变速或单向无声 | 核对 Linux 配置与本通带宽钉定日志；出厂 VoLTE PCM 常为 16k，不能按 8k 解码。确认 PCM 链接为接口 `04`，不要把接口 `01` 的 NMEA 当 PCM，也不要只调串口波特率。 |
| USB 掉线、通话时重新枚举 | 在通话前后检查 `vcgencmd get_throttled`（需 Pi 固件工具），理想为 `0x0`；非零时区分当前/历史欠压或降频位，检查电源、供电线、有源 Hub 和散热。命令缺失不代表供电正常。 |
| 第二通 PCM 零上行、下行写超时 | 确认 `MODEM_RESET_AFTER_CALL=true` 且已重启 Edge，核对上一通的四阶段日志。CPCMREG 首次 OK 不代表端点健康。原有 `/tmp/sim7600-recover-request` 哨兵在 Linux 无消费者，本功能独立于哨兵主动重启；systemd 进程重启不等于模组重启。 |
| 服务起不来或启动限流 | 看 journald；重点排查未合并 Linux `.env`（根模板是其他平台配置）、凭证、Python/系统库、端口占用。修正后在空闲窗口执行 `sudo systemctl reset-failed callpilot-edge.service` 再 start。 |

规则匹配依据：[udev 官方说明](https://www.freedesktop.org/software/systemd/man/latest/udev.html)
要求同一条规则中的父设备匹配键命中同一祖先，因而设备 ID 与接口号分开匹配。
服务生命周期语义见 [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)。

## 本地质量门与交接

```bash
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/mypy
.venv/bin/mypy --platform win32
.venv/bin/mypy --platform linux
bash -n scripts/linux/install.sh
# 有 shellcheck 时另外执行：
shellcheck scripts/linux/install.sh
```

以上测试不打开串口、不部署 systemd，也不发送真实 AT。交接时分别记录本地质量门结果和真机待验收项；
不要将既有手工音频脚本的成功等同于完整 Edge 的部署与通话成功。共享文件仍需其他平台维护者 review。
