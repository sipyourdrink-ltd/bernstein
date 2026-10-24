"""Tests for PII and secret detection gate (pii_output_gate.py).

Covers:
- Secret detection: AWS keys, GitHub tokens, Slack tokens, Stripe keys,
  private keys, generic API keys, passwords, connection strings, bearer tokens, JWTs
- PII detection: emails, phone numbers, SSNs
- Allowlist filtering: example.com, test@, placeholder, localhost, scoped to the
  flagged value rather than the line it sits on
- Diff scanning: only added lines scanned, removals and headers ignored
- Redaction: raw secrets never stored in findings
- format_findings output
- Integration with quality gates via _run_pii_gate
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.pii_output_gate import (
    SecretFinding,
    format_findings,
    scan_diff,
    scan_text,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# scan_text: secret detection
# ---------------------------------------------------------------------------


class TestScanTextSecrets:
    def test_aws_access_key_detected(self) -> None:
        text = 'aws_key = "AKIAIOSFODNN7EXAMPLE"'
        findings = scan_text(text)
        assert any(f.rule == "aws_access_key" for f in findings)

    def test_aws_secret_key_detected(self) -> None:
        text = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
        findings = scan_text(text)
        assert any(f.rule == "aws_secret_key" for f in findings)

    def test_github_token_detected(self) -> None:
        text = 'token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"'
        findings = scan_text(text)
        assert any(f.rule == "github_token" for f in findings)

    def test_slack_token_detected(self) -> None:
        # Build token dynamically to avoid GitHub push protection
        prefix = "xoxb"
        text = f"SLACK_TOKEN={prefix}-000FAKE000-000FAKE000-FakeTokenVal"
        findings = scan_text(text)
        assert any(f.rule == "slack_token" for f in findings)

    def test_stripe_key_detected(self) -> None:
        # Build key dynamically to avoid GitHub push protection
        prefix = "sk_" + "live"
        text = f'stripe_key = "{prefix}_FAKEFAKEFAKEFAKE"'
        findings = scan_text(text)
        assert any(f.rule == "stripe_key" for f in findings)

    def test_private_key_detected(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQ..."
        findings = scan_text(text)
        assert any(f.rule == "private_key" for f in findings)

    def test_generic_api_key_detected(self) -> None:
        text = 'api_key = "sk-proj-abcdef1234567890ABCD"'
        findings = scan_text(text)
        assert any(f.rule == "generic_api_key" for f in findings)

    def test_password_assignment_detected(self) -> None:
        text = 'db_password = "SuperS3cret!Pass"'
        findings = scan_text(text)
        assert any(f.rule == "password_assignment" for f in findings)

    def test_connection_string_detected(self) -> None:
        text = 'DATABASE_URL = "postgres://user:pass@dbhost:5432/mydb"'
        findings = scan_text(text)
        assert any(f.rule == "connection_string" for f in findings)

    def test_bearer_token_detected(self) -> None:
        text = 'authorization = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def"'
        findings = scan_text(text)
        assert any(f.rule == "bearer_token" for f in findings)

    def test_jwt_token_detected(self) -> None:
        text = "token = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        findings = scan_text(text)
        assert any(f.rule == "jwt_token" for f in findings)

    def test_gcp_service_account_detected(self) -> None:
        text = '{\n  "type": "service_account",\n  "project_id": "my-project"\n}'
        findings = scan_text(text)
        assert any(f.rule == "gcp_service_account" for f in findings)


# ---------------------------------------------------------------------------
# scan_text: PII detection
# ---------------------------------------------------------------------------


class TestScanTextPii:
    def test_email_detected(self) -> None:
        text = "Contact alice@realcompany.com for details."
        findings = scan_text(text)
        assert any(f.rule == "email_address" for f in findings)

    def test_phone_detected(self) -> None:
        text = "Call 415-555-1234 for support."
        findings = scan_text(text)
        assert any(f.rule == "phone_number" for f in findings)

    def test_ssn_detected(self) -> None:
        text = "SSN: 123-45-6789"
        findings = scan_text(text)
        assert any(f.rule == "ssn" for f in findings)

    def test_pii_severity_is_medium(self) -> None:
        text = "Contact alice@realcompany.com"
        findings = scan_text(text)
        pii = [f for f in findings if f.rule == "email_address"]
        assert pii and pii[0].severity == "medium"

    def test_secret_severity_is_high(self) -> None:
        text = 'api_key = "sk-proj-abcdef1234567890ABCD"'
        findings = scan_text(text)
        secrets = [f for f in findings if f.rule == "generic_api_key"]
        assert secrets and secrets[0].severity == "high"

    def test_credit_card_detected(self) -> None:
        text = "card = '4111 1111 1111 1111'"
        findings = scan_text(text)
        assert any(f.rule == "credit_card_number" for f in findings)

    def test_high_entropy_secret_detected(self) -> None:
        text = 'secret_token = "AbCdEf1234567890GhIjKlMnOpQr"'
        findings = scan_text(text)
        assert any(f.rule in {"high_entropy_assignment", "generic_api_key"} for f in findings)


# ---------------------------------------------------------------------------
# scan_text: allowlist filtering
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_example_com_email_allowed(self) -> None:
        text = "user@example.com"
        findings = scan_text(text)
        assert not any(f.rule == "email_address" for f in findings)

    def test_test_email_allowed(self) -> None:
        text = "test@company.org"
        findings = scan_text(text)
        assert not any(f.rule == "email_address" for f in findings)

    def test_noreply_email_allowed(self) -> None:
        text = "noreply@company.org"
        findings = scan_text(text)
        assert not any(f.rule == "email_address" for f in findings)

    def test_placeholder_api_key_allowed(self) -> None:
        text = 'api_key = "your_api_key_here"'
        findings = scan_text(text)
        assert not any(f.rule == "generic_api_key" for f in findings)

    def test_changeme_password_allowed(self) -> None:
        text = 'password = "changeme"'
        findings = scan_text(text)
        assert not any(f.rule == "password_assignment" for f in findings)

    def test_localhost_connection_allowed(self) -> None:
        text = 'url = "postgres://user:pass@localhost:5432/testdb"'
        findings = scan_text(text)
        assert not any(f.rule == "connection_string" for f in findings)

    def test_dummy_value_allowed(self) -> None:
        text = 'api_key = "dummy_key_for_testing"'
        findings = scan_text(text)
        assert not any(f.rule == "generic_api_key" for f in findings)

    def test_test_prefix_allowed(self) -> None:
        text = 'api_key = "TEST_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"'
        findings = scan_text(text)
        assert findings == []

    def test_example_prefix_allowed(self) -> None:
        text = 'secret_token = "EXAMPLE_AbCdEf1234567890GhIjKlMn"'
        findings = scan_text(text)
        assert findings == []


# ---------------------------------------------------------------------------
# scan_text: the allowlist exempts benign values, not the lines they sit on
# ---------------------------------------------------------------------------

# Token-shaped values are assembled at runtime so push protection does not
# read the test file as a leak.
_GITHUB_TOKEN = "gh" + "p_" + "Ab3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5"
_STRIPE_KEY = "sk_" + "live_" + "a1B2c3D4e5F6g7H8i9J0"
_URL_CREDS = "postgres" + "://svc:S3cretPass@"
_DB_URL = "postgres" + "://admin:S3cretPass@db.prod.internal:5432/app"

#: Every built-in benign marker, written the way it turns up beside real data.
_BENIGN_MARKERS = [
    "see https://example.com/docs",
    "mirror at example.org",
    "or example.net",
    "contact test@corp.io",
    "owner user@corp.io",
    "escalate to admin@corp.io",
    "from noreply@corp.io",
    "from no-reply@corp.io",
    "replace placeholder later",
    "changeme",
    "set your_api_key in CI",
    "xxxx",
    "falls back to localhost",
    "or 127.0.0.1",
    "binds 0.0.0.0",
    "password = 'test'",
    'fixture = "FAKE-USER"',
    'fixture = "TEST-USER"',
    'fixture = "EXAMPLE-USER"',
    'fixture = "DUMMY-USER"',
    'fixture = "PLACEHOLDER-USER"',
    'fixture = "LOCALHOST-USER"',
]


def _rules(text: str, **kwargs: object) -> list[str]:
    return [f.rule for f in scan_text(text, **kwargs)]  # type: ignore[arg-type]


class TestAllowlistIsScopedToTheFlaggedValue:
    """A benign token elsewhere on a line must not hide a real value beside it."""

    @pytest.mark.parametrize("marker", _BENIGN_MARKERS)
    def test_a_real_ssn_beside_any_marker_still_blocks(self, marker: str) -> None:
        findings = scan_text(f'{marker}; ssn = "123-45-6789"')

        ssn = [f for f in findings if f.rule == "ssn"]
        assert len(ssn) == 1
        assert ssn[0].block_merge

    @pytest.mark.parametrize(
        ("line", "rule"),
        [
            (f'GITHUB_TOKEN = "{_GITHUB_TOKEN}"  # replace placeholder in CI', "github_token"),
            (f'STRIPE = "{_STRIPE_KEY}"  # docs: https://example.com/billing', "stripe_key"),
            (f'DATABASE_URL = "{_DB_URL}"  # localhost in dev', "connection_string"),
            ('{"email": "user@acme.io", "ssn": "123-45-6789"}', "ssn"),
            ('fixture = "FAKE-USER"; card = "4111 1111 1111 1111"', "credit_card_number"),
        ],
    )
    def test_a_real_secret_or_pii_value_beside_a_marker_is_reported(self, line: str, rule: str) -> None:
        findings = [f for f in scan_text(line) if f.rule == rule]

        assert len(findings) == 1
        assert findings[0].block_merge

    def test_a_benign_value_does_not_shadow_a_real_one_of_the_same_rule(self) -> None:
        findings = scan_text('a = "FAKE-123-45-6789"; b = "987-65-4320"')

        ssn = [f for f in findings if f.rule == "ssn"]
        assert len(ssn) == 1
        assert "FAKE" not in ssn[0].redacted_match
        assert 'b = "***"' in ssn[0].redacted_match

    def test_an_allowlisted_address_does_not_hide_a_real_one(self) -> None:
        findings = scan_text("cc: test@corp.io, jane.doe@acme.io")

        assert [f.rule for f in findings] == ["email_address"]
        assert findings[0].redacted_match.endswith(", ***")

    @pytest.mark.parametrize(
        ("suppressed", "flagged"),
        [
            ('ssn = "FAKE-123-45-6789"', 'ssn = "123-45-6789"'),
            ('url = "postgres://user:pass@localhost:5432/testdb"', f'url = "{_DB_URL}"'),
            ('api_key = "TEST_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"', 'api_key = "QZ7_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"'),
            ('api_key = "your_api_key_here"', 'api_key = "kq81_vmP2xr7Tz4LwN9c"'),
            ("owner user@example.com", "owner jane.doe@acme.io"),
        ],
    )
    def test_a_marker_inside_the_flagged_value_still_exempts_it(self, suppressed: str, flagged: str) -> None:
        """Each exemption is pinned against a counterpart that is reported."""
        assert scan_text(suppressed) == []
        assert scan_text(flagged) != []

    def test_operator_prefixes_scope_the_same_way(self) -> None:
        assert _rules('ssn = "ACME-123-45-6789"', allowlist_prefixes=["ACME"]) == []
        assert _rules('fixture = "ACME-SYNTH"; ssn = "123-45-6789"', allowlist_prefixes=["ACME"]) == ["ssn"]

    def test_scan_diff_applies_the_same_scope(self) -> None:
        diff = '+++ b/customers.json\n@@ -0,0 +1 @@\n+{"email": "user@acme.io", "ssn": "123-45-6789"}\n'

        assert [f.rule for f in scan_diff(diff)] == ["ssn"]

    def test_the_pii_scan_gate_blocks_a_mixed_line(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        (tmp_path / "settings.py").write_text(f'GITHUB_TOKEN = "{_GITHUB_TOKEN}"  # replace placeholder\n')

        result = _run_pii_gate(QualityGatesConfig(pii_scan=True), tmp_path, changed_files=["settings.py"])

        assert not result.passed
        assert result.blocked
        assert "github_token" in result.detail

    @pytest.mark.parametrize(
        ("contains_marker", "is_marker", "rule"),
        [
            # ``test@`` inside ``latest@``: a real credential in a real URL.
            (
                'DATABASE_URL = "postgres://svc:latest@db.prod.internal:5432/app"',
                'DATABASE_URL = "postgres://svc:pw@localhost:5432/app"',
                "connection_string",
            ),
            # A URL password that *is* a marker: ``svc:admin@`` is a credential.
            (
                'DATABASE_URL = "postgres://svc:admin@db.prod.internal:5432/app"',
                'DATABASE_URL = "postgres://svc:admin@localhost:5432/app"',
                "connection_string",
            ),
            (
                'DATABASE_URL = "postgres://svc:test@db.prod.internal/app"',
                'DATABASE_URL = "postgres://svc:test@localhost/app"',
                "connection_string",
            ),
            ("owner sysadmin@acme-corp.io", "owner admin@acme-corp.io", "email_address"),
            ("owner contact.test@acme-corp.io", "owner test@acme-corp.io", "email_address"),
            ("owner ceo@counterexample.com", "owner ceo@example.com", "email_address"),
            (
                'DATABASE_URL = "postgres://svc:pw9x@db.myexample.org:5432/app"',
                'DATABASE_URL = "postgres://svc:pw9x@db.example.org:5432/app"',
                "connection_string",
            ),
            # ``.com.au`` is a registrable domain of its own, not example.com.
            ("owner jane@example.com.au", "owner jane@example.com", "email_address"),
            # The local-host and placeholder markers are anchored the same way: a
            # real host, database or path that only contains one is not exempt.
            (
                f'DATABASE_URL = "{_URL_CREDS}localhost-proxy.prod.internal:5432/app"',
                f'DATABASE_URL = "{_URL_CREDS}localhost:5432/app"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}localhost.prod.internal:5432/app"',
                f'DATABASE_URL = "{_URL_CREDS}localhost:5432/app"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/app_localhost"',
                f'DATABASE_URL = "{_URL_CREDS}localhost:5432/app"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}10.0.0.0:5432/app"',
                f'DATABASE_URL = "{_URL_CREDS}0.0.0.0:5432/app"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}127.0.0.10:5432/app"',
                f'DATABASE_URL = "{_URL_CREDS}127.0.0.1:5432/app"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/placeholders"',
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/placeholder"',
                "connection_string",
            ),
            # ``-`` and ``_`` join words too: a ``placeholder-proxy`` host or an
            # ``app-changeme-2`` database is a real name that contains a marker,
            # and the connection-string match covers the whole URL.
            (
                f'DATABASE_URL = "{_URL_CREDS}placeholder-proxy.prod.internal:5432/app"',
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/placeholder"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/app-changeme-2"',
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/changeme"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/app_placeholder"',
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/placeholder"',
                "connection_string",
            ),
            # A token that only contains the word is a token; one that starts
            # with the ``PLACEHOLDER`` value prefix is still a fixture.
            (
                'auth_token = "xxx-placeholder-token-xxx"',
                'auth_token = "placeholder-token"',
                "generic_api_key",
            ),
            # A marker used as a DNS label is a host name, not a marker; the
            # host marker already reads ``localhost.prod.internal`` that way.
            (
                f'DATABASE_URL = "{_URL_CREDS}db.changeme.prod.internal:5432/app"',
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/changeme"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}placeholder.prod.internal:5432/app"',
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/placeholder"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}db.your-api-key.prod.internal:5432/app"',
                'DATABASE_URL = "postgres://svc:your-api-key@db.prod.internal:5432/app"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}your-api-key.prod.internal:5432/app"',
                'DATABASE_URL = "postgres://svc:your-api-key@db.prod.internal:5432/app"',
                "connection_string",
            ),
            # A marker embedded in a database or path token is a real token,
            # not an exempt fixture value.
            (
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/app_your_api_key"',
                'DATABASE_URL = "postgres://svc:your-api-key@db.prod.internal:5432/app"',
                "connection_string",
            ),
            (
                f'DATABASE_URL = "{_URL_CREDS}db.prod.internal:5432/app-your-api-key"',
                'DATABASE_URL = "postgres://svc:your-api-key@db.prod.internal:5432/app"',
                "connection_string",
            ),
        ],
    )
    def test_a_value_that_only_contains_a_marker_is_not_exempt(
        self, contains_marker: str, is_marker: str, rule: str
    ) -> None:
        assert rule in _rules(contains_marker)
        assert rule not in _rules(is_marker)

    @pytest.mark.parametrize(
        "line",
        [
            "contact mailto:test@example.com",
            "see example.com.",
            "see https://api.example.com/x",
            "write to user@example.com.",
            'bind = "0.0.0.0:8080"  # all interfaces',
            'API = "http://127.0.0.1:8052/tasks"',
            "falls back to localhost.",
            "replace placeholder later",
            'api_key = "your_api_key_here"',
            "token = xxxxxxxxxxxxxxxxxxxx",
            'auth_token = "placeholder-token"',
            'token = "placeholder"',
            "SECRET = <changeme>",
            "replace the placeholder.",
            "set it to your-api-key.",
        ],
    )
    def test_anchoring_keeps_benign_uses_exempt(self, line: str) -> None:
        assert _rules(line) == []

    def test_scan_cost_grows_linearly_with_a_marker_dense_line(self) -> None:
        """One long line dense with markers and matches - a minified bundle or a source map.

        Checking each match against a per-line mask keeps the scan linear;
        checking it against every benign span made it quadratic, and the gate
        runs with no timeout. Compared as a ratio, so machine speed cancels out:
        4x the matches must cost well under the ~16x a quadratic scan pays.
        """
        import time

        def best_of_three(count: int) -> float:
            line = ",".join(['xxxx"4111111111111111"'] * count)
            timings = []
            for _ in range(3):
                started = time.perf_counter()
                assert len(scan_text(line)) == count
                timings.append(time.perf_counter() - started)
            return min(timings)

        small, large = best_of_three(2000), best_of_three(8000)

        assert large < 8 * small + 0.05, (small, large)

    def test_a_test_password_does_not_stretch_over_a_real_token(self) -> None:
        line = f'connect(password=pw, token="{_GITHUB_TOKEN}", user="admin")'

        assert "github_token" in _rules(line)

    @pytest.mark.parametrize("value", ["test", "password", "changeme", "secret", "admin"])
    def test_a_known_test_password_is_still_exempt(self, value: str) -> None:
        assert _rules(f'password = "{value}"') == []
        assert _rules('password = "hunter2"') == ["password_assignment"]


# ---------------------------------------------------------------------------
# scan_text: clean content
# ---------------------------------------------------------------------------


class TestCleanContent:
    def test_normal_code_no_findings(self) -> None:
        text = "def hello():\n    return 'world'\n"
        assert scan_text(text) == []

    def test_comments_without_secrets_no_findings(self) -> None:
        text = "# This module handles authentication\n# Use environment variables for secrets\n"
        assert scan_text(text) == []


# ---------------------------------------------------------------------------
# scan_text: finding properties
# ---------------------------------------------------------------------------


class TestFindingProperties:
    def test_finding_is_frozen_dataclass(self) -> None:
        findings = scan_text('api_key = "sk-proj-abcdef1234567890ABCD"')
        assert findings
        assert isinstance(findings[0], SecretFinding)

    def test_line_number_is_correct(self) -> None:
        text = "line1\nline2\napi_key = 'sk-proj-abcdef1234567890ABCD'"
        findings = scan_text(text)
        assert findings[0].line_number == 3

    def test_redacted_match_does_not_contain_raw_secret(self) -> None:
        text = 'token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"'
        findings = scan_text(text)
        gh = next(f for f in findings if f.rule == "github_token")
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in gh.redacted_match
        assert "***" in gh.redacted_match

    def test_same_rule_fires_for_each_distinct_match(self) -> None:
        """Regression for audit-044: the scanner used to dedup per rule_id,
        silently dropping the second secret of the same type. A rule must now
        fire once per distinct (rule, line, span) match."""
        text = "key1@real.com\nkey2@real.com\n"
        findings = scan_text(text)
        email_findings = [f for f in findings if f.rule == "email_address"]
        assert len(email_findings) == 2
        assert {f.line_number for f in email_findings} == {1, 2}

    def test_duplicate_identical_line_deduped(self) -> None:
        """Exact same span on the same line must not be reported twice -
        ``(rule, line_num, start, end)`` dedup still applies."""
        text = "alert@real.com"
        findings = scan_text(text)
        email_findings = [f for f in findings if f.rule == "email_address"]
        assert len(email_findings) == 1


# ---------------------------------------------------------------------------
# audit-044 regression: per-rule dedup must not swallow distinct secrets
# ---------------------------------------------------------------------------


class TestAudit044PerRuleDedup:
    """Regression tests for audit-044.

    Prior to the fix, ``_scan_line`` skipped any rule after its first hit
    (``if rule_label in seen_rules: continue``), so a file with two different
    AWS keys or two different credit-card numbers would report only one -
    scanner evasion. Dedup now keys on ``(rule, line_num, span)`` instead.
    """

    def test_two_distinct_aws_keys_both_reported(self) -> None:
        text = 'honeypot = "AKIAIOSFODNN7EXAMPLE"\nreal = "AKIAI44QH8DHBEXAMPLE"\n'
        findings = scan_text(text)
        aws = [f for f in findings if f.rule == "aws_access_key"]
        assert len(aws) == 2, f"expected 2 aws_access_key findings, got {len(aws)}: {aws}"
        assert {f.line_number for f in aws} == {1, 2}

    def test_two_distinct_credit_cards_both_reported(self) -> None:
        # Two different Luhn-valid numbers on separate lines.
        text = "card_a = '4111 1111 1111 1111'\ncard_b = '5500 0000 0000 0004'\n"
        findings = scan_text(text)
        cards = [f for f in findings if f.rule == "credit_card_number"]
        assert len(cards) == 2, f"expected 2 credit_card_number findings, got {len(cards)}: {cards}"
        assert {f.line_number for f in cards} == {1, 2}

    def test_two_distinct_github_tokens_both_reported(self) -> None:
        text = 'a = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\nb = "ghp_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"\n'
        findings = scan_text(text)
        toks = [f for f in findings if f.rule == "github_token"]
        assert len(toks) == 2
        assert {f.line_number for f in toks} == {1, 2}

    def test_two_distinct_aws_keys_on_same_line_both_reported(self) -> None:
        """Even on the same line, two distinct matches (different spans) fire."""
        text = 'pair = ("AKIAIOSFODNN7EXAMPLE", "AKIAI44QH8DHBEXAMPLE")'
        findings = scan_text(text)
        aws = [f for f in findings if f.rule == "aws_access_key"]
        assert len(aws) == 2

    def test_diff_with_two_distinct_aws_keys_both_reported(self) -> None:
        diff = (
            "--- a/config.py\n"
            "+++ b/config.py\n"
            "@@ -1,1 +1,3 @@\n"
            ' previous = "AKIAI44QH8DHBEXAMPLE"\n'
            '+honeypot = "AKIAIOSFODNN7EXAMPLE"\n'
            '+real = "AKIAI44QH8DHBPREVIEW"\n'
        )
        findings = scan_diff(diff)
        aws = [f for f in findings if f.rule == "aws_access_key"]
        assert len(aws) == 2


# ---------------------------------------------------------------------------
# scan_diff: only added lines
# ---------------------------------------------------------------------------


class TestScanDiff:
    def test_added_line_with_secret_detected(self) -> None:
        diff = (
            "--- a/config.py\n"
            "+++ b/config.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            '+API_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
            " other = 1\n"
        )
        findings = scan_diff(diff)
        assert any(f.rule == "aws_access_key" for f in findings)

    def test_removed_line_with_secret_not_detected(self) -> None:
        diff = '--- a/config.py\n+++ b/config.py\n@@ -1,3 +1,2 @@\n import os\n-API_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        findings = scan_diff(diff)
        assert not any(f.rule == "aws_access_key" for f in findings)

    def test_context_line_not_scanned(self) -> None:
        diff = '--- a/config.py\n+++ b/config.py\n@@ -1,3 +1,3 @@\n API_KEY = "AKIAIOSFODNN7EXAMPLE"\n+# new comment\n'
        findings = scan_diff(diff)
        assert not any(f.rule == "aws_access_key" for f in findings)

    def test_diff_header_plus_lines_skipped(self) -> None:
        diff = "+++ b/file_with_AKIAIOSFODNN7EXAMPLE.py\n+clean line\n"
        findings = scan_diff(diff)
        assert not any(f.rule == "aws_access_key" for f in findings)

    def test_clean_diff_no_findings(self) -> None:
        diff = "--- a/utils.py\n+++ b/utils.py\n@@ -1 +1,2 @@\n def foo(): pass\n+def bar(): pass\n"
        assert scan_diff(diff) == []


# ---------------------------------------------------------------------------
# format_findings
# ---------------------------------------------------------------------------


class TestFormatFindings:
    def test_no_findings_message(self) -> None:
        assert "No secrets or PII" in format_findings([])

    def test_findings_include_count(self) -> None:
        findings = scan_text('password = "hunter2"')
        output = format_findings(findings)
        assert "1 finding" in output

    def test_findings_include_severity(self) -> None:
        findings = scan_text('password = "hunter2"')
        output = format_findings(findings)
        assert "HIGH" in output

    def test_findings_include_rule(self) -> None:
        findings = scan_text('password = "hunter2"')
        output = format_findings(findings)
        assert "password_assignment" in output


# ---------------------------------------------------------------------------
# Quality gate integration (_run_pii_gate)
# ---------------------------------------------------------------------------


class TestPiiQualityGate:
    def test_clean_directory_passes(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text("def main():\n    return 42\n")
        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["src/"])
        result = _run_pii_gate(config, tmp_path)
        assert result.passed
        assert not result.blocked
        assert result.gate == "pii_scan"

    def test_secret_in_file_blocks(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        src = tmp_path / "src"
        src.mkdir()
        (src / "config.py").write_text('API_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["src/"])
        result = _run_pii_gate(config, tmp_path)
        assert not result.passed
        assert result.blocked
        assert "aws_access_key" in result.detail

    def test_pii_in_file_does_not_block(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        src = tmp_path / "src"
        src.mkdir()
        # email is medium severity - does not block, but is flagged
        (src / "utils.py").write_text("# Contact alice@realcompany.com\n")
        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["src/"])
        result = _run_pii_gate(config, tmp_path)
        assert result.passed
        assert not result.blocked

    def test_missing_path_ignored(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["nonexistent/"])
        result = _run_pii_gate(config, tmp_path)
        assert result.passed

    def test_binary_files_skipped(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        src = tmp_path / "src"
        src.mkdir()
        (src / "image.png").write_bytes(b"\x89PNG\r\n")
        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["src/"])
        result = _run_pii_gate(config, tmp_path)
        assert result.passed

    def test_ignore_paths_skip_fixture_dirs(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        (fixtures / "secrets.txt").write_text('api_key = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8")
        config = QualityGatesConfig(
            pii_scan=True,
            pii_scan_paths=["fixtures/"],
            pii_ignore_paths=["fixtures/*"],
        )
        result = _run_pii_gate(config, tmp_path)
        assert result.passed

    def test_pii_gate_in_run_quality_gates(self, tmp_path: Path) -> None:
        from bernstein.core.models import Complexity, Scope, Task
        from bernstein.core.quality_gates import QualityGatesConfig, run_quality_gates

        src = tmp_path / "src"
        src.mkdir()
        (src / "leak.py").write_text('SECRET = "AKIAIOSFODNN7EXAMPLE"\n')
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=False,
            pii_scan=True,
            pii_scan_paths=["src/"],
        )
        task = Task(
            id="T-pii-1",
            title="Test task",
            description="Test",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
        )
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        gate_names = [r.gate for r in result.gate_results]
        assert "pii_scan" in gate_names

    def test_pii_gate_disabled_skips(self, tmp_path: Path) -> None:
        from bernstein.core.models import Complexity, Scope, Task
        from bernstein.core.quality_gates import QualityGatesConfig, run_quality_gates

        src = tmp_path / "src"
        src.mkdir()
        (src / "leak.py").write_text('SECRET = "AKIAIOSFODNN7EXAMPLE"\n')
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=False,
            pii_scan=False,
        )
        task = Task(
            id="T-pii-2",
            title="Test task",
            description="Test",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
        )
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        gate_names = [r.gate for r in result.gate_results]
        assert "pii_scan" not in gate_names

    def test_pii_gate_scans_only_changed_files_in_runner_path(self, tmp_path: Path) -> None:
        from bernstein.core.models import Complexity, Scope, Task
        from bernstein.core.quality_gates import QualityGatesConfig, run_quality_gates

        src = tmp_path / "src"
        src.mkdir()
        (src / "clean.py").write_text("VALUE = 42\n", encoding="utf-8")
        (src / "leak.py").write_text('SECRET = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8")
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=False,
            pii_scan=True,
            pii_scan_paths=["src/"],
        )
        task = Task(
            id="T-pii-3",
            title="Test task",
            description="Test",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            owned_files=["src/clean.py"],
        )
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
