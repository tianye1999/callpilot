"""原子能力：设备自检——查 SIM、信号、网络、语音通道、固件。

实拨电话前先跑它，确认模组/SIM 就绪。纯查询，不发起通话，安全。

用法：python examples/modem/probe_device.py
"""

from __future__ import annotations

from _common import make_modem, setup_logging

# (说明, AT 指令) —— 每条都是一个独立的原子查询。
CHECKS = [
    ("SIM 卡状态 (CPIN)", "AT+CPIN?"),
    ("信号强度 (CSQ)", "AT+CSQ"),
    ("网络注册 (CREG)", "AT+CREG?"),
    ("当前运营商 (COPS)", "AT+COPS?"),
    ("语音 PCM 通道 (QPCMV)", "AT+QPCMV?"),
    ("固件版本 (CGMR)", "AT+CGMR"),
]


def main() -> None:
    setup_logging()
    modem = make_modem()
    modem.connect()
    try:
        for label, cmd in CHECKS:
            print(f"── {label} :: {cmd}")
            print(modem.send_command(cmd).strip() or "(无响应)")
            print()
    finally:
        modem.close()


if __name__ == "__main__":
    main()
