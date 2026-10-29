"""集中配置注册表：环境变量读取、凭证校验与 .env 持久化。

roadmap P2-2/P2-5/P2-6 的地基：
- ``CONFIG_SPECS`` 注册全部配置项（类型/默认值/是否可编辑/是否敏感/是否需重启）；
- ``get_str/get_int/get_float/get_bool`` 按类型读取 ``os.environ``，缺省回落注册表默认值；
- ``validate_provider_credentials`` 校验 Agent 提供方所需凭证是否齐全；
- ``read_panel_values`` 供 Web 设置面板渲染（secret 项掩码为「已设置/未设置」）；
- ``update_env_file`` 把修改写回 .env（保留注释与行序）并同步 ``os.environ``。
"""

from __future__ import annotations

import codecs
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import platforms

logger = logging.getLogger(__name__)

_ENV_WRITE_LOCK = threading.RLock()

APP_NAME = "CallPilot"

# get_bool 认定为真的取值（大小写不敏感）。
_TRUTHY = {"true", "1", "yes"}

# bool 类配置在写回 .env 时允许的取值（大小写不敏感）。
_BOOL_WRITABLE = {"true", "false", "1", "0", "yes", "no"}

# 匹配 .env 中的赋值行：可选缩进与 export 前缀 + 变量名 + '='。
_ENV_LINE_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=")

# 值里含这些字符时写回 .env 需要加双引号。
_NEEDS_QUOTING_RE = re.compile(r"[\s#\"']")


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


def app_support_dir() -> Path:
    """Per-user writable runtime directory for the bundled app."""
    override = os.environ.get("AGENTCALL_APP_SUPPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if _is_frozen():
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.cwd()


def env_file_path() -> Path:
    override = os.environ.get("AGENTCALL_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return app_support_dir() / ".env"


def data_dir() -> Path:
    override = os.environ.get("AGENTCALL_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if _is_frozen():
        return app_support_dir() / "data"
    return Path.cwd() / "data"


def log_dir() -> Path:
    override = os.environ.get("AGENTCALL_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if _is_frozen():
        return app_support_dir() / "logs"
    return data_dir()


def call_log_dir() -> Path:
    override = os.environ.get("CALL_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return data_dir() / "recordings"


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
    choice_labels: dict[str, str] | None = None
    # 面板可选帮助文本/链接（如音色官网试听页 URL）；空则不渲染。
    help: str = ""


@dataclass(frozen=True)
class KeyValidationResult:
    ok: bool
    status: Literal["valid", "invalid", "network", "unsupported"]
    message: str = ""


CONFIG_SPECS: tuple[ConfigSpec, ...] = (
    # ---- Agent ----
    ConfigSpec("AGENT_PROVIDER", "Agent 提供方", "select", "qwen",
               choices=("qwen", "doubao", "openai", "local"), requires_restart=True,
               choice_labels={"doubao": "doubao (experimental)",
                              "local": "local (三段式，音频不出本机)"}),
    ConfigSpec("DASHSCOPE_API_KEY", "DashScope API Key", "str", "",
               secret=True, requires_restart=True),
    ConfigSpec("QWEN_REALTIME_MODEL", "Qwen 实时模型", "str",
               "qwen3.5-omni-plus-realtime", requires_restart=True),
    # 精选常用音色做下拉;完整 55 种(含方言/多语言)见官网试听页,列表外音色
    # 可直接在 .env 填 QWEN_VOICE(get_str 读环境变量,不受 choices 限制)。
    ConfigSpec("QWEN_VOICE", "Qwen 音色", "select", "Raymond",
               choices=("Raymond", "Ethan", "Tina", "Cindy", "Serena",
                        "Harvey", "Maia", "Sunnybobi"),
               help="https://help.aliyun.com/zh/model-studio/omni-voice-list"),
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
    ConfigSpec("DOUBAO_APP_ID", "豆包 App ID", "str", "",
               secret=True, requires_restart=True),
    ConfigSpec("DOUBAO_ACCESS_KEY", "豆包 Access Key", "str", "",
               secret=True, requires_restart=True),
    # Realtime 端点覆写（调试用）；留空走 dashscope SDK 内置 wss 地址。
    ConfigSpec("DASHSCOPE_REALTIME_URL", "Qwen Realtime 端点覆写", "str", "",
               editable=False, hidden=True),
    # OpenAI Realtime。API Key 仍从环境变量读（凭证校验见 PROVIDER_REQUIRED_KEYS），
    # 此处登记只为在设置面板显示「已设置/未设置」状态（editable=False，不回传真值）。
    ConfigSpec("OPENAI_API_KEY", "OpenAI API Key", "str", "",
               secret=True, requires_restart=True),
    ConfigSpec("OPENAI_REALTIME_MODEL", "OpenAI 实时模型", "select",
               "gpt-realtime-2.1-mini",
               choices=("gpt-realtime-2.1-mini", "gpt-realtime-2.1",
                        "gpt-realtime-2", "gpt-realtime",
                        "gpt-realtime-mini"),
               requires_restart=True),
    # OpenAI Realtime 全部 10 个音色(官方推荐 marin/cedar)。
    ConfigSpec("OPENAI_VOICE", "OpenAI 音色", "select", "alloy",
               choices=("alloy", "ash", "ballad", "coral", "echo",
                        "sage", "shimmer", "verse", "marin", "cedar"),
               help="https://openai.fm"),
    # OpenAI 说话 Vibe（仅 OpenAI 链路）：选中后其风格描述追加在 VOICE_STYLE
    # 之后一并注入会话 instructions（见 prompts.openai_vibe_line 与 openai_agent）。
    # 空 = 不追加，行为与旧版一致；qwen 链路不受影响。参考 https://openai.fm 的 VIBE。
    ConfigSpec("OPENAI_VIBE", "OpenAI 说话 Vibe", "select", "",
               choices=("", "calm", "cheerful", "professional",
                        "warm", "energetic"),
               choice_labels={"": "（默认·不追加）"},
               help="https://openai.fm"),
    # server_vad 判定「对方说完了」所需的静默时长（毫秒）。OpenAI 默认 500；
    # 电话场景往下调可显著缩短「对方说完→AI 开口」的等待（每 100ms 立省 100ms），
    # 代价是对方句中停顿稍长就会被抢话。真机体感 250~350 较自然。0/负值=不下发，
    # 用 OpenAI 默认。仅 OpenAI 链路。
    ConfigSpec("OPENAI_VAD_SILENCE_MS", "OpenAI VAD 静默判停（毫秒）", "int", "300"),
    # server_vad 能量阈值（0~1，OpenAI 默认 0.5）。电话线路底噪会拖住判停：真机
    # 实测音尾→speech_stopped 拖到 0.9~2.7s（静默窗仅 0.3s）。适当抬高让底噪更快
    # 被认作静音、判停更利落；太高会漏掉小声说话的开头。0=用服务端默认。
    ConfigSpec("OPENAI_VAD_THRESHOLD", "OpenAI VAD 能量阈值（0=服务端默认）",
               "float", "0"),
    # 输入降噪（喂给 VAD 与模型前）：near_field=近讲耳机，far_field=远场/电话。
    # 与抬 VAD 阈值同攻「判停拖尾」。空=不启用。
    ConfigSpec("OPENAI_NOISE_REDUCTION", "OpenAI 输入降噪（空/near_field/far_field）",
               "str", ""),
    # 判停方式。server_vad 只看静音时长——静默窗短则抢话（对方「OK…」稍顿就被
    # 接走，2026-08-12 真机复现）、长则每轮都慢，一个旋钮救不了两头。semantic_vad
    # 按语义判断对方是否说完：完整问句立刻接，明显有下文就等。选 semantic_vad 时
    # OPENAI_VAD_SILENCE_MS / OPENAI_VAD_THRESHOLD 不生效。
    ConfigSpec("OPENAI_TURN_DETECTION", "OpenAI 判停方式（server_vad/semantic_vad）",
               "str", "server_vad"),
    # semantic_vad 的积极程度：low=多等、medium、high=尽快接话、auto=默认(medium)。
    ConfigSpec("OPENAI_VAD_EAGERNESS", "semantic_vad 接话积极度", "str", "auto"),
    # 单轮回复的 token 上限（0=不限）。提示词管不住啰嗦（WIL-83/112 实测：明令
    # 「说短点」仍单轮 14~18 秒），本地硬切又会把话切在半截；在 provider 侧限长
    # 才是从源头让它少生成，且转写与对端听到的一致。**注意 audio 与 reasoning
    # token 都计入**：真机实测一句话 ≈ 250（text 90 + audio 110 + reasoning 48），
    # 设 200 会把第一句掐成 incomplete。两三句话 ≈ 900。
    ConfigSpec("OPENAI_MAX_RESPONSE_TOKENS", "OpenAI 单轮回复 token 上限（0=不限）",
               "int", "0"),
    # 端点覆写（可选）：留空即直连 api.openai.com；仅在用反代/Azure OpenAI，
    # 或所在网络无法直连 OpenAI 时才需要填。
    ConfigSpec("OPENAI_REALTIME_URL", "OpenAI Realtime 端点覆写", "str", "",
               requires_restart=True),
    ConfigSpec("AGENT_MODEL_NAME_OPENAI", "OpenAI 模型显示名", "str",
               "OpenAI Realtime", editable=False, hidden=True,
               requires_restart=True),
    # 留空 = 按 SIM 的 IMSI 自动识别（默认）。识别不到或携号转网导致识别有误时，
    # 填这里覆盖。它同时喂给 dial_guard 的误拨保护——识别不到时那道保护会失效，
    # 而跨运营商客服号是按普通通话计费的（#72）。
    # requires_restart：覆盖只在 refresh_sim_identity（连接/重连时）施加，改完不重启
    # 的话 dial_guard / 结果校验 / /api/meta 仍拿旧值，面板却显示已生效（Codex 评审 P1）。
    ConfigSpec("CARRIER_HOTLINE", "运营商免费客服号（留空=按 SIM 自动识别）", "str", "",
               requires_restart=True),
    ConfigSpec("OWNER_NAME", "机主姓名", "str", ""),
    # 机主手机号：AI 把通话口信转发给机主时的短信收件号码（send_sms 的 to 填
    # owner 时由系统解析成这个号码，模型不接触真实号码）。同时进发信白名单——
    # 这个号码即使从未与本机通信过也放行（WIL-116）。留空 = 无法代发给机主。
    ConfigSpec("OWNER_PHONE", "机主手机号（转告短信收件号码）", "str", ""),
    ConfigSpec("INBOUND_AI_ANSWER_MODE", "蓝牙手机 AI 接听策略", "select", "off",
               choices=("off", "allowlist", "unanswered")),
    ConfigSpec("INBOUND_AI_ALLOWLIST", "蓝牙 AI 接听白名单（仅本地 .env）", "str", "",
               editable=False, hidden=True, secret=True),
    ConfigSpec("INBOUND_AI_ANSWER_AFTER_SECONDS", "蓝牙来电无人接听等待（秒）", "int", "15"),
    ConfigSpec(
        "INBOUND_TAKEOVER_ENABLED",
        "来电转手机真人接管",
        "bool",
        "false",
        requires_restart=True,
    ),
    ConfigSpec("INBOUND_TAKEOVER_TIMEOUT_SECONDS", "来电真人接管等待时限（秒）", "float", "20"),
    # 机主确认环（WIL-120 二期 Path A）：ask_owner 工具等 Web 确认卡答复的
    # 窗口；超时=拒绝（fail-closed，机主没看到不等于同意）。
    ConfigSpec("OWNER_CONFIRM_TIMEOUT_SECONDS", "机主确认等待窗口（秒）", "int", "60"),
    # Path B「我来说」（WIL-120 四期）：三方并入时拨给机主的第二号码
    # （不能是本机 SIM 自己的号）。留空 = Path B 不可用，确认卡按钮保持置灰。
    ConfigSpec("OWNER_SECOND_PHONE", "机主第二号码（三方并入用）", "str", ""),
    # wait_for_sms 工具（WIL-120 三期）：等新短信（OTP/确认短信）的窗口。
    ConfigSpec("WAIT_SMS_TIMEOUT_SECONDS", "等新短信窗口（秒）", "int", "60"),
    ConfigSpec(
        "INBOUND_TAKEOVER_PREFERENCE",
        "来电真人接管偏好",
        "str",
        "",
        requires_restart=True,
    ),
    ConfigSpec(
        "INBOUND_TRIAGE_MODE",
        "来电智能分诊模式",
        "select",
        "off",
        choices=("off", "shadow", "enforce"),
        requires_restart=True,
    ),
    # 默认留空：让 prompts.agent_persona() 按 AGENT_LANGUAGE 回退到
    # 「AI 助理」/「AI assistant」，英文模式下不会硬塞中文人设。
    ConfigSpec("AGENT_PERSONA", "AI 人设称谓", "str", ""),
    # 语音风格描述（如"语速稍慢、亲切自然"）；并进 instructions，qwen/openai 两链路都生效。
    # 注：OpenAI 的 cedar/marin 可能不太吃风格指令（社区反馈，非官方结论）。
    ConfigSpec("VOICE_STYLE", "语音风格描述", "str", ""),
    # AI 通话语言：决定 AI 打/接电话说什么语言、通话摘要用什么语言写；
    # 与前端 UI 语言（localStorage）相互独立。改动需重启会话。
    ConfigSpec("AGENT_LANGUAGE", "AI 通话语言", "select", "zh",
               choices=("zh", "en"), requires_restart=True),
    # 默认留空 = 无预设事项（提示词走「无预设」优雅分支，不会硬塞元指令当主题）；
    # 外呼时通常在页面临时填具体主题。
    ConfigSpec("AGENT_OUTBOUND_TASK", "外呼任务指令", "str", ""),
    ConfigSpec("PROMPT_GEN_ENABLED", "动态场景提示词", "bool", "true"),
    ConfigSpec("PROMPT_GEN_MODEL", "动态场景提示词模型", "str", ""),
    ConfigSpec("PROMPT_GEN_TIMEOUT", "动态场景提示词超时（秒）", "float", "5.0",
               editable=False, hidden=True),
    ConfigSpec("PROMPT_GEN_WAIT_SECONDS", "动态场景提示词等待（秒）", "float", "3.0",
               editable=False, hidden=True),
    ConfigSpec("NUMBER_PROFILES_ENABLED", "预调教任务库", "bool", "true"),
    ConfigSpec("NUMBER_PROFILES_FILE", "预调教任务库文件", "str", ""),
    ConfigSpec("CALL_PLAYBOOKS_ENABLED", "呼叫情报库", "bool", "true"),
    ConfigSpec("CALL_PLAYBOOKS_FILE", "呼叫情报库文件", "str", ""),
    ConfigSpec("CALL_PLAYBOOKS_MIN_EVIDENCE_CALLS", "情报必填项最少独立通话证据", "int", "2"),
    # ---- 本地三段式（AGENT_PROVIDER=local）----
    ConfigSpec("LOCAL_LLM_PROVIDER", "三段式 LLM 提供方", "select", "dashscope",
               choices=("dashscope", "deepseek"), requires_restart=True),
    ConfigSpec("LOCAL_LLM_BASE_URL", "三段式 DeepSeek API 地址", "str", "https://api.deepseek.com",
               requires_restart=True),
    ConfigSpec("DEEPSEEK_API_KEY", "DeepSeek API Key", "str", "",
               secret=True, requires_restart=True),
    ConfigSpec("LOCAL_LLM_MODEL", "三段式 LLM 模型", "str", "qwen-plus",
               requires_restart=True),
    ConfigSpec("LOCAL_LLM_THINKING", "三段式 DeepSeek 思考模式", "select", "disabled",
               choices=("disabled", "low", "high"), requires_restart=True),
    ConfigSpec("LOCAL_LLM_TIMEOUT", "三段式 LLM 超时（秒）", "float", "20.0",
               editable=False, hidden=True),
    ConfigSpec("LOCAL_MODELS_DIR", "三段式模型目录", "str", "",
               editable=False, hidden=True, requires_restart=True),
    ConfigSpec("LOCAL_ASR_SEGMENTED", "三段式分段识别", "bool", "false", requires_restart=True),
    ConfigSpec("LOCAL_ASR_SEGMENT_SILENCE", "三段式识别切段静音（秒）", "float", "0.25",
               requires_restart=True),
    ConfigSpec("LOCAL_TURN_SILENCE", "三段式轮末物理静音（秒，含切段等待）", "float", "0.6",
               requires_restart=True),
    ConfigSpec("LOCAL_TTS_MODEL", "三段式 TTS 模型", "str", "vits-piper-zh_CN-chaowen-medium-int8",
               requires_restart=True),
    ConfigSpec("LOCAL_TTS_SPEAKER_ID", "三段式 TTS 说话人", "int", "0", requires_restart=True),
    ConfigSpec("LOCAL_TTS_STREAMING", "三段式分句流式合成", "bool", "false", requires_restart=True),
    ConfigSpec("LOCAL_TTS_CACHE_PHRASES", "三段式 TTS 缓存短句", "str", "", requires_restart=True),
    ConfigSpec("LOCAL_TTS_PEAK", "三段式 TTS 目标峰值（空为关闭）", "str", "", requires_restart=True),
    ConfigSpec("MANUAL_RESPONSE_CONTROL", "手动应答控制", "bool", "false"),
    # barge-in：对端开口即打断 AI（掐生成+清积压），代替半双工丢上行。开启前提：
    # 模组无下行→上行回采（SIM7600 CPCMREG 实测互相关 r=0.009，干净；Quectel UAC
    # 路线未验证，保持默认 false 走半双工）。
    ConfigSpec("BARGE_IN_ENABLED", "对端插话打断 AI", "bool", "false",
               requires_restart=True),
    ConfigSpec("MANUAL_RESPONSE_SILENCE_MS", "手动应答静默窗口（毫秒）", "int", "1000"),
    ConfigSpec("MANUAL_RESPONSE_MAX_WAIT_MS", "手动应答最长等待（毫秒）", "int", "8000"),
    # 文本判官本批仅实现旁观模式；enforce 不进 choices，配置写回会在边界拒绝。
    ConfigSpec("DTMF_JUDGE_MODE", "DTMF 文本判官", "select", "off",
               choices=("off", "shadow")),
    ConfigSpec("DTMF_JUDGE_MODEL", "DTMF 判官文本模型", "str", ""),
    # ---- 模组 ----
    # 模组厂商：quectel=EC20/EG25（UAC/QPCMV 音频）；simcom=SIM7600（CPCMREG
    # PCM-over-USB 音频，须配 MODEM_AUDIO_MODE=nmea 走串口 PCM 桥）。
    ConfigSpec("MODEM_VENDOR", "模组厂商", "select", platforms.default_modem_vendor(),
               choices=("auto", "quectel", "simcom", "bluetooth"), requires_restart=True),
    ConfigSpec("MODEM_SIGNAL_POLL_SECONDS", "信号轮询间隔（秒，0 关闭）", "int",
               str(platforms.default_signal_poll_seconds())),
    ConfigSpec("BLUETOOTH_PHONE_ADDRESS", "手机蓝牙地址", "str", "",
               help="仅 Linux 蓝牙手机线路；只填本地 .env，不上传手机地址。",
               secret=True, editable=False, hidden=True, requires_restart=True),
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
    ConfigSpec("MODEM_PCM_UPLINK_PREBUFFER", "PCM 开场上行预缓冲", "bool", "false"),
    ConfigSpec("MODEM_PCM_UPLINK_HOLD_MS", "SIM7600 先读后写实验等待毫秒", "int", "0"),
    ConfigSpec("MODEM_PCM_PIN_8K", "SIM7600 PCM 固定 8kHz", "bool",
               str(platforms.default_pcm_pin_8k()).lower(), editable=False, hidden=True),
    ConfigSpec("MODEM_RESET_AFTER_CALL", "SIM7600 通话后重启模组", "bool",
               str(platforms.default_reset_after_call()).lower(),
               editable=False, hidden=True, requires_restart=True),
    ConfigSpec("PARALLEL_AGENT_CONNECT", "AI 建连与模组准备并行", "bool",
               str(platforms.default_parallel_agent_connect()).lower(), editable=False, hidden=True),
    ConfigSpec("MODEM_ANSWER_GREETING", "SIM7600 接听问候（空=关闭）", "str",
               platforms.default_modem_answer_greeting(), editable=False, hidden=True, requires_restart=True),
    ConfigSpec("MODEM_ANSWER_GREETING_DELAY_MS", "接听后模组问候延迟（毫秒）", "int",
               str(platforms.default_modem_answer_greeting_delay_ms()), editable=False, hidden=True),
    ConfigSpec("MODEM_TTS_PARAMS", "SIM7600 TTS 参数（空=不设置）", "str",
               platforms.default_modem_tts_params(), editable=False, hidden=True, requires_restart=True),
    ConfigSpec("MODEM_TX_GAIN", "上行发送增益", "float", "1.0"),
    # 对方语音送 AI 模型前的独立增益；每通开始读取，录音/监听仍保留各自路径。
    ConfigSpec("AGENT_UPLINK_GAIN", "AI 输入增益（对方语音）", "float", "1.0"),
    # 模组语音送远程手机前的独立增益；每个 LiveKit 会话创建时读取，支持热更新。
    ConfigSpec("REMOTE_DOWNLINK_GAIN", "远程手机下行增益", "float", "16.0"),
    # ---- 通话行为 ----
    ConfigSpec("HALF_DUPLEX_HANGOVER_SECONDS", "半双工挂尾时长（秒）", "float", "0.5"),
    ConfigSpec("HANGUP_TOOL_DELAY_SECONDS", "挂断工具延迟（秒）", "float", "4.5"),
    # 默认维持 inband —— 2026-08-03 真机对照定档（#74 / WIL-82）：
    # 白天拨 10086 各 5 通，只统计「在按键菜单上按的键」，判据为对端是否真的
    # 推进到下一级（本机 result=success 已证实是假阳，不作数）。
    # 一级菜单「转回广东10086请按1」是两臂唯一完全可比的事件：
    #     inband 4/4 推进，qvts 5/5 推进 —— 无可测差异。
    # 更早认为「inband 被运营商 IVR 忽略」的结论，是在 DTMF_GUARD_MS 护窗
    # （#45）之前得到的；真因是语音与双音混叠，不是 mode。护窗上线后不再复现。
    # 差距不明显就不动生产默认值。
    ConfigSpec("DTMF_MODE", "DTMF 发送模式", "select", "inband",
               choices=("inband", "qvts", "both")),
    # 带内双音候选标定(#80-D):200ms/0.50 为候选基线（约 -6dBFS）;待 G2 真机验证。
    ConfigSpec("DTMF_TONE_MS", "带内按键音时长（毫秒）", "int", "200"),
    # 按键护窗（#45 信号层）：DTMF 前后这段时间丢弃 Agent 下行语音，让按键
    # 前后的上行只有双音。真机实测模型会「边说边按」，语音与双音混叠后对端
    # 两者都听不清。0 = 关闭护窗（回到旧行为）。
    ConfigSpec("DTMF_GUARD_MS", "按键前后静音护窗（毫秒）", "int", "400"),
    ConfigSpec("DTMF_TONE_AMPLITUDE", "带内按键音幅度 (0, 1]，默认 0.5（约 -6dBFS）", "float", "0.5"),
    # 单轮长度闸门（WIL-90 / WIL-85 N2）：一轮生成超过这么多秒就打断生成。
    #
    # 分工要说清：**提示词负责把中位数压下来，本闸门只兜住跑飞的长尾**。
    # 2026-08-05 WIL-89 基线（22 通）：单轮时长中位 6.8s、p90 17.8s、最长 36.6s。
    # 默认 20s 取在 p90 之上，正常轮次不会被碰到，只掐那条 36 秒的。
    #
    # 掐的是**生成**不是**播放**——模型把整轮音频以突发写入、远快于实时播放
    # （见 call_log._build_stereo_mix 注释），所以已排队的音频照常播完，
    # 对端听到的是一段完整播出的语音，只是不再继续变长。
    # 0 = 关闭。
    ConfigSpec("AGENT_MAX_TURN_SECONDS", "AI 单轮生成上限（秒）；0=不限", "float", "20"),
    # 媒体死亡看门狗（WIL-100）：上行仍在出帧、内容却精确为 0，累计这么久即判定
    # 媒体已死并落 dead_media_detected 事件。**检测总是开着，挂断另有开关。**
    #
    # 2026-08-06 真机：机主挂断后会话僵尸十分钟，Realtime 连接空转、录音直到手动
    # 挂断才落盘。NO CARRIER 被串口抖动吞掉（modem.py:225 同类事故），CLCC 又一直
    # 报着有活跃通话，两道既有保护都不适用——只剩媒体本身可判。
    ConfigSpec("DEAD_MEDIA_TIMEOUT_SECONDS", "上行纯静音累计多久判定媒体已死（秒）；0=关闭", "float", "60"),
    # 判定媒体已死后是否**真的挂断**。默认关：
    #
    # 精确为 0 的上行只能证明**媒体静默**，不等于对端挂断（2026-08-06 Codex 评审
    # P1）。对方按静音、通话被 hold、UAC 补零、采集链路可恢复故障，都会是同样的
    # 签名；而 GSM DTX 在持续丢帧后也允许静音而非补舒适噪声，所以「真实音频必有
    # 底噪」不是普适保证。误挂断活着的通话，比晚挂断严重得多。
    #
    # 所以先只观测：攒够真实数据（活跃通话里到底会不会出现 60 秒精确静音）再决定
    # 是否默认开启。与 WIL-83 同一处理方式——先量，再改行为。
    ConfigSpec("DEAD_MEDIA_HANGUP", "媒体判死后是否自动挂断（默认关，先观测）", "bool", "false"),
    ConfigSpec("REPEAT_SUPPRESS_SIMILARITY", "复读抑制相似度阈值", "float", "0.9"),
    # 外呼硬时限（秒）：LLM 收尾裁判失灵/漏判时的最后防线，到点自动道别挂断；
    # 0 = 不限制。（正常收尾由 summarizer.judge_wrap_up 提前判定。）
    # 长通话场景（客服排队）不改这里——用 number_profiles 的 max_call_seconds
    # 按场景覆盖（WIL-120 一期），全局保持安全默认。
    ConfigSpec("OUTBOUND_MAX_SECONDS", "外呼最长时长（秒）", "int", "150"),
    # 来电缺失 NO CARRIER 且 CLCC 轮询也停活时的会话级最后防线。
    ConfigSpec("INBOUND_MAX_SECONDS", "来电最长时长（秒）", "int", "1800"),
    ConfigSpec("WRAP_UP_JUDGE_GRACE_SECONDS", "收尾裁判首次等待（秒）", "float", "20.0",
               editable=False, hidden=True),
    ConfigSpec("WRAP_UP_JUDGE_INTERVAL_SECONDS", "收尾裁判间隔（秒）", "float", "15.0",
               editable=False, hidden=True),
    ConfigSpec("WRAP_UP_JUDGE_CARRIER_SMS", "收尾裁判参考本通官方短信", "bool", "false"),
    ConfigSpec("RECORDING_ENABLED", "通话录音开关", "bool", "false"),
    ConfigSpec("RECORDING_RETENTION_DAYS", "录音保留天数", "int", "30"),
    ConfigSpec("SUMMARY_ENABLED", "通话摘要开关", "bool", "true"),
    ConfigSpec("SUMMARY_MODEL", "摘要模型", "str", ""),
    # 对话建单模型（WIL-120 三期 b）：留空 = provider 强档默认（openai=gpt-4o）
    ConfigSpec("INTAKE_MODEL", "对话建单模型", "str", ""),
    # 摘要 API 超时秒数（真机实测 15s 对长转写不够用），调试项不进面板。
    ConfigSpec("SUMMARY_TIMEOUT", "摘要超时（秒）", "float", "30",
               editable=False, hidden=True),
    ConfigSpec("SMS_VERIFICATION_WAIT_SECONDS", "官方短信校验等待（秒）", "float", "30",
               editable=False, hidden=True),
    # ---- 本地监听 ----
    ConfigSpec(
        "MONITOR_AI_PLAYBACK", "本地监听 AI 语音", "bool", "false",
        requires_restart=True,
    ),
    # 默认留空 = 跟随系统默认输出设备（可移植；填设备名则按名匹配）。
    ConfigSpec(
        "MONITOR_OUTPUT_DEVICE", "监听输出设备", "str", "",
        requires_restart=True,
    ),
    ConfigSpec(
        "MONITOR_AI_GAIN", "监听增益（AI 下行）", "float", "1.0",
        requires_restart=True,
    ),
    # 电话上行是窄带信号且电平偏低，监听需大幅放大（真机调到 15 倍才够听清）。
    ConfigSpec(
        "MONITOR_UPLINK_GAIN", "监听增益（对方上行）", "float", "8.0",
        requires_restart=True,
    ),
    # ---- 白名单与节流 ----
    ConfigSpec("DIAL_WHITELIST", "外呼白名单", "str", ""),
    ConfigSpec("DIAL_INTERVAL_SECONDS", "连续拨号间隔（秒）", "float", "5.0"),
    ConfigSpec("SMS_RATE_LIMIT_PER_HOUR", "短信发送频控（每小时）", "int", "10"),
    # 收件短信进 app 后删 SIM 上那条，防 SIM 短信存储（常 ~20-50 条）满导致
    # 新短信收不进来。默认开；关掉则 SIM 会堆积，满后需手动清。
    ConfigSpec("SMS_DELETE_AFTER_INGEST", "短信入库后删 SIM 副本", "bool", "true"),
    ConfigSpec("SMS_EMAIL_FORWARD_ENABLED", "收到短信后转发到邮箱", "bool", "false"),
    ConfigSpec("SMS_EMAIL_RECIPIENT", "短信转发收件邮箱", "str", ""),
    ConfigSpec("SMS_EMAIL_SMTP_HOST", "发件 SMTP 主机", "str", ""),
    ConfigSpec("SMS_EMAIL_SMTP_PORT", "发件 SMTP 端口", "int", "587"),
    ConfigSpec(
        "SMS_EMAIL_SMTP_SECURITY",
        "发件 SMTP 加密方式",
        "select",
        "starttls",
        choices=("starttls", "ssl"),
    ),
    ConfigSpec("SMS_EMAIL_SMTP_USERNAME", "发件 SMTP 用户名", "str", ""),
    ConfigSpec(
        "SMS_EMAIL_SMTP_PASSWORD",
        "发件 SMTP 应用密码",
        "str",
        "",
        secret=True,
    ),
    ConfigSpec("SMS_EMAIL_FROM", "发件邮箱地址", "str", ""),
    ConfigSpec("TOOL_QUERY_CODE_ENABLED", "启用验证码查询工具", "bool", "true",
               requires_restart=True),
    # 发短信目标限制改为「只能回复已联系过的号码」(见 contacts.py),
    # 不再用静态白名单,故移除原 SMS_WHITELIST 配置项。
    # 开发期总开关:置 true 时放行给任意号码发短信(仅本机开发测试用,默认关)。
    ConfigSpec("SMS_ALLOW_ANY_TARGET", "短信允许发给任意号码(开发用)", "bool", "false"),
    # ---- 连接管理 ----
    ConfigSpec("QWEN_PREWARM", "Qwen 连接预热", "bool", "true"),
    ConfigSpec("QWEN_RECONNECT_MAX", "Qwen 最大重连次数", "int", "2"),
    ConfigSpec("OPENAI_RECONNECT_MAX", "OpenAI 最大重连次数", "int", "2"),
    # ---- 远程网页拨号 POC ----
    ConfigSpec("REMOTE_WEB_DIALER_ENABLED", "启用远程网页拨号", "bool", "false",
               requires_restart=True),
    ConfigSpec("REMOTE_CLOUD_ENABLED", "使用 CallPilot 云控制面", "bool", "false",
               requires_restart=True),
    ConfigSpec("REMOTE_CONTENT_READ_ENABLED", "允许已授权手机读取短信与通话记录",
               "bool", "false", requires_restart=True),
    ConfigSpec("REMOTE_CLOUD_URL", "CallPilot 云控制面地址", "str",
               "https://api-beta.bondings.ai", requires_restart=True),
    ConfigSpec("REMOTE_CLOUD_DIALER_URL", "CallPilot 手机拨号地址", "str",
               "https://dial-beta.bondings.ai/", editable=False, hidden=True),
    ConfigSpec("REMOTE_MEDIA_PROVIDER", "远程媒体服务", "select", "livekit",
               choices=("livekit",)),
    # EC20/EG25 真机验证：UAC 路径只注入带内双音时，运营商 IVR 可能不识别；
    # 远程真人拨号默认走模组 QVTS，避免继承 AI 通话的带内默认值。
    ConfigSpec("REMOTE_DTMF_MODE", "远程拨号 DTMF 模式", "select", "qvts",
               choices=("qvts", "both", "inband")),
    ConfigSpec("REMOTE_HUMAN_RECORDING_ENABLED", "远程真人通话录音", "bool", "false"),
    ConfigSpec("REMOTE_CONTROL_URL", "远程拨号页 HTTPS 地址", "str", ""),
    ConfigSpec("LIVEKIT_URL", "LiveKit WebSocket 地址", "str", ""),
    ConfigSpec("LIVEKIT_API_KEY", "LiveKit API Key", "str", "", secret=True),
    ConfigSpec("LIVEKIT_API_SECRET", "LiveKit API Secret", "str", "", secret=True),
    ConfigSpec("REMOTE_DISCONNECT_GRACE_SECONDS", "远程断线宽限（秒）", "float", "5"),
    ConfigSpec("REMOTE_OUTBOUND_MAX_SECONDS", "远程外呼最长时长（秒）", "int", "1800"),
    ConfigSpec("REMOTE_DIAL_LIMIT_PER_HOUR", "远程外呼频控（每小时）", "int", "10"),
    ConfigSpec("REMOTE_GATEWAY_PORT", "远程拨号隧道本机端口", "int", "47445",
               requires_restart=True),
    ConfigSpec("REMOTE_MAX_PAIRED_DEVICES", "远程配对设备上限", "int", "5",
               editable=False, hidden=True),
    ConfigSpec("REMOTE_PAIRING_TTL_SECONDS", "远程配对码有效期（秒）", "int", "300",
               editable=False, hidden=True),
    ConfigSpec("REMOTE_INVITE_TTL_SECONDS", "远程拨号邀请有效期（秒）", "int", "300",
               editable=False, hidden=True),
    ConfigSpec("REMOTE_CONNECT_TIMEOUT_SECONDS", "远程外呼接通超时（秒）", "float", "45",
               editable=False, hidden=True),
    ConfigSpec("SETUP_DONE", "首次启动向导完成", "bool", "false",
               editable=False, hidden=True),
    # 预热调优项：单次握手超时与保活周期（秒），一般无需改动，不进面板。
    ConfigSpec("QWEN_PREWARM_TIMEOUT", "Qwen 预热握手超时（秒）", "float", "5.0",
               editable=False, hidden=True),
    ConfigSpec("QWEN_PREWARM_INTERVAL", "Qwen 预热保活间隔（秒）", "float", "240.0",
               editable=False, hidden=True),
    # ---- Web 服务 ----
    ConfigSpec("WEB_OPEN_BROWSER", "启动时打开浏览器", "bool", "true",
               editable=False, hidden=True, requires_restart=True),
    ConfigSpec("WEB_HOST", "Web 监听地址", "str", "127.0.0.1",
               editable=False, requires_restart=True),
    # 默认用不常见的高位端口，规避 8000/8080 这类常见端口的占用冲突；
    # 桌面 App 会自动打开该地址，CLI 用户从启动日志获取，无需记忆。
    ConfigSpec("WEB_PORT", "Web 监听端口", "int", "47100",
               editable=False, requires_restart=True),
    # 非 loopback 监听（局域网/公网暴露）时必填：Web API 能拨号/发短信，
    # 裸监听等于把电话交给整个网段。loopback（默认）下忽略本项、行为不变。
    ConfigSpec("WEB_AUTH_TOKEN", "Web 访问令牌（非本机监听必填）", "str", "",
               secret=True, editable=False, hidden=True, requires_restart=True),
)


def is_loopback_host(host: str) -> bool:
    """WEB_HOST 是否只在本机回环上监听（此时无需 Web 访问令牌）。"""
    return host.strip().lower() in {"127.0.0.1", "::1", "localhost"}

_SPECS_BY_KEY: dict[str, ConfigSpec] = {spec.key: spec for spec in CONFIG_SPECS}

# Agent 提供方 -> 必需的环境变量（凭证不进注册表，避免出现在面板）。
PROVIDER_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "qwen": ("DASHSCOPE_API_KEY",),
    "doubao": ("DOUBAO_APP_ID", "DOUBAO_ACCESS_KEY"),
    "openai": ("OPENAI_API_KEY",),
    # 三段式的默认 LLM 脑是 dashscope 文本模型（qwen-plus），复用同一凭证。
    "local": ("DASHSCOPE_API_KEY",),
}


def get_spec(key: str) -> ConfigSpec:
    """按 key 取注册信息；未注册抛 ``KeyError``。"""
    try:
        return _SPECS_BY_KEY[key]
    except KeyError:
        raise KeyError(f"未注册的配置项: {key}") from None


# ---- 类型化读取（读 os.environ，缺省回落注册表默认值）----


def is_explicit(key: str) -> bool:
    """.env 已由入口加载到环境；键存在即显式设置，空值也不被预设覆盖。"""
    return key in os.environ


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
    if provider == "local":
        llm_provider = get_str("LOCAL_LLM_PROVIDER")
        if llm_provider not in {"dashscope", "deepseek"}:
            return [f"未知的 LOCAL_LLM_PROVIDER: {llm_provider}"]
        required = ("DEEPSEEK_API_KEY",) if llm_provider == "deepseek" else ("DASHSCOPE_API_KEY",)
    if required is None:
        known = ", ".join(sorted(PROVIDER_REQUIRED_KEYS))
        return [f"未知的 Agent 提供方: {provider}（可选: {known}）"]
    errors: list[str] = []
    for key in required:
        if not os.environ.get(key, "").strip():
            errors.append(f"缺少环境变量 {key}（{provider} 必需）")
    text_key = "OPENAI_API_KEY" if provider == "openai" else "DASHSCOPE_API_KEY"
    if provider == "local":
        text_key = required[0]
    if (
        get_str("DTMF_JUDGE_MODE") == "shadow"
        and not os.environ.get(text_key, "").strip()
        and text_key not in required
    ):
        errors.append(f"缺少环境变量 {text_key}（DTMF 文本判官 shadow 必需）")
    return errors


def credential_status(provider: str | None = None) -> dict:
    """Return current provider credential readiness for UI/API status."""
    selected = provider or get_str("AGENT_PROVIDER")
    errors = validate_provider_credentials(selected)
    return {"provider": selected, "ok": not errors, "errors": errors}


def any_provider_credentials_ready() -> bool:
    """Return whether any supported provider has all required credentials."""
    return any(not validate_provider_credentials(provider) for provider in PROVIDER_REQUIRED_KEYS)


def setup_required() -> bool:
    """Require the first-run wizard until its explicit consent step is complete."""
    return not get_bool("SETUP_DONE")


def runtime_meta(provider: str, model: str, port: str) -> dict:
    """Build /api/meta payload with non-fatal configuration readiness."""
    if provider == "local":
        model = (
            f"本地 · {get_str('LOCAL_LLM_MODEL')} + "
            f"{get_str('LOCAL_TTS_MODEL')}#{get_int('LOCAL_TTS_SPEAKER_ID')}"
        )
    return {
        "provider": provider,
        "model": model,
        "port": port,
        "credentials": credential_status(provider),
        "setup_required": setup_required(),
    }


def _http_request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        return int(status), resp.read()


def validate_provider_key_online(
    provider: str,
    secret: str,
    *,
    timeout: float = 5.0,
) -> KeyValidationResult:
    """Lightweight online provider credential check without persisting the secret."""
    provider = provider.strip().lower()
    secret = secret.strip()
    if not secret:
        return KeyValidationResult(False, "invalid", "empty key")
    try:
        if provider == "openai":
            _http_request_json(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=timeout,
            )
            return KeyValidationResult(True, "valid")
        if provider == "qwen":
            payload = (
                b'{"model":"qwen-turbo","input":{"messages":['
                b'{"role":"user","content":"ping"}]},"parameters":{"max_tokens":1}}'
            )
            _http_request_json(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                method="POST",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                body=payload,
                timeout=timeout,
            )
            return KeyValidationResult(True, "valid")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return KeyValidationResult(False, "invalid", f"HTTP {exc.code}")
        return KeyValidationResult(False, "network", f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return KeyValidationResult(False, "network", str(exc))
    return KeyValidationResult(False, "unsupported", provider)


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
            "choice_labels": dict(spec.choice_labels or {}),
            "help": spec.help,
            "editable": spec.editable,
            "secret": spec.secret,
            "requires_restart": spec.requires_restart,
            "value": value,
            "configured": spec.key in os.environ,
        })
    return rows


# ---- .env 写回 ----


# 数值型配置的写回取值范围：(下界, 上界, 下界是否排他)。防止设置面板/API 把
# 0、超界值写进 .env——运行时才炸(如 dtmf_tone 对 amplitude∉(0,1] 抛
# ValueError,会导致按键失败；在写入边界提前拒绝(#80-D review)。
_NUMERIC_RANGES: dict[str, tuple[float, float, bool]] = {
    "DTMF_TONE_MS": (0, 2000, True),          # >0 且 ≤2000ms
    "DTMF_TONE_AMPLITUDE": (0.0, 1.0, True),  # (0, 1]
    "INBOUND_MAX_SECONDS": (0, 86400, True),   # >0 且 ≤24h
    "INBOUND_AI_ANSWER_AFTER_SECONDS": (0, 600, True),  # Bluetooth unanswered policy
    # 0=不限时合法（number_profiles 的 max_call_seconds 可按场景覆盖，WIL-120）。
    "OUTBOUND_MAX_SECONDS": (0, 86400, False),  # ≥0 且 ≤24h
    "OWNER_CONFIRM_TIMEOUT_SECONDS": (0, 600, True),  # >0 且 ≤10min
    "WAIT_SMS_TIMEOUT_SECONDS": (0, 600, True),       # >0 且 ≤10min
}


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
    if spec.kind in {"int", "float"}:
        bounds = _NUMERIC_RANGES.get(spec.key)
        if bounds is not None:
            low, high, low_exclusive = bounds
            number = float(value.strip())
            below = number <= low if low_exclusive else number < low
            if below or number > high or number != number:  # NaN != NaN
                low_op = ">" if low_exclusive else "≥"
                raise ValueError(
                    f"配置 {spec.key} 需满足 {low_op}{low:g} 且 ≤{high:g}，收到 {value!r}"
                )


_SMS_EMAIL_CONFIG_KEYS = {
    "SMS_EMAIL_FORWARD_ENABLED",
    "SMS_EMAIL_RECIPIENT",
    "SMS_EMAIL_SMTP_HOST",
    "SMS_EMAIL_SMTP_PORT",
    "SMS_EMAIL_SMTP_SECURITY",
    "SMS_EMAIL_SMTP_USERNAME",
    "SMS_EMAIL_SMTP_PASSWORD",
    "SMS_EMAIL_FROM",
}


def _validate_sms_email_updates(updates: dict[str, str]) -> None:
    """写入前校验短信转发配置合并后的完整状态。"""
    if not _SMS_EMAIL_CONFIG_KEYS.intersection(updates):
        return

    def merged(key: str) -> str:
        return updates.get(key, os.environ.get(key, get_spec(key).default)).strip()

    enabled = merged("SMS_EMAIL_FORWARD_ENABLED").lower() in _TRUTHY
    recipient = merged("SMS_EMAIL_RECIPIENT")
    from_address = merged("SMS_EMAIL_FROM")
    smtp_host = merged("SMS_EMAIL_SMTP_HOST")
    smtp_security = merged("SMS_EMAIL_SMTP_SECURITY")

    # 延迟导入，避免配置注册表反向依赖转发模块的初始化过程。
    from .sms_email_forwarder import is_valid_email_address

    if (enabled or "SMS_EMAIL_RECIPIENT" in updates) and recipient:
        if not is_valid_email_address(recipient):
            raise ValueError("配置 SMS_EMAIL_RECIPIENT 需要单个合法邮箱地址")
    if (enabled or "SMS_EMAIL_FROM" in updates) and from_address:
        if not is_valid_email_address(from_address):
            raise ValueError("配置 SMS_EMAIL_FROM 需要单个合法邮箱地址")
    if enabled or "SMS_EMAIL_SMTP_HOST" in updates:
        if smtp_host and any(char.isspace() or ord(char) < 33 for char in smtp_host):
            raise ValueError("配置 SMS_EMAIL_SMTP_HOST 不允许包含空白或控制字符")
    if enabled or "SMS_EMAIL_SMTP_SECURITY" in updates:
        if smtp_security not in get_spec("SMS_EMAIL_SMTP_SECURITY").choices:
            raise ValueError("配置 SMS_EMAIL_SMTP_SECURITY 只允许 starttls 或 ssl")
    if enabled or "SMS_EMAIL_SMTP_PORT" in updates:
        try:
            smtp_port = int(merged("SMS_EMAIL_SMTP_PORT"))
        except ValueError:
            raise ValueError("配置 SMS_EMAIL_SMTP_PORT 需要整数") from None
        if not 1 <= smtp_port <= 65535:
            raise ValueError("配置 SMS_EMAIL_SMTP_PORT 必须在 1..65535")

    if not enabled:
        return
    required = {
        "SMS_EMAIL_RECIPIENT": recipient,
        "SMS_EMAIL_SMTP_HOST": smtp_host,
        "SMS_EMAIL_SMTP_USERNAME": merged("SMS_EMAIL_SMTP_USERNAME"),
        "SMS_EMAIL_SMTP_PASSWORD": merged("SMS_EMAIL_SMTP_PASSWORD"),
        "SMS_EMAIL_FROM": from_address,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError("启用短信邮件转发前请完整配置: " + ", ".join(missing))


_REMOTE_DIALER_CONFIG_KEYS = {
    "REMOTE_WEB_DIALER_ENABLED",
    "REMOTE_CLOUD_ENABLED",
    "REMOTE_CLOUD_URL",
    "REMOTE_MEDIA_PROVIDER",
    "REMOTE_CONTROL_URL",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "REMOTE_DISCONNECT_GRACE_SECONDS",
    "REMOTE_OUTBOUND_MAX_SECONDS",
    "REMOTE_DIAL_LIMIT_PER_HOUR",
    "REMOTE_GATEWAY_PORT",
}


def _validate_remote_dialer_updates(updates: dict[str, str]) -> None:
    if not _REMOTE_DIALER_CONFIG_KEYS.intersection(updates):
        return

    def merged(key: str) -> str:
        return updates.get(key, os.environ.get(key, get_spec(key).default)).strip()

    enabled = merged("REMOTE_WEB_DIALER_ENABLED").lower() in _TRUTHY
    if not enabled:
        return
    cloud_enabled = merged("REMOTE_CLOUD_ENABLED").lower() in _TRUTHY
    required = (
        ("REMOTE_CLOUD_URL",)
        if cloud_enabled
        else (
            "REMOTE_CONTROL_URL",
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
        )
    )
    missing = [key for key in required if not merged(key)]
    if missing:
        raise ValueError("启用远程网页拨号前请完整配置: " + ", ".join(missing))

    if cloud_enabled:
        cloud_url = urllib.parse.urlparse(merged("REMOTE_CLOUD_URL"))
        if (
            cloud_url.scheme != "https"
            or not cloud_url.netloc
            or cloud_url.username
            or cloud_url.password
            or cloud_url.query
            or cloud_url.fragment
        ):
            raise ValueError("配置 REMOTE_CLOUD_URL 必须是无内嵌凭证的 HTTPS URL")
    else:
        public_url = urllib.parse.urlparse(merged("REMOTE_CONTROL_URL"))
        if (
            public_url.scheme != "https"
            or not public_url.netloc
            or public_url.username
            or public_url.password
            or public_url.fragment
        ):
            raise ValueError("配置 REMOTE_CONTROL_URL 必须是无内嵌凭证的 HTTPS URL")
        livekit_url = urllib.parse.urlparse(merged("LIVEKIT_URL"))
        if (
            livekit_url.scheme != "wss"
            or not livekit_url.netloc
            or livekit_url.username
            or livekit_url.password
            or livekit_url.fragment
        ):
            raise ValueError("配置 LIVEKIT_URL 必须是无内嵌凭证的 WSS URL")

    if float(merged("REMOTE_DISCONNECT_GRACE_SECONDS")) < 0:
        raise ValueError("配置 REMOTE_DISCONNECT_GRACE_SECONDS 不能小于 0")
    if int(merged("REMOTE_OUTBOUND_MAX_SECONDS")) <= 0:
        raise ValueError("配置 REMOTE_OUTBOUND_MAX_SECONDS 必须大于 0")
    if int(merged("REMOTE_DIAL_LIMIT_PER_HOUR")) < 0:
        raise ValueError("配置 REMOTE_DIAL_LIMIT_PER_HOUR 不能小于 0")
    gateway_port = int(merged("REMOTE_GATEWAY_PORT"))
    if gateway_port < 1 or gateway_port > 65535:
        raise ValueError("配置 REMOTE_GATEWAY_PORT 必须在 1-65535 之间")


def _format_assignment(key: str, value: str) -> str:
    """生成 ``KEY=value`` 赋值文本；值含空白/#/引号时加双引号并转义。"""
    if value == "" or not _NEEDS_QUOTING_RE.search(value):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def update_env_file(
    updates: dict[str, str],
    env_path: str | Path | None = None,
    *,
    allow_hidden: bool = False,
) -> list[str]:
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
        if not spec.editable and not (allow_hidden and spec.hidden):
            raise ValueError(f"配置 {key} 不允许在面板编辑")
        _check_value(spec, value)
    _validate_sms_email_updates(updates)
    _validate_remote_dialer_updates(updates)

    if not updates:
        return []

    path = Path(env_path) if env_path is not None else env_file_path()
    with _ENV_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raw = path.read_bytes()
            has_bom = raw.startswith(codecs.BOM_UTF8)
            text = raw.decode("utf-8-sig")
            newline = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines()
        else:
            has_bom = False
            newline = "\n"
            lines = []

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

        rendered = newline.join(lines) + newline
        if has_bom:
            rendered = "\ufeff" + rendered
        path.write_text(rendered, encoding="utf-8", newline="")
        for key, value in updates.items():
            os.environ[key] = value
    logger.info("已更新 %s: %s", path, ", ".join(updates))
    return list(updates)


def complete_setup(
    recording_enabled: bool,
    env_path: str | Path | None = None,
) -> list[str]:
    """Atomically persist recording consent and first-run completion."""
    if not isinstance(recording_enabled, bool):
        raise ValueError("recording_enabled 必须是布尔值")
    return update_env_file(
        {
            "RECORDING_ENABLED": "true" if recording_enabled else "false",
            "SETUP_DONE": "true",
        },
        env_path=env_path,
        allow_hidden=True,
    )


__all__ = [
    "APP_NAME",
    "CONFIG_SPECS",
    "ConfigSpec",
    "KeyValidationResult",
    "PROVIDER_REQUIRED_KEYS",
    "any_provider_credentials_ready",
    "app_support_dir",
    "call_log_dir",
    "complete_setup",
    "credential_status",
    "data_dir",
    "env_file_path",
    "get_bool",
    "get_float",
    "get_int",
    "get_spec",
    "get_str",
    "log_dir",
    "read_panel_values",
    "runtime_meta",
    "setup_required",
    "update_env_file",
    "validate_provider_credentials",
    "validate_provider_key_online",
]
