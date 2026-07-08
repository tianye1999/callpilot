"""集中配置注册表：环境变量读取、凭证校验与 .env 持久化。

roadmap P2-2/P2-5/P2-6 的地基：
- ``CONFIG_SPECS`` 注册全部配置项（类型/默认值/是否可编辑/是否敏感/是否需重启）；
- ``get_str/get_int/get_float/get_bool`` 按类型读取 ``os.environ``，缺省回落注册表默认值；
- ``validate_provider_credentials`` 校验 Agent 提供方所需凭证是否齐全；
- ``read_panel_values`` 供 Web 设置面板渲染（secret 项掩码为「已设置/未设置」）；
- ``update_env_file`` 把修改写回 .env（保留注释与行序）并同步 ``os.environ``。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import platforms

logger = logging.getLogger(__name__)

# get_bool 认定为真的取值（大小写不敏感）。
_TRUTHY = {"true", "1", "yes"}

# bool 类配置在写回 .env 时允许的取值（大小写不敏感）。
_BOOL_WRITABLE = {"true", "false", "1", "0", "yes", "no"}

# 匹配 .env 中的赋值行：可选缩进与 export 前缀 + 变量名 + '='。
_ENV_LINE_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=")

# 值里含这些字符时写回 .env 需要加双引号。
_NEEDS_QUOTING_RE = re.compile(r"[\s#\"']")


@dataclass(frozen=True)
class ConfigSpec:
    """单个配置项的注册信息。

    ``hidden`` 标记内部/调试项：默认值仍由注册表单一出处管理，但不进设置面板
    （``read_panel_values`` 跳过）；hidden 项必须同时 ``editable=False``，
    避免面板渲染不出却能经 API 写入。
    """

    key: str
    label: str
    kind: str  # "str" | "int" | "float" | "bool" | "select"
    default: str
    choices: tuple[str, ...] = ()
    editable: bool = True
    secret: bool = False
    requires_restart: bool = False
    hidden: bool = False


CONFIG_SPECS: tuple[ConfigSpec, ...] = (
    # ---- Agent ----
    ConfigSpec("AGENT_PROVIDER", "Agent 提供方", "select", "qwen",
               choices=("qwen", "doubao", "openai"), requires_restart=True),
    ConfigSpec("DASHSCOPE_API_KEY", "DashScope API Key", "str", "",
               editable=False, secret=True, requires_restart=True),
    ConfigSpec("QWEN_REALTIME_MODEL", "Qwen 实时模型", "str",
               "qwen3.5-omni-flash-realtime", requires_restart=True),
    ConfigSpec("QWEN_VOICE", "Qwen 音色", "str", "Raymond"),
    # 模型显示名只用于 /api/meta 与豆包自我介绍提示词，属内部项不进面板。
    # 显示名用语言中性的品牌名（Qwen/Doubao 是同款产品的国际名），
    # 避免英文界面右上角出现「通义千问」这类中文品牌串。
    ConfigSpec("AGENT_MODEL_NAME", "Qwen 模型显示名", "str",
               "Qwen3.5-Omni", editable=False, hidden=True,
               requires_restart=True),
    ConfigSpec("AGENT_MODEL_NAME_DOUBAO", "豆包模型显示名", "str",
               "Doubao Realtime", editable=False, hidden=True,
               requires_restart=True),
    # 豆包接入的固定资源参数（火山引擎文档给定，一般无需改动）。
    ConfigSpec("DOUBAO_RESOURCE_ID", "豆包资源 ID", "str", "volc.speech.dialog",
               editable=False, hidden=True),
    ConfigSpec("DOUBAO_APP_KEY", "豆包 App Key", "str", "PlgvMymc7f3tQnJ6",
               editable=False, hidden=True),
    # Realtime 端点覆写（调试用）；留空走 dashscope SDK 内置 wss 地址。
    ConfigSpec("DASHSCOPE_REALTIME_URL", "Qwen Realtime 端点覆写", "str", "",
               editable=False, hidden=True),
    # OpenAI Realtime。API Key 仍从环境变量读（凭证校验见 PROVIDER_REQUIRED_KEYS），
    # 此处登记只为在设置面板显示「已设置/未设置」状态（editable=False，不回传真值）。
    ConfigSpec("OPENAI_API_KEY", "OpenAI API Key", "str", "",
               editable=False, secret=True, requires_restart=True),
    ConfigSpec("OPENAI_REALTIME_MODEL", "OpenAI 实时模型", "str",
               "gpt-realtime-mini", requires_restart=True),
    ConfigSpec("OPENAI_VOICE", "OpenAI 音色", "str", "alloy"),
    # 端点覆写（可选）：留空即直连 api.openai.com；仅在用反代/Azure OpenAI，
    # 或所在网络无法直连 OpenAI 时才需要填。
    ConfigSpec("OPENAI_REALTIME_URL", "OpenAI Realtime 端点覆写", "str", "",
               requires_restart=True),
    ConfigSpec("AGENT_MODEL_NAME_OPENAI", "OpenAI 模型显示名", "str",
               "OpenAI Realtime Mini", editable=False, hidden=True,
               requires_restart=True),
    ConfigSpec("OWNER_NAME", "机主姓名", "str", ""),
    # 默认留空：让 prompts.agent_persona() 按 AGENT_LANGUAGE 回退到
    # 「AI 助理」/「AI assistant」，英文模式下不会硬塞中文人设。
    ConfigSpec("AGENT_PERSONA", "AI 人设称谓", "str", ""),
    # AI 通话语言：决定 AI 打/接电话说什么语言、通话摘要用什么语言写；
    # 与前端 UI 语言（localStorage）相互独立。改动需重启会话。
    ConfigSpec("AGENT_LANGUAGE", "AI 通话语言", "select", "zh",
               choices=("zh", "en"), requires_restart=True),
    ConfigSpec("AGENT_OUTBOUND_TASK", "外呼任务指令", "str",
               "代表机主主动外呼，对方接起后自然说明来意，并围绕本次目的简短沟通。"),
    # ---- 模组 ----
    # 默认值按当前平台在模块加载时定死（Windows 为 auto 哨兵，连接时扫描）。
    ConfigSpec("MODEM_PORT", "模组 AT 串口", "str", platforms.default_modem_port(),
               requires_restart=True),
    ConfigSpec("MODEM_BAUD", "串口波特率", "int", "115200", requires_restart=True),
    ConfigSpec("MODEM_AUDIO_MODE", "模组音频模式", "select", platforms.default_audio_mode(),
               choices=("uac_ffmpeg", "uac", "nmea"), requires_restart=True),
    ConfigSpec("MODEM_AUDIO_KEYWORD", "UAC 声卡匹配关键字", "str", "Interface",
               requires_restart=True),
    # nmea 音频模式专用的 PCM 数据串口；uac/uac_ffmpeg 模式留空即可。
    ConfigSpec("MODEM_PCM_PORT", "模组 PCM 串口", "str", "",
               requires_restart=True),
    ConfigSpec("MODEM_PCM_BAUD", "PCM 串口波特率", "int", "921600",
               requires_restart=True),
    ConfigSpec("MODEM_TX_GAIN", "上行发送增益", "float", "1.0"),
    # ---- 通话行为 ----
    ConfigSpec("HALF_DUPLEX_HANGOVER_SECONDS", "半双工挂尾时长（秒）", "float", "0.5"),
    ConfigSpec("HANGUP_TOOL_DELAY_SECONDS", "挂断工具延迟（秒）", "float", "4.5"),
    ConfigSpec("RECORDING_ENABLED", "通话录音开关", "bool", "true"),
    ConfigSpec("RECORDING_RETENTION_DAYS", "录音保留天数", "int", "30"),
    ConfigSpec("SUMMARY_ENABLED", "通话摘要开关", "bool", "true"),
    ConfigSpec("SUMMARY_MODEL", "摘要模型", "str", "qwen-plus"),
    # 摘要 API 超时秒数（真机实测 15s 对长转写不够用），调试项不进面板。
    ConfigSpec("SUMMARY_TIMEOUT", "摘要超时（秒）", "float", "30",
               editable=False, hidden=True),
    # ---- 本地监听 ----
    ConfigSpec("MONITOR_AI_PLAYBACK", "本地监听 AI 语音", "bool", "false"),
    ConfigSpec("MONITOR_OUTPUT_DEVICE", "监听输出设备", "str", "MacBook Air扬声器"),
    ConfigSpec("MONITOR_AI_GAIN", "监听增益（AI 下行）", "float", "1.0"),
    # 电话上行是窄带信号且电平偏低，监听需大幅放大（真机调到 15 倍才够听清）。
    ConfigSpec("MONITOR_UPLINK_GAIN", "监听增益（对方上行）", "float", "8.0"),
    # ---- 白名单与节流 ----
    ConfigSpec("DIAL_WHITELIST", "外呼白名单", "str", ""),
    ConfigSpec("DIAL_INTERVAL_SECONDS", "连续拨号间隔（秒）", "float", "5.0"),
    ConfigSpec("SMS_WHITELIST", "短信白名单", "str", ""),
    # ---- 连接管理 ----
    ConfigSpec("QWEN_PREWARM", "Qwen 连接预热", "bool", "true"),
    ConfigSpec("QWEN_RECONNECT_MAX", "Qwen 最大重连次数", "int", "2"),
    ConfigSpec("OPENAI_RECONNECT_MAX", "OpenAI 最大重连次数", "int", "2"),
    # 预热调优项：单次握手超时与保活周期（秒），一般无需改动，不进面板。
    ConfigSpec("QWEN_PREWARM_TIMEOUT", "Qwen 预热握手超时（秒）", "float", "5.0",
               editable=False, hidden=True),
    ConfigSpec("QWEN_PREWARM_INTERVAL", "Qwen 预热保活间隔（秒）", "float", "240.0",
               editable=False, hidden=True),
    # ---- Web 服务 ----
    ConfigSpec("WEB_HOST", "Web 监听地址", "str", "127.0.0.1",
               editable=False, requires_restart=True),
    ConfigSpec("WEB_PORT", "Web 监听端口", "int", "8000",
               editable=False, requires_restart=True),
)

_SPECS_BY_KEY: dict[str, ConfigSpec] = {spec.key: spec for spec in CONFIG_SPECS}

# Agent 提供方 -> 必需的环境变量（凭证不进注册表，避免出现在面板）。
PROVIDER_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "qwen": ("DASHSCOPE_API_KEY",),
    "doubao": ("DOUBAO_APP_ID", "DOUBAO_ACCESS_KEY"),
    "openai": ("OPENAI_API_KEY",),
}


def get_spec(key: str) -> ConfigSpec:
    """按 key 取注册信息；未注册抛 ``KeyError``。"""
    try:
        return _SPECS_BY_KEY[key]
    except KeyError:
        raise KeyError(f"未注册的配置项: {key}") from None


# ---- 类型化读取（读 os.environ，缺省回落注册表默认值）----


def get_str(key: str) -> str:
    """读取字符串配置（select 项同样用本函数读取）。"""
    spec = get_spec(key)
    return os.environ.get(key, spec.default)


def get_int(key: str) -> int:
    """读取整数配置；环境变量非法时告警并回落默认值。"""
    spec = get_spec(key)
    raw = os.environ.get(key, spec.default)
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("配置 %s 的值 %r 不是合法整数，回落默认值 %s", key, raw, spec.default)
        return int(spec.default)


def get_float(key: str) -> float:
    """读取浮点配置；环境变量非法时告警并回落默认值。"""
    spec = get_spec(key)
    raw = os.environ.get(key, spec.default)
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning("配置 %s 的值 %r 不是合法浮点数，回落默认值 %s", key, raw, spec.default)
        return float(spec.default)


def get_bool(key: str) -> bool:
    """读取布尔配置；``true/1/yes``（大小写不敏感）为真，其余一律为假。"""
    spec = get_spec(key)
    raw = os.environ.get(key, spec.default)
    return raw.strip().lower() in _TRUTHY


# ---- 凭证校验 ----


def validate_provider_credentials(provider: str) -> list[str]:
    """校验指定 Agent 提供方所需凭证；返回缺失项的错误消息列表（空列表=通过）。"""
    required = PROVIDER_REQUIRED_KEYS.get(provider)
    if required is None:
        known = ", ".join(sorted(PROVIDER_REQUIRED_KEYS))
        return [f"未知的 Agent 提供方: {provider}（可选: {known}）"]
    errors: list[str] = []
    for key in required:
        if not os.environ.get(key, "").strip():
            errors.append(f"缺少环境变量 {key}（{provider} 必需）")
    return errors


# ---- 面板读取 ----


def read_panel_values() -> list[dict]:
    """返回面板渲染所需的配置列表（spec 字段 + 当前生效值）。

    hidden 项（内部/调试）不返回；secret 项不回传真实值，
    掩码为「已设置」/「未设置」。所有字段均可直接 JSON 序列化。
    """
    rows: list[dict] = []
    for spec in CONFIG_SPECS:
        if spec.hidden:
            continue
        if spec.secret:
            value = "已设置" if os.environ.get(spec.key, "").strip() else "未设置"
        else:
            value = os.environ.get(spec.key, spec.default)
        rows.append({
            "key": spec.key,
            "label": spec.label,
            "kind": spec.kind,
            "default": spec.default,
            "choices": list(spec.choices),
            "editable": spec.editable,
            "secret": spec.secret,
            "requires_restart": spec.requires_restart,
            "value": value,
        })
    return rows


# ---- .env 写回 ----


def _check_value(spec: ConfigSpec, value: str) -> None:
    """按 kind 校验待写入的值；非法抛 ``ValueError``。"""
    if not isinstance(value, str):
        raise ValueError(f"配置 {spec.key} 的值必须是字符串，收到 {type(value).__name__}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"配置 {spec.key} 的值不允许包含换行")
    if spec.kind == "select" and value not in spec.choices:
        raise ValueError(
            f"配置 {spec.key} 只能取 {', '.join(spec.choices)}，收到 {value!r}"
        )
    if spec.kind == "int":
        try:
            int(value.strip())
        except ValueError:
            raise ValueError(f"配置 {spec.key} 需要整数，收到 {value!r}") from None
    if spec.kind == "float":
        try:
            float(value.strip())
        except ValueError:
            raise ValueError(f"配置 {spec.key} 需要数字，收到 {value!r}") from None
    if spec.kind == "bool" and value.strip().lower() not in _BOOL_WRITABLE:
        raise ValueError(
            f"配置 {spec.key} 需要布尔值（true/false/1/0/yes/no），收到 {value!r}"
        )


def _format_assignment(key: str, value: str) -> str:
    """生成 ``KEY=value`` 赋值文本；值含空白/#/引号时加双引号并转义。"""
    if value == "" or not _NEEDS_QUOTING_RE.search(value):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def update_env_file(updates: dict[str, str], env_path: str | Path = ".env") -> list[str]:
    """把配置写回 .env 并同步 ``os.environ``；返回实际写入的 key 列表。

    - 保留原文件注释、空行与行序；已有 key 原地替换（含重复行），新 key 追加尾部；
    - 仅允许注册表中 ``editable`` 的 key，且值需通过 kind 校验；
      任一 key 非法则整批拒绝（抛 ``ValueError``，文件与环境均不改动）；
    - 文件不存在时自动创建；``updates`` 为空时不落盘，直接返回空列表。
    """
    # 先整批校验，保证要么全部写入、要么全不写入。
    for key, value in updates.items():
        spec = _SPECS_BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"未注册的配置项: {key}")
        if not spec.editable:
            raise ValueError(f"配置 {key} 不允许在面板编辑")
        _check_value(spec, value)

    if not updates:
        return []

    path = Path(env_path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    replaced: set[str] = set()
    for i, line in enumerate(lines):
        match = _ENV_LINE_RE.match(line)
        if match is None:
            continue
        key = match.group(2)
        if key in updates:
            lines[i] = f"{match.group(1)}{_format_assignment(key, updates[key])}"
            replaced.add(key)
    for key, value in updates.items():
        if key not in replaced:
            lines.append(_format_assignment(key, value))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value
    logger.info("已更新 %s: %s", path, ", ".join(updates))
    return list(updates)


__all__ = [
    "CONFIG_SPECS",
    "ConfigSpec",
    "PROVIDER_REQUIRED_KEYS",
    "get_bool",
    "get_float",
    "get_int",
    "get_spec",
    "get_str",
    "read_panel_values",
    "update_env_file",
    "validate_provider_credentials",
]
