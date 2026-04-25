"""SMTP email notification sink.

The driver delegates connection management to :mod:`smtplib` (stdlib)
and runs the blocking send on a background thread so an unresponsive
SMTP server doesn't block the orchestrator's event loop. Authentication
credentials may be inlined or referenced via ``${ENV_VAR}``.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage
from typing import Any

from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationPermanentError,
)

__all__ = ["EmailSmtpSink"]


class EmailSmtpSink:
    """Send notifications via SMTP.

    Required config keys::

        id: <unique sink id>
        kind: email_smtp
        host: smtp.example.com
        from_addr: bernstein@example.com
        to_addrs: [alice@example.com, ops@example.com]

    Optional::

        port: 587
        username: ${SMTP_USER}
        password: ${SMTP_PASS}
        starttls: true
        ssl: false
        timeout_s: 15.0
    """

    kind: str = "email_smtp"

    def __init__(self, config: dict[str, Any]) -> None:
        self.sink_id = str(config["id"])
        host = config.get("host")
        if not isinstance(host, str) or not host:
            raise NotificationPermanentError(
                f"email_smtp sink {self.sink_id!r} requires 'host'",
            )
        self._host = host
        self._port = int(config.get("port", 587))
        from_addr = config.get("from_addr")
        if not isinstance(from_addr, str) or not from_addr:
            raise NotificationPermanentError(
                f"email_smtp sink {self.sink_id!r} requires 'from_addr'",
            )
        self._from_addr = from_addr
        to_addrs = config.get("to_addrs")
        if not isinstance(to_addrs, list) or not to_addrs:
            raise NotificationPermanentError(
                f"email_smtp sink {self.sink_id!r} requires non-empty 'to_addrs'",
            )
        self._to_addrs = [str(a) for a in to_addrs]
        self._username = _resolve(config.get("username"))
        self._password = _resolve(config.get("password"))
        self._starttls = bool(config.get("starttls", True))
        self._ssl = bool(config.get("ssl", False))
        self._timeout = float(config.get("timeout_s", 15.0))

    async def deliver(self, event: NotificationEvent) -> None:
        """Send a synthesised email for ``event``."""
        msg = EmailMessage()
        msg["Subject"] = f"[bernstein {event.severity}] {event.title}"
        msg["From"] = self._from_addr
        msg["To"] = ", ".join(self._to_addrs)
        msg["X-Bernstein-Event-Id"] = event.event_id
        msg["X-Bernstein-Event-Kind"] = event.kind.value
        msg.set_content(_render_text(event))

        try:
            await asyncio.to_thread(self._send_blocking, msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise NotificationPermanentError(f"smtp auth failed: {exc}") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise NotificationPermanentError(f"smtp recipients refused: {exc}") from exc
        except (TimeoutError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
            raise NotificationDeliveryError(f"smtp transient error: {exc}") from exc
        except smtplib.SMTPException as exc:
            raise NotificationDeliveryError(f"smtp error: {exc}") from exc
        except OSError as exc:
            raise NotificationDeliveryError(f"smtp network error: {exc}") from exc

    async def close(self) -> None:
        """No-op: connection is per-send."""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _send_blocking(self, msg: EmailMessage) -> None:
        smtp_cls = smtplib.SMTP_SSL if self._ssl else smtplib.SMTP
        with smtp_cls(self._host, self._port, timeout=self._timeout) as client:
            if self._starttls and not self._ssl:
                client.starttls()
            if self._username and self._password:
                client.login(self._username, self._password)
            client.send_message(msg)


def _render_text(event: NotificationEvent) -> str:
    parts = [event.title, ""]
    if event.body:
        parts.extend([event.body, ""])
    parts.append(f"event_id: {event.event_id}")
    parts.append(f"kind: {event.kind.value}")
    parts.append(f"severity: {event.severity}")
    if event.task_id:
        parts.append(f"task_id: {event.task_id}")
    if event.session_id:
        parts.append(f"session_id: {event.session_id}")
    if event.run_id:
        parts.append(f"run_id: {event.run_id}")
    return "\n".join(parts) + "\n"


def _resolve(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1])
    return value
