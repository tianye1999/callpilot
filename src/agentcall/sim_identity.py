"""SIM 卡运营商识别:IMSI(PLMN 前缀)→ 运营商 → 免费客服号。

背景(issue #88):开发/使用中会换卡,系统不感知运营商变化——测试拨号目标
(免费客服热线)随卡而变,拨错跨运营商客服号会按普通通话计费(2026-07-13
实测);换卡后一段时间网络未注册,拨号 45s 超时而用户无从自诊。

本模块只做纯函数解析(可独测):IMSI/CREG/CEREG 原始响应 → 结构化身份。
PLMN(MCC+MNC)→ 运营商映射是公开电信标准的确定性事实数据,不属于
「对话逻辑枚举」,不违反项目非枚举硬原则。

AT 交互与缓存在 modem 层(Eg25Modem.refresh_sim_identity)。
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, replace

# 中国大陆四大运营商 PLMN(MCC=460 + MNC)。来源:公开号段分配(ITU/工信部),
# 与 ~/.claude skill callpilot-sim-check、issue #72/#88 一致。
logger = logging.getLogger(__name__)

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

# 国家级兜底：MCC → (展示用运营商名, 免费客服号)。仅用于 PLMN 精确表未命中时。
#
# 为什么按 MCC 而不是逐家 MNC（WIL-133）：611 是 NANP 定义的**全国通用**运营商
# 客服短码，AT&T / Verizon / T-Mobile 乃至各家 MVNO 都拨得通。逐家列 MNC 反而
# 更差——MVNO 会漏、并购一发生就漂移，而漏一个就等于误拨保护对那张卡失效。
# 安全相关的字段（号码）给精确值，展示字段（运营商名）诚实到国家粒度即可。
#
# 另注：美国 MNC 是 3 位（如 AT&T 310-410），而 plmn 取的是 imsi[:5]，切出来是
# MCC+MNC 前两位，本来就对不齐——这也是必须按 MCC 兜底的原因。
_MCC_FALLBACK: dict[str, tuple[str, str]] = {
    "310": ("美国运营商", "611"),
    "311": ("美国运营商", "611"),
    "312": ("美国运营商", "611"),
    "313": ("美国运营商", "611"),
    "316": ("美国运营商", "611"),
}

# 可作为人工覆盖值的合法客服号 = 上面两张表的全部取值。限定在这个集合里，是因为
# 误填会让保护**反向失效**：移动卡误填 10010 时，本卡免费的 10086 被拦、而对
# 移动收费的 10010 反而放行（Codex 评审 P1 复现）。
VALID_SERVICE_NUMBERS: frozenset[str] = frozenset(_SERVICE_NUMBERS.values()) | {
    number for _carrier, number in _MCC_FALLBACK.values()
}

_IMSI_RE = re.compile(r"\b(\d{14,15})\b")
_CREG_RE = re.compile(r"\+CREG:\s*(?:\d+\s*,\s*)?(\d+)(?:\s|$)")
_EPS_RE = re.compile(r"\+C[EG]REG:\s*(?:\d+\s*,\s*)?(\d+)(?=\s*(?:,|$))", re.MULTILINE)

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
    reg_status: str          # CS 域注册状态人话(已注册/搜网中/…)
    eps_registered: bool = False  # EPS 域已注册(CEREG 1/5)
    eps_status: str = "未知"

    @property
    def network_attached(self) -> bool:
        """CS 或 EPS 任一域已注册;VoLTE-only 卡不要求 CS 域注册。"""
        return self.registered or self.eps_registered

    def as_dict(self) -> dict:
        return {**asdict(self), "network_attached": self.network_attached}


UNKNOWN_SIM = SimIdentity(
    present=False, plmn="", carrier="未知", service_number="",
    registered=False, reg_status="未知",
)


def identify_hfp(properties: dict) -> SimIdentity:
    """HFP exposes network identity, not IMSI or direct SIM presence."""
    carrier = str(properties.get("Name") or "未知")
    status = str(properties.get("Status") or "unknown")
    registered = status in {"registered", "roaming"}
    return SimIdentity(
        present=registered, plmn="", carrier=carrier,
        service_number=_SERVICE_NUMBERS.get(carrier, ""), registered=registered,
        reg_status="已注册" if registered else "未注册",
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


def parse_eps_registration(raw: str) -> tuple[bool, str]:
    """解析 CEREG/CGREG 查询或 URC,允许携带位置等扩展字段。"""
    m = _EPS_RE.search(raw or "")
    if not m:
        return False, "未知"
    stat = m.group(1)
    return stat in _REGISTERED_STATS, _CREG_LABELS.get(stat, f"状态{stat}")


def identify(imsi_raw: str, creg_raw: str = "", cereg_raw: str = "") -> SimIdentity:
    """由 CIMI/CREG/CEREG 原始响应合成 SimIdentity(纯函数,幂等)。"""
    imsi = parse_imsi(imsi_raw)
    registered, reg_status = parse_creg(creg_raw)
    eps_registered, eps_status = parse_eps_registration(cereg_raw)
    if not imsi:
        return SimIdentity(
            present=False, plmn="", carrier="未知", service_number="",
            registered=registered, reg_status=reg_status,
            eps_registered=eps_registered, eps_status=eps_status,
        )
    plmn = imsi[:5]
    carrier = _PLMN_CARRIERS.get(plmn)
    service_number = _SERVICE_NUMBERS.get(carrier or "", "")
    if carrier is None:
        # 精确表未命中再退到国家粒度（WIL-133）。不这么做的话，非大陆卡的
        # service_number 恒为空，dial_guard 的误拨保护会整个失效——而跨运营商
        # 客服号按普通通话计费，从美国卡拨 10086 更是国际长途。
        carrier, service_number = _MCC_FALLBACK.get(imsi[:3], ("未知", ""))
    return SimIdentity(
        present=True,
        plmn=plmn,
        carrier=carrier,
        service_number=service_number,
        registered=registered,
        reg_status=reg_status,
        eps_registered=eps_registered,
        eps_status=eps_status,
    )


def with_service_number_override(
    identity: SimIdentity, override: str
) -> SimIdentity:
    """用配置的免费客服号覆盖自动识别结果（#72 的兜底项）。

    两种情况需要它：

    1. **识别不到**——非大陆卡、读不到 IMSI、或运营商 PLMN 不在表里。此时
       ``service_number`` 为空，`dial_guard` 的误拨保护会**整个失效**：它靠
       「拨的号在已知客服号里、但不等于本卡客服号」来拦截，本卡客服号为空就
       永远拦不住。跨运营商客服号按普通通话计费（2026-07-13 实测）。
    2. **携号转网**——号在移动、号段/识别结果却指向别家。

    留空 = 按 SIM 自动识别（默认行为不变）。非纯数字的配置一律忽略并回落到
    自动识别：宁可退回已知行为，也不要拿一个畸形号码去喂误拨保护。
    """
    cleaned = (override or "").strip()
    if not cleaned:
        return identity
    # 只接受已知的免费客服号，不接受任意数字串。理由是误填会让保护**反向**失效：
    # 移动卡误填 10010 → 本卡免费的 10086 被拦、对移动收费的 10010 反而放行。
    # 顺带也挡掉「把完整手机号粘进来」这种最危险的输入。
    # （isascii 一并解决全角数字：str.isdigit() 对 "１００８６" 返回 True。）
    if not cleaned.isascii() or cleaned not in VALID_SERVICE_NUMBERS:
        logger.warning(
            "CARRIER_HOTLINE 取值不是已知的免费客服号，已忽略并回落到自动识别"
        )
        return identity
    if identity.service_number and identity.service_number != cleaned:
        # 自动识别成功却与人工覆盖不一致：携号转网时这是对的，填错时这是危险的。
        # 无论哪种都要看得见，不能悄悄改掉一个安全相关的值。
        logger.warning(
            "CARRIER_HOTLINE 覆盖了自动识别结果：识别为 %s(%s)，改用 %s。"
            "若填错，拨本卡免费号会被拦、拨该号可能产生话费。",
            identity.carrier,
            identity.service_number,
            cleaned,
        )
    return replace(identity, service_number=cleaned)


def with_registration(identity: SimIdentity, creg_raw: str) -> SimIdentity:
    """Return ``identity`` with only its cached CREG state updated."""
    registered, reg_status = parse_creg(creg_raw)
    return replace(identity, registered=registered, reg_status=reg_status)


def with_eps_registration(identity: SimIdentity, raw: str) -> SimIdentity:
    """仅更新缓存的 EPS 注册状态,保留 SIM 身份与 CS 状态。"""
    eps_registered, eps_status = parse_eps_registration(raw)
    return replace(identity, eps_registered=eps_registered, eps_status=eps_status)
