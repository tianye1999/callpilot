"""SMS -> email forwarding: OTP detection, payload safety, SMTP and worker lifecycle."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from agentcall.sms_email_forwarder import (
    ForwardSms,
    SmsEmailForwarder,
    SmsEmailSettings,
    SmtpEmailSender,
    build_email_message,
    extract_otp,
    is_valid_email_address,
    load_sms_email_settings,
)


def settings(**overrides) -> SmsEmailSettings:
    values = {
        "recipient": "owner@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_username": "sender@example.com",
        "smtp_password": "app-password-value",
        "from_address": "sender@example.com",
        "timeout_seconds": 10.0,
    }
    values.update(overrides)
    return SmsEmailSettings(**values)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("【服务】验证码：9404，此验证码有效保留24小时", "9404"),
        ("您的验证码为 5678，5分钟内有效", "5678"),
        ("校验码: 0921", "0921"),
        ("动态密码4839，有效期10分钟", "4839"),
        ("Your verification code is 872913", "872913"),
        ("Your OTP is 4521", "4521"),
        ("G-459201 is your verification code", "459201"),
        ("Use 208941 as your account verification code", "208941"),
        ("请回电13800138000，工单验证码9981", "9981"),
        ("尾号1234的卡消费500元，验证码 6677", "6677"),
        ("验证码为8888，客服电话4001234567", "8888"),
        ("PIN: 8080", "8080"),
        ("验证码：0000", "0000"),
        ("验证码：2026", "2026"),
        ("验证码123456号", "123456"),
    ],
)
def test_extract_otp_positive_samples(body, expected):
    assert extract_otp(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "话费41.4元，流量172.67MB，通话时长7分钟",
        "2026-07-04CA8202航班登机口D08",
        "快递单号1234567890已签收",
        "扫码支付成功，金额128元",
        "You have 3 verification attempts left",
        "zipcode 10001 has been verified",
        "订单号202607061234已创建",
        "行程码已更新，请扫码查看",
        "Your card ending 1234 was used. If this was not you, verify your account in the app.",
        "Account 12345678 was updated. Never share your verification code with anyone.",
        "Your ZIP code is 10001",
        "Your verification code for order 12345678 will arrive separately",
        "尾号1234，验证码稍后发送",
        "订单 87654321 的验证码将另行发送",
        "Order 12345678: your verification code will arrive separately",
        "您的尾号 1234 是登录验证码接收卡号",
        "支付5000元，验证码稍后发送",
        "余额1000元验证码通知",
        "验证码：2026年服务将在稍后开始",
    ],
)
def test_extract_otp_negative_samples(body):
    assert extract_otp(body) is None


@pytest.mark.parametrize(
    "address",
    ["owner@example.com", "name.surname+tag@sub.example.co.uk"],
)
def test_email_address_validation_accepts_single_mailbox(address):
    assert is_valid_email_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "missing-at.example.com",
        "two@example.com,other@example.com",
        "Name <owner@example.com>",
        "owner@example.com\r\nBcc: attacker@example.com",
        ".owner@example.com",
        "owner@example",
    ],
)
def test_email_address_validation_rejects_invalid_or_multiple_mailboxes(address):
    assert not is_valid_email_address(address)


def test_build_email_message_prefixes_otp_and_preserves_unicode_body():
    received_at = datetime(2026, 7, 10, 8, 9, 10, tzinfo=timezone.utc)
    message = build_email_message(
        ForwardSms("10086", "您的验证码是 482913，请勿泄露", received_at),
        settings(),
    )

    assert message["Subject"] == "【验证码 482913】[CallPilot 短信转发] 来自 10086"
    assert message["To"] == "owner@example.com"
    assert message["From"] == "CallPilot <sender@example.com>"
    content = message.get_content()
    assert "发件号码: 10086" in content
    assert "接收时间: 2026-07-10 08:09:10 +0000" in content
    assert "您的验证码是 482913，请勿泄露" in content
    assert message.get_content_charset() == "utf-8"


def test_build_email_message_without_otp_uses_plain_subject_and_unknown_sender():
    message = build_email_message(
        ForwardSms(None, "本月账单已生成，金额128元", datetime.now(timezone.utc)),
        settings(),
    )

    assert message["Subject"] == "[CallPilot 短信转发] 来自 未知发件人"
    assert "发件号码: 未知" in message.get_content()


def test_build_email_message_sanitizes_untrusted_sender_header():
    message = build_email_message(
        ForwardSms(
            "10086\r\nBcc: attacker@example.com",
            "普通短信",
            datetime.now(timezone.utc),
        ),
        settings(),
    )

    assert "\r" not in str(message["Subject"])
    assert "\n" not in str(message["Subject"])
    assert message["Bcc"] is None


class FakeSmtp:
    def __init__(self, host, port, *, timeout):
        self.calls = [("connect", host, port, timeout)]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.calls.append(("close",))

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, *, context):
        self.calls.append(("starttls", context))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        self.calls.append(("send_message", message["To"]))


class FakeSmtpSsl(FakeSmtp):
    def __init__(self, host, port, *, timeout, context):
        super().__init__(host, port, timeout=timeout)
        self.calls.append(("ssl_context", context))


def test_smtp_sender_uses_starttls_before_authentication():
    clients = []

    def factory(*args, **kwargs):
        client = FakeSmtp(*args, **kwargs)
        clients.append(client)
        return client

    sender = SmtpEmailSender(
        smtp_factory=factory,
        smtp_ssl_factory=FakeSmtpSsl,
        context_factory=lambda: "verified-context",
    )
    message = EmailMessage()
    message["To"] = "owner@example.com"

    sender.send(settings(), message)

    assert clients[0].calls == [
        ("connect", "smtp.example.com", 587, 10.0),
        ("ehlo",),
        ("starttls", "verified-context"),
        ("ehlo",),
        ("login", "sender@example.com", "app-password-value"),
        ("send_message", "owner@example.com"),
        ("close",),
    ]


def test_smtp_sender_supports_implicit_tls_without_plaintext_fallback():
    clients = []

    def ssl_factory(*args, **kwargs):
        client = FakeSmtpSsl(*args, **kwargs)
        clients.append(client)
        return client

    sender = SmtpEmailSender(
        smtp_factory=FakeSmtp,
        smtp_ssl_factory=ssl_factory,
        context_factory=lambda: "verified-context",
    )

    sender.send(settings(smtp_security="ssl", smtp_port=465), EmailMessage())

    assert ("ssl_context", "verified-context") in clients[0].calls
    assert not any(call[0] == "starttls" for call in clients[0].calls)
    assert any(call[0] == "send_message" for call in clients[0].calls)


def test_forwarder_enqueue_is_nonblocking_while_sender_is_slow():
    started = threading.Event()
    release = threading.Event()

    def slow_send(_settings, _message):
        started.set()
        release.wait(timeout=2)

    forwarder = SmsEmailForwarder(
        settings_loader=settings,
        send_email=slow_send,
        retry_base_seconds=0,
    )
    before = time.monotonic()
    assert forwarder.enqueue("10086", "普通短信") is True
    elapsed = time.monotonic() - before

    assert elapsed < 0.1
    assert started.wait(timeout=1)
    release.set()
    forwarder.stop(timeout=1)
    assert not forwarder.is_running


def test_forwarder_retries_with_a_bound_and_eventually_succeeds():
    sent = threading.Event()
    attempts = []

    def flaky_send(_settings, _message):
        attempts.append(time.monotonic())
        if len(attempts) < 3:
            raise OSError("temporary")
        sent.set()

    forwarder = SmsEmailForwarder(
        settings_loader=settings,
        send_email=flaky_send,
        max_attempts=3,
        retry_base_seconds=0,
    )
    assert forwarder.enqueue("10086", "普通短信")
    assert sent.wait(timeout=1)
    forwarder.stop(timeout=1)

    assert len(attempts) == 3


def test_forwarder_failure_logs_never_include_body_code_or_password(caplog):
    attempted = threading.Event()
    body = "验证码 654321"
    password = "smtp-password-secret"

    def fail(_settings, _message):
        attempted.set()
        raise RuntimeError(f"failed with {body} and {password}")

    caplog.set_level(logging.WARNING)
    forwarder = SmsEmailForwarder(
        settings_loader=lambda: settings(smtp_password=password),
        send_email=fail,
        max_attempts=1,
        retry_base_seconds=0,
    )
    assert forwarder.enqueue("10086", body)
    assert attempted.wait(timeout=1)
    deadline = time.monotonic() + 1
    while "放弃" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.01)
    forwarder.stop(timeout=1)

    assert body not in caplog.text
    assert "654321" not in caplog.text
    assert password not in caplog.text
    assert "RuntimeError" in caplog.text


def test_forwarder_disabled_does_not_start_worker():
    forwarder = SmsEmailForwarder(settings_loader=lambda: None)

    assert forwarder.enqueue("10086", "普通短信") is False
    assert not forwarder.is_running
    forwarder.stop(timeout=1)


def test_live_settings_loader_defaults_off_and_never_raises_on_partial_config(
    monkeypatch, caplog
):
    monkeypatch.setenv("SMS_EMAIL_FORWARD_ENABLED", "false")
    assert load_sms_email_settings() is None

    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("SMS_EMAIL_FORWARD_ENABLED", "true")
    monkeypatch.setenv("SMS_EMAIL_RECIPIENT", "owner@example.com")
    monkeypatch.delenv("SMS_EMAIL_SMTP_HOST", raising=False)
    assert load_sms_email_settings() is None
    assert "owner@example.com" not in caplog.text


def test_live_settings_loader_reads_valid_runtime_config(monkeypatch):
    values = {
        "SMS_EMAIL_FORWARD_ENABLED": "true",
        "SMS_EMAIL_RECIPIENT": "owner@example.com",
        "SMS_EMAIL_SMTP_HOST": "smtp.example.com",
        "SMS_EMAIL_SMTP_PORT": "465",
        "SMS_EMAIL_SMTP_SECURITY": "ssl",
        "SMS_EMAIL_SMTP_USERNAME": "sender@example.com",
        "SMS_EMAIL_SMTP_PASSWORD": "test-app-password",
        "SMS_EMAIL_FROM": "sender@example.com",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    loaded = load_sms_email_settings()

    assert loaded is not None
    assert loaded.recipient == "owner@example.com"
    assert loaded.smtp_port == 465
    assert loaded.smtp_security == "ssl"


def test_live_settings_loader_rejects_invalid_port_instead_of_using_default(
    monkeypatch, caplog
):
    values = {
        "SMS_EMAIL_FORWARD_ENABLED": "true",
        "SMS_EMAIL_RECIPIENT": "owner@example.com",
        "SMS_EMAIL_SMTP_HOST": "smtp.example.com",
        "SMS_EMAIL_SMTP_PORT": "not-a-port",
        "SMS_EMAIL_SMTP_SECURITY": "starttls",
        "SMS_EMAIL_SMTP_USERNAME": "sender@example.com",
        "SMS_EMAIL_SMTP_PASSWORD": "test-app-password",
        "SMS_EMAIL_FROM": "sender@example.com",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    caplog.set_level(logging.WARNING)
    assert load_sms_email_settings() is None
    assert "not-a-port" not in caplog.text


def test_smtp_password_is_hidden_from_settings_repr():
    password = "test-secret-value"

    assert password not in repr(settings(smtp_password=password))


def test_forwarder_queue_is_bounded_and_drops_without_blocking():
    started = threading.Event()
    release = threading.Event()

    def slow_send(_settings, _message):
        started.set()
        release.wait(timeout=2)

    forwarder = SmsEmailForwarder(
        settings_loader=settings,
        send_email=slow_send,
        queue_size=1,
        retry_base_seconds=0,
    )
    assert forwarder.enqueue("10086", "first")
    assert started.wait(timeout=1)
    assert forwarder.enqueue("10086", "second")
    assert forwarder.enqueue("10086", "third") is False
    release.set()
    forwarder.stop(timeout=1)


def test_forwarder_stop_interrupts_retry_backoff_and_joins_worker():
    attempted = threading.Event()
    attempts = []

    def fail(_settings, _message):
        attempts.append(1)
        attempted.set()
        raise OSError("temporary")

    forwarder = SmsEmailForwarder(
        settings_loader=settings,
        send_email=fail,
        max_attempts=3,
        retry_base_seconds=30,
    )
    assert forwarder.enqueue("10086", "普通短信")
    assert attempted.wait(timeout=1)

    forwarder.stop(timeout=1)

    assert not forwarder.is_running
    assert attempts == [1]


def test_forwarder_survives_payload_build_error_and_processes_next_sms(caplog):
    sent = threading.Event()

    def build_message(sms, _settings):
        if sms.body == "malformed secret body":
            raise ValueError("malformed secret body")
        return EmailMessage()

    def send(_settings, _message):
        sent.set()

    caplog.set_level(logging.WARNING)
    forwarder = SmsEmailForwarder(
        settings_loader=settings,
        build_message=build_message,
        send_email=send,
        retry_base_seconds=0,
    )
    assert forwarder.enqueue("10086", "malformed secret body")
    assert forwarder.enqueue("10086", "next message")
    assert sent.wait(timeout=1)
    forwarder.stop(timeout=1)

    assert "malformed secret body" not in caplog.text
    assert "ValueError" in caplog.text
