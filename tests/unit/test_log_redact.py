"""Tests for PII redaction logging filter (log_redact.py).

Covers:
- redact_pii(): email, phone, SSN, credit card patterns
- redact_pii(): clean text passes through unchanged
- PiiRedactingFilter: redacts record.msg (eager string)
- PiiRedactingFilter: redacts record.args (tuple and dict)
- install_pii_filter(): attaches to root logger, idempotent
- End-to-end: logged messages contain no PII
"""

from __future__ import annotations

import logging

from bernstein.core.log_redact import (
    PiiRedactingFilter,
    install_pii_filter,
    redact_pii,
    redact_sensitive_text,
)

# ---------------------------------------------------------------------------
# redact_pii() - individual patterns
# ---------------------------------------------------------------------------


class TestRedactEmail:
    def test_simple_email(self) -> None:
        assert redact_pii("contact alice@example.com today") == "contact [REDACTED] today"

    def test_email_with_plus(self) -> None:
        assert "[REDACTED]" in redact_pii("user+tag@domain.org")

    def test_no_email(self) -> None:
        assert redact_pii("no email here") == "no email here"


class TestRedactPhone:
    def test_us_phone(self) -> None:
        assert redact_pii("call (555) 123-4567") == "call [REDACTED]"

    def test_phone_with_dots(self) -> None:
        assert redact_pii("fax 555.123.4567") == "fax [REDACTED]"

    def test_international_prefix(self) -> None:
        assert redact_pii("ring +1-555-123-4567") == "ring [REDACTED]"

    def test_no_phone(self) -> None:
        assert redact_pii("port 8052") == "port 8052"


class TestRedactSSN:
    def test_ssn(self) -> None:
        assert redact_pii("ssn 123-45-6789") == "ssn [REDACTED]"

    def test_no_ssn(self) -> None:
        assert redact_pii("version 1.2.3") == "version 1.2.3"


class TestRedactCreditCard:
    def test_cc_spaces(self) -> None:
        assert redact_pii("card 4111 1111 1111 1111") == "card [REDACTED]"

    def test_cc_dashes(self) -> None:
        assert redact_pii("card 4111-1111-1111-1111") == "card [REDACTED]"

    def test_cc_contiguous(self) -> None:
        assert redact_pii("card 4111111111111111") == "card [REDACTED]"

    def test_no_cc(self) -> None:
        assert redact_pii("id 12345") == "id 12345"


class TestRedactMultiple:
    def test_mixed_pii(self) -> None:
        text = "User alice@acme.com SSN 123-45-6789 card 4111111111111111"
        result = redact_pii(text)
        assert "alice@acme.com" not in result
        assert "123-45-6789" not in result
        assert "4111111111111111" not in result
        assert result.count("[REDACTED]") == 3


class TestCleanTextUnchanged:
    def test_plain(self) -> None:
        assert redact_pii("Task T001 completed in 3.2s") == "Task T001 completed in 3.2s"

    def test_empty(self) -> None:
        assert redact_pii("") == ""


class TestCredentialRedaction:
    def test_explicit_credential_shapes_are_redacted(self) -> None:
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            "VGhpcyBpcyBhIHRlc3QgY2FuYXJ5LCBub3QgYSByZWFsIGtleQ==\n"
            "-----END PRIVATE KEY-----"
        )
        canaries = [
            "BearerTokenCanary0123456789",
            "JsonBearerTokenCanary0123456789",
            "ghp_0123456789abcdefghijklmnopqrstuv",
            "sk-proj-0123456789abcdefghijklmnop",
            "A1b2C3d4E5f6G7h8I9j0K1l2",
            "A1b2!C3d4@E5f6#G7h8$",
            private_key,
        ]
        text = "\n".join(
            [
                f"Authorization: Bearer {canaries[0]}",
                f'{{"Authorization": "Bearer {canaries[1]}"}}',
                canaries[2],
                canaries[3],
                f'api_token="{canaries[4]}"',
                f'accessToken="{canaries[5]}"',
                canaries[6],
            ]
        )

        redacted = redact_sensitive_text(text)

        for canary in canaries:
            assert canary not in redacted
        assert redacted.count("[REDACTED]") == len(canaries)

    def test_entropy_without_sensitive_name_survives(self) -> None:
        sha256 = "a3" * 32
        uuid = "123e4567-e89b-12d3-a456-426614174000"
        base64_payload = "VGhpcyBpcyBhIGxlZ2l0aW1hdGUgcGF5bG9hZA=="
        text = f"sha256={sha256} request_id={uuid} payload={base64_payload}"

        assert redact_sensitive_text(text) == text


# ---------------------------------------------------------------------------
# PiiRedactingFilter
# ---------------------------------------------------------------------------


class TestFilterMsg:
    def test_redacts_msg_string(self) -> None:
        f = PiiRedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="User alice@example.com logged in",
            args=None,
            exc_info=None,
        )
        assert f.filter(rec) is True
        assert "alice@example.com" not in rec.msg
        assert "[REDACTED]" in rec.msg

    def test_non_string_msg_ignored(self) -> None:
        f = PiiRedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=42,  # type: ignore[arg-type]
            args=None,
            exc_info=None,
        )
        assert f.filter(rec) is True
        assert rec.msg == 42  # type: ignore[comparison-overlap]


class TestFilterArgs:
    def test_redacts_tuple_args(self) -> None:
        f = PiiRedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Login from %s on port %d",
            args=("alice@example.com", 8052),
            exc_info=None,
        )
        f.filter(rec)
        assert isinstance(rec.args, tuple)
        assert "alice@example.com" not in rec.args[0]
        assert rec.args[1] == 8052  # int untouched

    def test_redacts_dict_args(self) -> None:
        f = PiiRedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Login from %(email)s",
            args=None,
            exc_info=None,
        )
        rec.args = {"email": "bob@acme.com"}
        f.filter(rec)
        assert isinstance(rec.args, dict)
        assert "bob@acme.com" not in rec.args["email"]

    def test_none_args_ok(self) -> None:
        f = PiiRedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="No PII here",
            args=None,
            exc_info=None,
        )
        assert f.filter(rec) is True


# ---------------------------------------------------------------------------
# install_pii_filter()
# ---------------------------------------------------------------------------


class TestInstall:
    def test_installs_on_root(self) -> None:
        root = logging.getLogger()
        # Remove any existing filter from prior test runs
        for f in root.filters.copy():
            if isinstance(f, PiiRedactingFilter):
                root.removeFilter(f)
        if hasattr(root, "_bernstein_pii_filter"):
            delattr(root, "_bernstein_pii_filter")

        filt = install_pii_filter()
        assert isinstance(filt, PiiRedactingFilter)
        assert filt in root.filters

        # Cleanup
        root.removeFilter(filt)
        delattr(root, "_bernstein_pii_filter")

    def test_idempotent(self) -> None:
        test_logger = logging.getLogger("test.idempotent")
        filt1 = install_pii_filter(test_logger)
        filt2 = install_pii_filter(test_logger)
        assert filt1 is filt2
        # Only one filter added
        pii_filters = [f for f in test_logger.filters if isinstance(f, PiiRedactingFilter)]
        assert len(pii_filters) == 1

        # Cleanup
        test_logger.removeFilter(filt1)
        delattr(test_logger, "_bernstein_pii_filter")

    def test_named_logger(self) -> None:
        test_logger = logging.getLogger("test.named")
        filt = install_pii_filter(test_logger)
        assert filt in test_logger.filters

        # Cleanup
        test_logger.removeFilter(filt)
        delattr(test_logger, "_bernstein_pii_filter")


# ---------------------------------------------------------------------------
# End-to-end: log message output
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_logged_message_redacted(self) -> None:
        test_logger = logging.getLogger("test.e2e")
        test_logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        test_logger.addHandler(handler)

        install_pii_filter(test_logger)

        # Capture formatted output
        records: list[str] = []
        orig_emit = handler.emit

        def capture_emit(record: logging.LogRecord) -> None:
            records.append(handler.format(record))
            orig_emit(record)

        handler.emit = capture_emit  # type: ignore[assignment]

        test_logger.info("User %s has SSN %s", "alice@corp.com", "123-45-6789")

        assert len(records) == 1
        assert "alice@corp.com" not in records[0]
        assert "123-45-6789" not in records[0]
        assert records[0].count("[REDACTED]") == 2

        # Cleanup
        test_logger.removeHandler(handler)
        for f in test_logger.filters.copy():
            if isinstance(f, PiiRedactingFilter):
                test_logger.removeFilter(f)
        if hasattr(test_logger, "_bernstein_pii_filter"):
            delattr(test_logger, "_bernstein_pii_filter")
