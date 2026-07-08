"""原子能力：等待来电 → 自动接听 (ATA) → 保持通话直到对方挂断或 Ctrl-C。

用法：python examples/modem/answer_call.py
然后用另一部手机拨打这张 SIM 卡的号码。
"""

from __future__ import annotations

import time

from _common import make_modem, setup_logging


def main() -> None:
    setup_logging()
    modem = make_modem()
    modem.connect()

    def on_ring(caller: str | None) -> None:
        print(f"📞 来电: {caller or '未知'} → 接听")
        modem.answer()

    modem.on_ring(on_ring)
    modem.on_hangup(lambda: print("通话结束"))
    modem.start_listener()

    print("等待来电…（Ctrl-C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if modem.is_call_connected():
            modem.hangup()
        modem.close()


if __name__ == "__main__":
    main()
