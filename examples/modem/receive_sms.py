"""原子能力：监听并打印新到短信（+CMTI 上报 → 读取存储位 → 解析正文）。

用法：python examples/modem/receive_sms.py
然后给这张 SIM 卡发条短信，终端会实时打印发件号码与正文。

（收信靠模组主动上报 +CMTI，由 start_listener 的读循环处理。）
"""

from __future__ import annotations

import time

from _common import make_modem, setup_logging


def main() -> None:
    setup_logging()
    modem = make_modem()
    modem.connect()

    def on_sms(sender: str | None, text: str) -> None:
        print(f"\n📨 新短信 来自 {sender or '未知'}:\n{text}\n")

    modem.on_sms(on_sms)
    modem.start_listener()

    print("等待短信…（Ctrl-C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        modem.close()


if __name__ == "__main__":
    main()
