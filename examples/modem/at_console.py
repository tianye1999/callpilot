"""原子能力：发送任意 AT 指令，打印模组原始响应。

这是最底层的一块积木——所有电话/短信能力最终都是 AT 指令。

用法：
    python examples/modem/at_console.py "AT+CSQ"     # 单条，打完即退
    python examples/modem/at_console.py              # 交互式，逐行输入，空行退出
"""

from __future__ import annotations

import sys

from _common import make_modem, setup_logging


def main() -> None:
    setup_logging()
    modem = make_modem()
    modem.connect()
    try:
        if len(sys.argv) > 1:
            # 命令行给了指令：发一条就退。
            print(modem.send_command(" ".join(sys.argv[1:])).strip())
            return
        # 交互式：逐行读，空行退出。
        print("输入 AT 指令（空行退出）：")
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                break
            print(modem.send_command(line).strip())
    finally:
        modem.close()


if __name__ == "__main__":
    main()
