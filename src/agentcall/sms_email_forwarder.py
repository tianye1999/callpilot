"""通过 SMTP 非阻塞转发模组实时收到的新短信。"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from queue import Empty, Full, Queue
from typing import Any, Literal

from . import config

logger = logging.getLogger(__name__)

SmtpSecurity = Literal["starttls", "ssl"]

_EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_EMAIL_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

_CHINESE_OTP_KEYWORDS = (
    "验证码",
    "校验码",
    "动态密码",
    "动态码",
    "动态验证码",
    "识别码",
    "认证码",
    "短信密码",
    "登录码",
    "口令",
    "验证代码",
)
_ENGLISH_OTP_PATTERNS = (
    r"\bverification\s+code\b",
    r"\bverify\s+code\b",
    r"\bone[-\s]?time\s+(?:password|passcode)\b",
    r"\botp\b",
    r"\bpasscode\b",
    r"\bsecurity\s+code\b",
    r"\bauth\s+code\b",
    r"\blogin\s+code\b",
    r"\baccess\s+code\b",
    r"\bconfirmation\s+code\b",
    r"\byour\s+code\s+is\b",
    r"\bpin\b",
)
_OTP_KEYWORD_RE = re.compile(
    "|".join(f"(?:{pattern})" for pattern in _ENGLISH_OTP_PATTERNS),
    re.IGNORECASE,
)
_NUMERIC_OTP_RE = re.compile(r"(?<![0-9A-Za-z])([0-9]{4,8})(?![0-9A-Za-z])")
_LETTER_HYPHEN_OTP_RE = re.compile(r"(?<![0-9A-Za-z])([A-Z])-([0-9]{4,8})(?![0-9A-Za-z])")
_ALNUM_OTP_RE = re.compile(r"(?<![0-9A-Za-z])([A-Z0-9]{4,8})(?![0-9A-Za-z])")
_UNIT_SUFFIXES = tuple(
    sorted(
        (
            "小时",
            "分钟",
            "公里",
            "GB",
            "MB",
            "KB",
            "TB",
            "km",
            "kg",
            "ml",
            "分",
            "秒",
            "天",
            "日",
            "个",
            "次",
            "元",
            "块",
            "角",
            "G",
            "M",
            "K",
            "%",
            "折",
            "年",
            "月",
            "号",
            "周",
            "岁",
            "人",
            "条",
            "件",
            "米",
            "g",
            "L",
        ),
        key=len,
        reverse=True,
    )
)


@dataclass(frozen=True)
class _KeywordHit:
    start: int
    end: int


@dataclass(frozen=True)
class _OtpCandidate:
    value: str
    start: int
    end: int
    kind_priority: int


@dataclass(frozen=True)
class SmsEmailSettings:
    recipient: str
    smtp_host: str
    smtp_port: int
    smtp_security: SmtpSecurity
    smtp_username: str
    smtp_password: str = field(repr=False)
    from_address: str
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not is_valid_email_address(self.recipient):
            raise ValueError("SMS_EMAIL_RECIPIENT 不是合法的单个邮箱地址")
        if not self.smtp_host or any(ord(char) < 33 for char in self.smtp_host):
            raise ValueError("SMS_EMAIL_SMTP_HOST 不能为空或包含空白/控制字符")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMS_EMAIL_SMTP_PORT 必须在 1..65535")
        if self.smtp_security not in ("starttls", "ssl"):
            raise ValueError("SMS_EMAIL_SMTP_SECURITY 只允许 starttls 或 ssl")
        if not self.smtp_username.strip():
            raise ValueError("SMS_EMAIL_SMTP_USERNAME 不能为空")
        if not self.smtp_password:
            raise ValueError("SMS_EMAIL_SMTP_PASSWORD 不能为空")
        if not is_valid_email_address(self.from_address):
            raise ValueError("SMS_EMAIL_FROM 不是合法的单个邮箱地址")
        if self.timeout_seconds <= 0:
            raise ValueError("SMTP timeout 必须大于 0")


@dataclass(frozen=True)
class ForwardSms:
    sender: str | None
    body: str
    received_at: datetime


@dataclass(frozen=True)
class _ForwardJob:
    settings: SmsEmailSettings
    sms: ForwardSms


def is_valid_email_address(value: str) -> bool:
    """保守校验单个 ASCII 邮箱地址，不接受显示名或地址列表。"""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value or len(value) > 254 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        return False
    if value.count("@") != 1:
        return False
    local, domain = value.rsplit("@", 1)
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    if _EMAIL_LOCAL_RE.fullmatch(local) is None:
        return False
    labels = domain.split(".")
    return len(labels) >= 2 and all(_EMAIL_DOMAIN_LABEL_RE.fullmatch(label) for label in labels)


def extract_otp(body: str) -> str | None:
    """仅在验证码强提示词附近提取 4 至 8 位候选值。"""
    text = str(body or "")
    if not text:
        return None
    keywords = _find_otp_keywords(text)
    candidates = _find_otp_candidates(text)
    if not keywords or not candidates:
        return None

    after_matches: list[tuple[int, int, int, str]] = []
    before_matches: list[tuple[int, int, int, str]] = []
    for keyword in keywords:
        for candidate in candidates:
            if (
                keyword.end <= candidate.start
                and candidate.end <= min(len(text), keyword.end + 20)
                and _is_valid_after_keyword_bridge(text[keyword.end : candidate.start])
            ):
                distance = candidate.start - keyword.end
                if not _candidate_is_noise(text, candidate, distance):
                    after_matches.append(
                        (distance, candidate.kind_priority, candidate.start, candidate.value)
                    )
            elif (
                max(0, keyword.start - 24) <= candidate.start
                and candidate.end <= keyword.start
                and _is_valid_before_keyword_bridge(text[candidate.end : keyword.start])
            ):
                distance = keyword.start - candidate.end
                if not _candidate_is_noise(text, candidate, distance):
                    before_matches.append(
                        (distance, candidate.kind_priority, candidate.start, candidate.value)
                    )

    if after_matches:
        return min(after_matches)[3]
    if before_matches:
        return min(before_matches)[3]
    return None


def _is_valid_after_keyword_bridge(value: str) -> bool:
    """验证码提示词与候选值之间只允许常见连接词和标点。"""
    bridge = value.strip(" \t\r\n:：,，-_")
    return bridge.lower() in ("", "is") or bridge in ("为", "是")


def _is_valid_before_keyword_bridge(value: str) -> bool:
    """候选值在提示词前时，仅接受英文短信常见的强绑定短语。"""
    if _crosses_sentence_boundary(value):
        return False
    bridge = " ".join(value.strip(" \t\r\n:：,，-_").lower().split())
    return bridge in ("is your", "as your account")


def _crosses_sentence_boundary(value: str) -> bool:
    return any(
        mark in value for mark in (".", "!", "?", ";", "。", "！", "？", "；", "\n", "\r")
    )


def _find_otp_keywords(text: str) -> list[_KeywordHit]:
    hits: list[_KeywordHit] = []
    for keyword in _CHINESE_OTP_KEYWORDS:
        start = text.find(keyword)
        while start != -1:
            hits.append(_KeywordHit(start=start, end=start + len(keyword)))
            start = text.find(keyword, start + 1)
    hits.extend(
        _KeywordHit(start=match.start(), end=match.end())
        for match in _OTP_KEYWORD_RE.finditer(text)
    )
    return sorted(hits, key=lambda hit: (hit.start, hit.end))


def _find_otp_candidates(text: str) -> list[_OtpCandidate]:
    candidates = [
        _OtpCandidate(match.group(1), match.start(1), match.end(1), 0)
        for match in _NUMERIC_OTP_RE.finditer(text)
    ]
    candidates.extend(
        _OtpCandidate(match.group(2), match.start(2), match.end(2), 1)
        for match in _LETTER_HYPHEN_OTP_RE.finditer(text)
    )
    for match in _ALNUM_OTP_RE.finditer(text):
        value = match.group(1)
        if value.isdigit() or value.isalpha() or not any(char.isdigit() for char in value):
            continue
        candidates.append(_OtpCandidate(value, match.start(1), match.end(1), 2))
    return candidates


def _candidate_is_noise(text: str, candidate: _OtpCandidate, keyword_distance: int) -> bool:
    suffix = text[candidate.end :].lstrip().lower()
    if candidate.value.isdigit() and len(candidate.value) == 4:
        if 1990 <= int(candidate.value) <= 2099:
            if suffix.startswith(("年", "-", "/")):
                return True
            return keyword_distance > 3
    if keyword_distance > 3 and any(
        suffix.startswith(unit.lower()) for unit in _UNIT_SUFFIXES
    ):
        return True
    return False


def _safe_sender_label(sender: str | None) -> str:
    if not sender:
        return "未知发件人"
    sanitized = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in sender)
    sanitized = " ".join(sanitized.split()).strip()
    return sanitized[:120] or "未知发件人"


def build_email_message(sms: ForwardSms, settings: SmsEmailSettings) -> EmailMessage:
    sender_label = _safe_sender_label(sms.sender)
    base_subject = f"[CallPilot 短信转发] 来自 {sender_label}"
    otp = extract_otp(sms.body)
    subject = f"【验证码 {otp}】{base_subject}" if otp else base_subject
    received_at = sms.received_at
    if received_at.tzinfo is None:
        received_at = received_at.astimezone()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = Address(display_name="CallPilot", addr_spec=settings.from_address)
    message["To"] = settings.recipient
    message.set_content(
        "\n".join(
            (
                f"发件号码: {sender_label if sms.sender else '未知'}",
                f"接收时间: {received_at.strftime('%Y-%m-%d %H:%M:%S %z')}",
                "",
                "短信内容:",
                sms.body,
                "",
            )
        ),
        charset="utf-8",
    )
    return message


class SmtpEmailSender:
    """仅允许 TLS 的 SMTP 发送器；工厂可注入以便确定性测试。"""

    def __init__(
        self,
        *,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., Any] = smtplib.SMTP_SSL,
        context_factory: Callable[[], Any] = ssl.create_default_context,
    ) -> None:
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory
        self._context_factory = context_factory

    def send(self, settings: SmsEmailSettings, message: EmailMessage) -> None:
        context = self._context_factory()
        if settings.smtp_security == "starttls":
            with self._smtp_factory(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.timeout_seconds,
            ) as client:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
                client.login(settings.smtp_username, settings.smtp_password)
                client.send_message(message)
            return
        if settings.smtp_security == "ssl":
            with self._smtp_ssl_factory(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.timeout_seconds,
                context=context,
            ) as client:
                client.login(settings.smtp_username, settings.smtp_password)
                client.send_message(message)
            return
        raise ValueError("SMTP security must be starttls or ssl")


def load_sms_email_settings() -> SmsEmailSettings | None:
    """读取实时配置；功能关闭或配置无效时安全返回 ``None``。"""
    if not config.get_bool("SMS_EMAIL_FORWARD_ENABLED"):
        return None
    try:
        return SmsEmailSettings(
            recipient=config.get_str("SMS_EMAIL_RECIPIENT").strip(),
            smtp_host=config.get_str("SMS_EMAIL_SMTP_HOST").strip(),
            smtp_port=int(config.get_str("SMS_EMAIL_SMTP_PORT").strip()),
            smtp_security=config.get_str("SMS_EMAIL_SMTP_SECURITY").strip().lower(),  # type: ignore[arg-type]
            smtp_username=config.get_str("SMS_EMAIL_SMTP_USERNAME").strip(),
            smtp_password=config.get_str("SMS_EMAIL_SMTP_PASSWORD"),
            from_address=config.get_str("SMS_EMAIL_FROM").strip(),
        )
    except (KeyError, ValueError):
        logger.warning("短信邮件转发配置不完整或无效，已跳过本次转发")
        return None


class SmsEmailForwarder:
    """有界延迟启动 worker；模组回调只读取配置快照并入队。"""

    def __init__(
        self,
        *,
        settings_loader: Callable[[], SmsEmailSettings | None] = load_sms_email_settings,
        build_message: Callable[[ForwardSms, SmsEmailSettings], EmailMessage] = build_email_message,
        send_email: Callable[[SmsEmailSettings, EmailMessage], None] | None = None,
        queue_size: int = 100,
        max_attempts: int = 3,
        retry_base_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._settings_loader = settings_loader
        self._build_message = build_message
        self._smtp_sender = SmtpEmailSender()
        self._send_email = send_email or self._smtp_sender.send
        self._queue: Queue[_ForwardJob] = Queue(maxsize=queue_size)
        self._max_attempts = max_attempts
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def enqueue(
        self,
        sender: str | None,
        body: str,
        *,
        received_at: datetime | None = None,
    ) -> bool:
        try:
            settings = self._settings_loader()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取短信邮件转发配置失败: error_type=%s", type(exc).__name__)
            return False
        if settings is None:
            return False
        job = _ForwardJob(settings, ForwardSms(sender, str(body or ""), received_at or self._clock()))

        with self._lock:
            if self._stopped:
                return False
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._worker,
                    name="sms-email-forwarder",
                    daemon=True,
                )
                self._thread.start()
            try:
                self._queue.put_nowait(job)
            except Full:
                logger.warning("短信邮件转发队列已满，已丢弃一条待转发消息")
                return False
        return True

    def stop(self, timeout: float = 16.0) -> None:
        with self._lock:
            self._stopped = True
            self._stop_event.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                try:
                    self._deliver(job)
                except Exception as exc:  # noqa: BLE001 - malformed input must not kill the worker.
                    logger.warning(
                        "构造短信转发邮件失败，已跳过: error_type=%s",
                        type(exc).__name__,
                    )
            finally:
                self._queue.task_done()

    def _deliver(self, job: _ForwardJob) -> None:
        message = self._build_message(job.sms, job.settings)
        for attempt in range(1, self._max_attempts + 1):
            if self._stop_event.is_set():
                return
            try:
                self._send_email(job.settings, message)
                logger.info("短信邮件转发成功")
                return
            except Exception as exc:  # noqa: BLE001 - SMTP failures must not kill the service.
                error_type = type(exc).__name__
                if attempt >= self._max_attempts:
                    logger.warning(
                        "短信邮件转发失败，达到重试上限后放弃: attempts=%d error_type=%s",
                        attempt,
                        error_type,
                    )
                    return
                logger.warning(
                    "短信邮件转发失败，准备重试: attempt=%d/%d error_type=%s",
                    attempt,
                    self._max_attempts,
                    error_type,
                )
                delay = self._retry_base_seconds * (2 ** (attempt - 1))
                if self._stop_event.wait(delay):
                    return


__all__ = [
    "ForwardSms",
    "SmsEmailForwarder",
    "SmsEmailSettings",
    "SmtpEmailSender",
    "build_email_message",
    "extract_otp",
    "is_valid_email_address",
    "load_sms_email_settings",
]
