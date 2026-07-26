"""Log-sanitization tests for the auth middleware's denial sinks (#3070).

The middleware logs the request path and the agent id on every denial
branch.  ``path`` comes from the request line and is percent-decoded before
it reaches the middleware, so ``%0A`` (and every other percent-encoded
control byte) arrives as a raw control character.  An unescaped control
character in a log argument lets a caller forge a second log record.

Two properties are asserted here:

*   The sink wrapper (``sanitize_log``) escapes every character that a log
    reader treats as a record boundary, not only CR and LF.  ``str.splitlines``
    also breaks on VT, FF, FS, GS, RS, NEL, U+2028 and U+2029, and any of
    those survives URL parsing.
*   Every denial branch in the middleware routes its untrusted values
    through that wrapper, so a crafted path cannot forge a record on any of
    them.  Fixing one call site would leave the neighbouring branches open.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

# These tests exercise the secure-by-default middleware, so opt out of the
# autouse fixture that sets ``BERNSTEIN_AUTH_DISABLED`` for the suite.
pytestmark = pytest.mark.auth_enabled

_AUTH_LOGGER = "bernstein.core.security.auth_middleware"

# Characters that survive URL parsing and still split a log record or drive a
# terminal.  ``urlsplit`` removes TAB/CR/LF from the URL it is handed, so the
# rest of this set is what actually reaches the sink from a live request.
_RECORD_BREAKERS = "\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_TERMINAL_DRIVERS = "\x1b\x00\x07\x7f"
_INJECTION_SUFFIX = "\nWARNING forged record\r\n" + _RECORD_BREAKERS + _TERMINAL_DRIVERS


def _wire(raw_path: str) -> str:
    """Percent-encode *raw_path* the way an attacker would put it on the wire.

    The ASGI server decodes it back into ``scope["path"]``, which is how the
    control characters reach the middleware in production.
    """
    return quote(raw_path, safe="/")


def _is_clean(text: str) -> bool:
    """True when *text* is a single record with no control characters left."""
    if len(text.splitlines()) > 1:
        return False
    return all(ch == "\t" or (ch >= " " and ch != "\x7f" and not ("\x80" <= ch <= "\x9f")) for ch in text)


# ---------------------------------------------------------------------------
# Sink wrapper
# ---------------------------------------------------------------------------


def test_sanitize_log_escapes_every_record_breaker() -> None:
    """No character that ``str.splitlines`` breaks on survives the wrapper."""
    for ch in "\n\r" + _RECORD_BREAKERS:
        cleaned = sanitize_log(f"before{ch}after")

        assert ch not in cleaned, f"U+{ord(ch):04X} survived sanitize_log: {cleaned!r}"
        assert len(cleaned.splitlines()) == 1, f"U+{ord(ch):04X} still forges a record: {cleaned!r}"


def test_sanitize_log_escapes_control_bytes() -> None:
    """C0, DEL and C1 control bytes are escaped rather than passed through."""
    for ch in _TERMINAL_DRIVERS + "\x0e\x1f\x9b":
        cleaned = sanitize_log(f"before{ch}after")

        assert ch not in cleaned, f"U+{ord(ch):04X} survived sanitize_log: {cleaned!r}"
    assert _is_clean(sanitize_log("".join(chr(c) for c in range(0, 0xA0))))


def test_sanitize_log_keeps_printable_text_verbatim() -> None:
    """Sanitizing must not change the meaning of a benign value."""
    for value in ("safe content 123", "/tasks/abc-123/cancel", "agent-é中\U0001f600"):
        assert sanitize_log(value) == value


# ---------------------------------------------------------------------------
# Middleware denial branches
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """Build the real application so the real middleware stack runs."""
    from bernstein.core.server import create_app

    return create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")


def _agent_headers(application: FastAPI, *, task_ids: list[str]) -> dict[str, str]:
    store: Any = application.state.identity_store
    _, token = store.create_identity("session-log-injection-probe", "backend", task_ids=task_ids)
    return {"Authorization": f"Bearer {token}"}


def _denial_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _AUTH_LOGGER and r.levelno >= logging.WARNING]


def _assert_no_forged_record(caplog: pytest.LogCaptureFixture) -> None:
    records = _denial_records(caplog)
    assert records, "the denial branch did not log, so this test proves nothing"
    for record in records:
        message = record.getMessage()

        assert _is_clean(message), f"unsanitised value reached the log sink: {message!r}"


def test_operator_only_denial_log_is_sanitised(app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    """The ``admin:manage`` denial escapes the path it logs.

    Unknown write routes fail closed to ``admin:manage``, so this reaches
    the operator-only branch without depending on a specific route existing.
    """
    headers = _agent_headers(app, task_ids=[])
    path = "/unknown-write-route" + _INJECTION_SUFFIX

    with caplog.at_level(logging.WARNING, logger=_AUTH_LOGGER):
        response = TestClient(app, client=("10.9.0.1", 41001)).post(_wire(path), headers=headers, json={})

    assert response.status_code == 403, response.text
    _assert_no_forged_record(caplog)


def test_task_scope_denial_log_is_sanitised(app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    """The task-scope denial escapes both the agent id and the path.

    The scope error message embeds the addressed task id, which is a slice
    of the same attacker-controlled path, so it needs the wrapper too.
    """
    headers = _agent_headers(app, task_ids=["task-mine"])
    path = "/tasks/task-not-mine" + _INJECTION_SUFFIX + "/cancel"

    with caplog.at_level(logging.WARNING, logger=_AUTH_LOGGER):
        response = TestClient(app, client=("10.9.0.2", 41002)).post(_wire(path), headers=headers, json={})

    assert response.status_code == 403, response.text
    _assert_no_forged_record(caplog)
