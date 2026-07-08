"""原子能力：发送 DTMF 按键音（IVR 电话菜单导航，AT+QVTS / AT+VTS）。

⚠️ 需在**通话进行中**运行才有意义（按键音要发到已建立的语音通道里）。
   典型用法：先用 dial_call.py 拨通一个 IVR，再在另一个终端跑本脚本按键。

用法：python examples/modem/send_dtmf.py <按键序列>
例：  python examples/modem/send_dtmf.py 1
      python examples/modem/send_dtmf.py 103#     # 允许 0-9 * # A-D
"""

from __future__ import annotations

import sys

from _common import make_modem, setup_logging


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: send_dtmf.py <按键序列，如 103#>")
        sys.exit(1)
    digits = sys.argv[1]

    setup_logging()
    modem = make_modem()
    modem.connect()
    try:
        ok = modem.send_dtmf(digits)
        print(f"✅ 已发送 {digits}" if ok else "❌ 发送失败（按键非法或无通话）")
    finally:
        modem.close()


if __name__ == "__main__":
    main()
