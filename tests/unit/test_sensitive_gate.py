"""Tests for the compaction sensitive-content gate.

Covers the acceptance criteria for the deny gate over compaction input:

1. Pure and deterministic verdicts over a fixture corpus.
2. Credential-shaped spans are redacted with typed placeholders or the
   whole compaction is refused when the span is not safely delimitable.
3. Every redaction, refusal, and allowlist suppression is recorded in the
   HMAC audit chain (span hash only, never content) and the chain verifies.
4. Allowlist entries suppress rules and the suppression is audit-logged.
"""

from __future__ import annotations

import hashlib

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_COMPACTION_SENSITIVE_GATE,
    AuditChainStore,
)
from bernstein.core.tokens.sensitive_gate import (
    ENTROPY_MIN_BITS_PER_CHAR,
    ENTROPY_MIN_VALUE_LENGTH,
    GateConfig,
    GateDecision,
    emit_gate_audit,
    scan_for_sensitive_content,
    shannon_entropy,
)

# ---------------------------------------------------------------------------
# Fixture corpus - positives
# ---------------------------------------------------------------------------

PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA7bq7wmiCPDlKcQXn3pg9qF4mUJVjcMZLxQ2R8aTnW1sBvKd0\n"
    "Zx9YpH3mN6cRqLtE5uWvGf8kAoJdSyXbCePiMnrTz4hQjV2wKgUmBaN7lDxOsF1e\n"
    "-----END RSA PRIVATE KEY-----"
)

ID_RSA_CONTENT = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAABFwAAAAdz\n"
    "c2gtcnNhAAAAAwEAAQAAAQEAy8kVpTfW2nQxJmH0dGeLcU4bRaZoP7iM9sBvE1uY\n"
    "-----END OPENSSH PRIVATE KEY-----"
)

AKIA_KEY = "AKIAIOSFODNN7EXAMPLE"

GHP_TOKEN = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

ANTHROPIC_KEY = "sk-ant-api03-Zx9YpH3mN6cRqLtE5uWvGf8kAoJdSyXbCePiMnrTz4hQ"

ENV_FILE_DUMP = (
    "$ cat .env\n"
    "DATABASE_URL=postgres://user:pass@host/db\n"
    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCyEXAMPLEKEY\n"
)

ENTROPY_ASSIGNMENT = 'api-key: "9fXq2LmZv8RbT4wKpN6yHs3JdA7eUcGh"'

POSITIVES = [
    pytest.param(PEM_BLOCK, id="pem-block"),
    pytest.param(ID_RSA_CONTENT, id="id-rsa-content"),
    pytest.param(f"found key {AKIA_KEY} in config", id="akia-key"),
    pytest.param(f"token is {GHP_TOKEN}", id="ghp-token"),
    pytest.param(f"use {ANTHROPIC_KEY} for the call", id="anthropic-key"),
    pytest.param(ENV_FILE_DUMP, id="env-file-dump"),
    pytest.param(ENTROPY_ASSIGNMENT, id="entropy-assignment"),
]

# ---------------------------------------------------------------------------
# Fixture corpus - negatives
# ---------------------------------------------------------------------------

UUID_TEXT = 'request_id = "550e8400-e29b-41d4-a716-446655440000"'

SHA256_TEXT = 'digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"'

BASE64_IMAGE = "![img](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk)"

NORMAL_CODE = (
    "def resolve(env_name: str) -> str:\n"
    '    """Look up a value from the environment."""\n'
    "    return os.environ.get(env_name, '')\n"
)

HEX_DIGEST_WITH_SENSITIVE_NAME = 'token_digest = "9b74c9897bac770ffc029102a200c5de1bc1e9cc292e1cadd07f4a8256a2b0ac"'

NEGATIVES = [
    pytest.param(UUID_TEXT, id="uuid"),
    pytest.param(SHA256_TEXT, id="sha256-hash"),
    pytest.param(BASE64_IMAGE, id="base64-image"),
    pytest.param(NORMAL_CODE, id="normal-code"),
    pytest.param(HEX_DIGEST_WITH_SENSITIVE_NAME, id="hex-digest-sensitive-name"),
    pytest.param("plain prose about deployment pipelines", id="prose"),
]


# ---------------------------------------------------------------------------
# Determinism and verdicts (acceptance criterion 1)
# ---------------------------------------------------------------------------


class TestVerdicts:
    @pytest.mark.parametrize("text", POSITIVES)
    def test_positive_is_flagged(self, text: str) -> None:
        decision = scan_for_sensitive_content(text)
        assert decision.action in ("redacted", "refused")
        assert decision.findings

    @pytest.mark.parametrize("text", NEGATIVES)
    def test_negative_is_allowed(self, text: str) -> None:
        decision = scan_for_sensitive_content(text)
        assert decision.action == "allow"
        assert decision.text == text
        assert not decision.findings

    @pytest.mark.parametrize("text", POSITIVES + NEGATIVES)
    def test_deterministic_across_runs(self, text: str) -> None:
        first = scan_for_sensitive_content(text)
        second = scan_for_sensitive_content(text)
        assert first == second

    def test_empty_input_allows(self) -> None:
        decision = scan_for_sensitive_content("")
        assert decision.action == "allow"
        assert decision.text == ""


# ---------------------------------------------------------------------------
# Redaction behaviour
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_akia_key_redacted_with_typed_placeholder(self) -> None:
        text = f"before {AKIA_KEY} after"
        decision = scan_for_sensitive_content(text)
        assert decision.action == "redacted"
        assert AKIA_KEY not in decision.text
        span_hash = hashlib.sha256(AKIA_KEY.encode("utf-8")).hexdigest()
        assert f"[REDACTED:content.aws-access-key:{span_hash[:8]}]" in decision.text
        assert "before" in decision.text
        assert "after" in decision.text

    def test_complete_pem_block_redacted(self) -> None:
        text = f"file contents:\n{PEM_BLOCK}\ndone"
        decision = scan_for_sensitive_content(text)
        assert decision.action == "redacted"
        assert "PRIVATE KEY" not in decision.text.replace("REDACTED", "")
        assert "MIIEowIBAAKCAQEA" not in decision.text
        assert "[REDACTED:content.pem-private-key:" in decision.text

    def test_ghp_and_anthropic_tokens_redacted(self) -> None:
        text = f"a={GHP_TOKEN} b={ANTHROPIC_KEY}"
        decision = scan_for_sensitive_content(text)
        assert decision.action == "redacted"
        assert GHP_TOKEN not in decision.text
        assert ANTHROPIC_KEY not in decision.text
        assert "[REDACTED:content.github-token:" in decision.text
        assert "[REDACTED:content.anthropic-key:" in decision.text

    def test_finding_carries_span_hash_not_content(self) -> None:
        decision = scan_for_sensitive_content(f"x {AKIA_KEY} y")
        finding = decision.findings[0]
        assert finding.span_hash == hashlib.sha256(AKIA_KEY.encode("utf-8")).hexdigest()
        assert AKIA_KEY not in repr(decision.findings)


# ---------------------------------------------------------------------------
# Refusal behaviour (non-delimitable spans)
# ---------------------------------------------------------------------------


class TestRefusal:
    def test_env_file_dump_refuses_compaction(self) -> None:
        decision = scan_for_sensitive_content(ENV_FILE_DUMP)
        assert decision.action == "refused"
        assert decision.text == ENV_FILE_DUMP  # untouched: caller must not forward it

    def test_unterminated_pem_refuses(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7bq7wmiCPDlK"
        decision = scan_for_sensitive_content(text)
        assert decision.action == "refused"

    @pytest.mark.parametrize(
        "token",
        [
            "id_rsa",
            "secrets.pem",
            "~/.ssh/known_hosts",
            "~/.aws/credentials",
            "~/.gnupg/secring.gpg",
            "~/.kube/config",
        ],
    )
    def test_path_shaped_tokens_refuse(self, token: str) -> None:
        decision = scan_for_sensitive_content(f"reading {token} now")
        assert decision.action == "refused"

    def test_public_key_path_not_refused(self) -> None:
        decision = scan_for_sensitive_content("copy id_rsa.pub to the server")
        assert decision.action == "allow"


# ---------------------------------------------------------------------------
# Entropy heuristic (conservative: prefer false negatives)
# ---------------------------------------------------------------------------


class TestEntropyHeuristic:
    def test_threshold_is_documented_and_conservative(self) -> None:
        # Strictly above 4.0 bits/char: a pure-hex value (16 symbols) can
        # never exceed log2(16) == 4.0, so digests are structurally excluded.
        assert ENTROPY_MIN_BITS_PER_CHAR == 4.0
        assert ENTROPY_MIN_VALUE_LENGTH == 24

    def test_shannon_entropy_of_uniform_hex_is_at_most_four(self) -> None:
        assert shannon_entropy("0123456789abcdef" * 4) <= 4.0

    def test_normalized_name_matching(self) -> None:
        # api-key normalizes to apikey and matches the sensitive-name set.
        decision = scan_for_sensitive_content(ENTROPY_ASSIGNMENT)
        assert decision.action == "redacted"
        assert decision.findings[0].rule_id == "content.entropy-assignment"

    def test_non_sensitive_name_with_random_value_is_allowed(self) -> None:
        decision = scan_for_sensitive_content('cache_id = "9fXq2LmZv8RbT4wKpN6yHs3JdA7eUcGh"')
        assert decision.action == "allow"

    def test_sensitive_name_with_low_entropy_value_is_allowed(self) -> None:
        decision = scan_for_sensitive_content('session-token = "aaaaaaaaaaaaaaaaaaaaaaaaaaaa"')
        assert decision.action == "allow"

    def test_sensitive_name_with_short_value_is_allowed(self) -> None:
        decision = scan_for_sensitive_content('api-key = "9fXq2LmZv8RbT4w"')
        assert decision.action == "allow"


# ---------------------------------------------------------------------------
# Config: extra deny rules and allowlist (acceptance criterion 4)
# ---------------------------------------------------------------------------


class TestConfig:
    def test_disabled_gate_allows_everything(self) -> None:
        config = GateConfig(enabled=False)
        decision = scan_for_sensitive_content(PEM_BLOCK, config=config)
        assert decision.action == "allow"

    def test_extra_deny_pattern_redacts(self) -> None:
        config = GateConfig(extra_deny=("INTERNAL-[0-9]{6}",))
        decision = scan_for_sensitive_content("ref INTERNAL-123456 here", config=config)
        assert decision.action == "redacted"
        assert "INTERNAL-123456" not in decision.text
        assert decision.findings[0].rule_id.startswith("extra.")

    def test_invalid_extra_deny_pattern_is_skipped(self) -> None:
        config = GateConfig(extra_deny=("[unclosed",))
        decision = scan_for_sensitive_content("harmless text", config=config)
        assert decision.action == "allow"

    def test_allowlist_by_rule_id_suppresses(self) -> None:
        config = GateConfig(allow=("content.aws-access-key",))
        decision = scan_for_sensitive_content(f"x {AKIA_KEY} y", config=config)
        assert decision.action == "allow"
        assert decision.text == f"x {AKIA_KEY} y"
        assert not decision.findings
        assert decision.suppressed
        assert decision.suppressed[0].rule_id == "content.aws-access-key"

    def test_allowlist_by_rule_and_span_hash_suppresses(self) -> None:
        span_hash8 = hashlib.sha256(AKIA_KEY.encode("utf-8")).hexdigest()[:8]
        config = GateConfig(allow=(f"content.aws-access-key:{span_hash8}",))
        decision = scan_for_sensitive_content(f"x {AKIA_KEY} y", config=config)
        assert decision.action == "allow"
        assert decision.suppressed

    def test_allowlist_span_hash_does_not_suppress_other_spans(self) -> None:
        config = GateConfig(allow=("content.aws-access-key:00000000",))
        decision = scan_for_sensitive_content(f"x {AKIA_KEY} y", config=config)
        assert decision.action == "redacted"
        assert not decision.suppressed

    def test_from_defaults_reads_compaction_defaults(self) -> None:
        from bernstein.core import defaults

        config = GateConfig.from_defaults()
        assert config.enabled == defaults.COMPACTION.sensitive_gate_enabled
        assert config.extra_deny == defaults.COMPACTION.sensitive_gate_extra_deny
        assert config.allow == defaults.COMPACTION.sensitive_gate_allow


# ---------------------------------------------------------------------------
# Audit-chain emission (acceptance criterion 3)
# ---------------------------------------------------------------------------


class TestAuditEmission:
    def _chain(self, tmp_path: object) -> AuditChainStore:
        from pathlib import Path

        return AuditChainStore(Path(str(tmp_path)) / "audit", key=b"k" * 32)

    def test_redaction_emits_verifiable_event(self, tmp_path: object) -> None:
        chain = self._chain(tmp_path)
        decision = scan_for_sensitive_content(f"x {AKIA_KEY} y")
        emit_gate_audit(decision, chain=chain, task_id="task-1", session_id="s-1")

        events = chain.query(event_type=EVENT_COMPACTION_SENSITIVE_GATE)
        assert len(events) == 1
        details = events[0].details
        assert details["task_id"] == "task-1"
        assert details["rule_id"] == "content.aws-access-key"
        assert details["action"] == "redacted"
        assert details["span_hash"] == hashlib.sha256(AKIA_KEY.encode("utf-8")).hexdigest()
        assert AKIA_KEY not in str(details)
        ok, errors = chain.verify()
        assert ok, errors

    def test_refusal_emits_event_per_finding(self, tmp_path: object) -> None:
        chain = self._chain(tmp_path)
        decision = scan_for_sensitive_content(ENV_FILE_DUMP)
        assert decision.action == "refused"
        emit_gate_audit(decision, chain=chain, task_id="task-2", session_id="s-2")

        events = chain.query(event_type=EVENT_COMPACTION_SENSITIVE_GATE)
        assert events
        assert all(e.details["action"] == "refused" for e in events)
        assert all("wJalrXUtnFEMI" not in str(e.details) for e in events)
        ok, errors = chain.verify()
        assert ok, errors

    def test_suppression_is_audit_logged(self, tmp_path: object) -> None:
        chain = self._chain(tmp_path)
        config = GateConfig(allow=("content.aws-access-key",))
        decision = scan_for_sensitive_content(f"x {AKIA_KEY} y", config=config)
        emit_gate_audit(decision, chain=chain, task_id="task-3", session_id="s-3")

        events = chain.query(event_type=EVENT_COMPACTION_SENSITIVE_GATE)
        assert len(events) == 1
        assert events[0].details["action"] == "suppressed"
        assert events[0].details["rule_id"] == "content.aws-access-key"
        ok, errors = chain.verify()
        assert ok, errors

    def test_allow_decision_emits_nothing(self, tmp_path: object) -> None:
        chain = self._chain(tmp_path)
        decision = scan_for_sensitive_content("plain text")
        emit_gate_audit(decision, chain=chain, task_id="task-4", session_id="s-4")
        assert not chain.query(event_type=EVENT_COMPACTION_SENSITIVE_GATE)


# ---------------------------------------------------------------------------
# Decision dataclass surface
# ---------------------------------------------------------------------------


class TestGateDecision:
    def test_decision_is_frozen(self) -> None:
        decision = scan_for_sensitive_content("plain")
        assert isinstance(decision, GateDecision)
        with pytest.raises(AttributeError):
            decision.action = "refused"  # type: ignore[misc]
