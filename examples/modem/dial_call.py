"""原子能力：外呼一个号码，等待接通，保持若干秒后挂断。

用法：python examples/modem/dial_call.py <号码> [保持秒数=15]

⚠️ 真实拨打，可能产生话费；请拨你有权拨打的号码（如自己的手机、运营商热线）。
"""

from __future__ import annotations

import sys
import threading
import time

from _common import make_modem, setup_logging

CONNECT_TIMEOUT = 45  # 与主程序一致的接通等待上限


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: dial_call.py <号码> [保持秒数]")
        sys.exit(1)
    number = sys.argv[1]
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    setup_logging()
    modem = make_modem()
    modem.connect()

    connected = threading.Event()
    modem.on_call_connected(lambda num: (print(f"✅ 已接通: {num}"), connected.set()))
    modem.on_hangup(lambda: print("通话结束 (NO CARRIER)"))
    # 接通检测靠 CLCC 轮询，必须启动监听线程。
    modem.start_listener()

    try:
        modem.dial(number)
        print(f"拨号中 -> {number} …")
        if connected.wait(timeout=CONNECT_TIMEOUT):
            print(f"保持通话 {hold}s（对方先挂则提前结束）…")
            for _ in range(hold):
                if not modem.is_call_connected():
                    break
                time.sleep(1)
        else:
            print(f"{CONNECT_TIMEOUT}s 内未接通")
    finally:
        modem.hangup()
        modem.close()


if __name__ == "__main__":
    main()
