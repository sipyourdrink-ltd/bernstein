"""Tests for the SMTP email sink (smtplib mocked)."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

import pytest

from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationEventKind,
    NotificationPermanentError,
)
from bernstein.core.notifications.sinks.email_smtp import EmailSmtpSink


def _config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "email-1",
        "kind": "email_smtp",
        "host": "smtp.example.com",
        "from_addr": "bernstein@example.com",
        "to_addrs": ["alice@example.com"],
    }
    base.update(overrides)
    return base


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_id="ev-mail",
        kind=NotificationEventKind.POST_MERGE,
        title="Merge done",
        body="success",
        severity="info",
        timestamp=1.0,
    )


class _FakeSMTP:
    """Stand-in for ``smtplib.SMTP`` used to capture the send args."""

    last: _FakeSMTP | None = None

    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.sent_messages: list[EmailMessage] = []
        _FakeSMTP.last = self

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, msg: EmailMessage) -> None:
        self.sent_messages.append(msg)


@pytest.mark.asyncio
async def test_email_send_uses_starttls_and_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    sink = EmailSmtpSink(_config(username="u", password="p"))
    await sink.deliver(_event())
    fake = _FakeSMTP.last
    assert fake is not None
    assert fake.starttls_called is True
    assert fake.login_args == ("u", "p")
    assert len(fake.sent_messages) == 1
    sent = fake.sent_messages[0]
    assert "Merge done" in sent["Subject"]
    assert sent["X-Bernstein-Event-Id"] == "ev-mail"


@pytest.mark.asyncio
async def test_email_auth_failure_is_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AuthFail(_FakeSMTP):
        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"nope")

    monkeypatch.setattr(smtplib, "SMTP", _AuthFail)
    sink = EmailSmtpSink(_config(username="u", password="p"))
    with pytest.raises(NotificationPermanentError, match="auth failed"):
        await sink.deliver(_event())


@pytest.mark.asyncio
async def test_email_oserror_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Disconnect(_FakeSMTP):
        def send_message(self, msg: EmailMessage) -> None:
            raise smtplib.SMTPServerDisconnected("dropped")

    monkeypatch.setattr(smtplib, "SMTP", _Disconnect)
    sink = EmailSmtpSink(_config())
    with pytest.raises(NotificationDeliveryError, match="transient"):
        await sink.deliver(_event())


def test_email_requires_to_addrs() -> None:
    with pytest.raises(NotificationPermanentError, match="to_addrs"):
        EmailSmtpSink(_config(to_addrs=[]))


def test_email_requires_host() -> None:
    cfg = _config()
    cfg.pop("host")
    with pytest.raises(NotificationPermanentError, match="host"):
        EmailSmtpSink(cfg)
