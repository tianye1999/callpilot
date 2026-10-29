"""SIM7600 (SIMCom) USB→PTY 桥：把 AT/PCM bulk 接口暴露成 macOS 串口。

复用 ``ec20_usb_pty`` 的桥接/重连机制，仅换 SIMCom VID/PID 与默认接口映射：
  - interface 2 (AT)  -> /tmp/sim7600-at   （MODEM_PORT）
  - interface 4 (PCM) -> /tmp/sim7600-pcm  （MODEM_PCM_PORT，nmea 音频模式读写）

接口号/端点来自 Phase 0 真机探测（scripts/sim7600_probe.py，SIM7600G/PID 9001）。
SIM7600 在语音通话建立瞬间会重枚举 USB，旧句柄失效——``ec20_usb_pty`` 的
等待-重连循环（run/wait_for_device + run_bridges_once(reset_first=…)）会自动
重建 PTY（symlink 指向新 slave），故通话中掉线可自愈。

用法：
    DYLD_LIBRARY_PATH=/opt/homebrew/lib \\
        python scripts/sim7600_usb_pty.py            # 桥 AT+PCM 默认映射
    python scripts/sim7600_usb_pty.py --list          # 仅列接口
    python scripts/sim7600_usb_pty.py --probe          # 逐接口 AT 探测

依赖：pyusb + 系统 libusb（macOS: ``brew install libusb``）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 支持直接 `python scripts/sim7600_usb_pty.py`（把仓库根放进 sys.path 才能 import scripts 包）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from agentcall.modem_hardware import (  # noqa: E402
    RECOVERY_REQUEST_PATH,
    SIM7600_ALT_PID,
    SIM7600_PID,
    SIM7600_VID,
)
from agentcall.modem_hardware import (  # noqa: E402
    SIM7600_MAPS as DEFAULT_MAPS,
)
from scripts import ec20_usb_pty  # noqa: E402  # 需先补 sys.path


def main() -> int:
    return ec20_usb_pty.main(
        default_vid=SIM7600_VID,
        default_pid=SIM7600_PID,
        default_maps=DEFAULT_MAPS,
        prog="sim7600_usb_pty",
        description="SIM7600 USB vendor serial PTY bridge for macOS",
        reset_on_start=True,  # SIM7600 对 USB 故障敏感，首次桥接前先复位清 stall
        # PCM 子系统卡死时自动切到 9011 组合再切回（真机唯一有效的软件恢复手段，
        # CRESET/CFUN 均无效，见 WIL-109）。免去人工物理拔插。
        recover_alt_pid=SIM7600_ALT_PID,
        # app 侧检测到劣化后写此文件请求自愈（覆盖「收数据但播放卡」形态）。
        recovery_request_path=RECOVERY_REQUEST_PATH,
    )


if __name__ == "__main__":
    raise SystemExit(main())
