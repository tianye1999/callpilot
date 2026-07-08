"""原子能力：发送一条短信（中文自动走 UCS2 编码，英文/数字走 GSM）。

用法：python examples/modem/send_sms.py <号码> <正文…>
例：  python examples/modem/send_sms.py 10086 CXLL
      python examples/modem/send_sms.py <手机号> 你好，这是一条测试短信
"""

from __future__ import annotations

import sys

from _common import make_modem, setup_logging


def main() -> None:
    if len(sys.argv) < 3:
        print("用法: send_sms.py <号码> <正文>")
        sys.exit(1)
    number = sys.argv[1]
    text = " ".join(sys.argv[2:])

    setup_logging()
    modem = make_modem()
    modem.connect()
    try:
        ok = modem.send_sms(number, text)
        print("✅ 已发送" if ok else "❌ 发送失败")
    finally:
        modem.close()


if __name__ == "__main__":
    main()
