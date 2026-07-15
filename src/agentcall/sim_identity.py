"""SIM 卡运营商识别:IMSI(PLMN 前缀)→ 运营商 → 免费客服号。

背景(issue #88):开发/使用中会换卡,系统不感知运营商变化——测试拨号目标
(免费客服热线)随卡而变,拨错跨运营商客服号会按普通通话计费(2026-07-13
实测);换卡后一段时间网络未注册,拨号 45s 超时而用户无从自诊。

本模块只做纯函数解析(可独测):IMSI/CREG 原始响应 → 结构化身份。
PLMN(MCC+MNC)→ 运营商映射是公开电信标准的确定性事实数据,不属于
「对话逻辑枚举」,不违反项目非枚举硬原则。

AT 交互与缓存在 modem 层(Eg25Modem.refresh_sim_identity)。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace

# 中国大陆四大运营商 PLMN(MCC=460 + MNC)。来源:公开号段分配(ITU/工信部),
# 与 ~/.claude skill callpilot-sim-check、issue #72/#88 一致。
_PLMN_CARRIERS: dict[str, str] = {
    # 中国移动
    "46000": "中国移动", "46002": "中国移动", "46004": "中国移动",
    "46007": "中国移动", "46008": "中国移动", "46013": "中国移动",
    # 中国联通
    "46001": "中国联通", "46006": "中国联通", "46009": "中国联通",
    # 中国电信
    "46003": "中国电信", "46005": "中国电信", "46011": "中国电信",
    "46012": "中国电信",
    # 中国广电(工信部第四家基础运营商,700MHz)
    "46015": "中国广电",
}

# 运营商 → 免费客服热线(真机拨测唯一允许的目标,见 CLAUDE.md 硬约束)。
_SERVICE_NUMBERS: dict[str, str] = {
    "中国移动": "10086",
    "中国联通": "10010",
    "中国电信": "10000",
    "中国广电": "10099",
}

_IMSI_RE = re.compile(r"\b(\d{14,15})\b")
_CREG_RE = re.compile(r"\+CREG:\s*(?:\d+\s*,\s*)?(\d+)(?:\s|$)")

# CREG <stat> 语义(3GPP TS 27.007):1=已注册(本地),5=已注册(漫游)。
_REGISTERED_STATS = {"1", "5"}
_CREG_LABELS = {
    "0": "未注册",
    "1": "已注册",
    "2": "搜网中",
    "3": "注册被拒",
    "4": "未知",
    "5": "已注册(漫游)",
}


@dataclass(frozen=True)
class SimIdentity:
    """一张 SIM 的结构化身份;字段全部可安全对外(不含完整 IMSI)。"""

    present: bool            # 是否成功读到 SIM(CIMI 有响应)
    plmn: str                # IMSI 前 5 位(如 46011);未读到为 ""
    carrier: str             # 运营商中文名;未识别为 "未知"
    service_number: str      # 该运营商免费客服号;未识别为 ""
    registered: bool         # CS 域已注册(CREG 1/5)
    reg_status: str          # 注册状态人话(已注册/搜网中/…)

    def as_dict(self) -> dict:
        return asdict(self)


UNKNOWN_SIM = SimIdentity(
    present=False, plmn="", carrier="未知", service_number="",
    registered=False, reg_status="未知",
)


def parse_imsi(raw: str) -> str:
    """从 AT+CIMI 原始响应提取 IMSI(14-15 位数字);无则返回 ""。

    响应形如 ``460110123456789\\r\\n\\r\\nOK``;ERROR/+CME ERROR(SIM 未插/
    未就绪)则匹配不到数字串。
    """
    if not raw or "ERROR" in raw.upper():
        return ""
    m = _IMSI_RE.search(raw)
    return m.group(1) if m else ""


def parse_creg(raw: str) -> tuple[bool, str]:
    """从 AT+CREG? 原始响应解析 (是否已注册, 状态人话)。"""
    m = _CREG_RE.search(raw or "")
    if not m:
        return False, "未知"
    stat = m.group(1)
    return stat in _REGISTERED_STATS, _CREG_LABELS.get(stat, f"状态{stat}")


def identify(imsi_raw: str, creg_raw: str = "") -> SimIdentity:
    """由 CIMI/CREG 原始响应合成 SimIdentity(纯函数,幂等)。"""
    imsi = parse_imsi(imsi_raw)
    if not imsi:
        registered, reg_status = parse_creg(creg_raw)
        return SimIdentity(
            present=False, plmn="", carrier="未知", service_number="",
            registered=registered, reg_status=reg_status,
        )
    plmn = imsi[:5]
    carrier = _PLMN_CARRIERS.get(plmn, "未知")
    registered, reg_status = parse_creg(creg_raw)
    return SimIdentity(
        present=True,
        plmn=plmn,
        carrier=carrier,
        service_number=_SERVICE_NUMBERS.get(carrier, ""),
        registered=registered,
        reg_status=reg_status,
    )


def with_registration(identity: SimIdentity, creg_raw: str) -> SimIdentity:
    """Return ``identity`` with only its cached CREG state updated."""
    registered, reg_status = parse_creg(creg_raw)
    return replace(identity, registered=registered, reg_status=reg_status)
