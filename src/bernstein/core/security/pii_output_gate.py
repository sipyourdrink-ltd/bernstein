"""PII and secret detection gate for agent output.

Scans code diffs and file contents produced by agents before merge to catch
leaked secrets, API keys, passwords, and PII.  Designed to run as a quality
gate — returns structured findings that hard-block merge when secrets are found.

Regex-only (no network calls, no LLM).  Patterns cover:
- Cloud provider keys (AWS, GCP, Azure)
- Platform tokens (GitHub, Slack, Stripe, generic JWT)
- Private keys (PEM)
- Hardcoded passwords / connection strings
- PII (emails, phone numbers, SSNs)

Usage::

    from bernstein.core.security.pii_output_gate import scan_diff, scan_text

    findings = scan_diff(diff_text)
    if findings:
        # block merge
        ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretFinding:
    """A single secret or PII match found in agent output.

    Attributes:
        rule: Short label identifying the detection rule (e.g. ``"aws_access_key"``).
        severity: ``"high"`` for secrets/keys, ``"medium"`` for PII.
        line_number: 1-based line number where the match was found, or 0 if unknown.
        redacted_match: Up to 60 chars around the match with the secret replaced
            by ``***``.  Raw secrets are never stored.
        description: Human-readable explanation of the finding.
    """

    rule: str
    severity: str
    line_number: int
    redacted_match: str
    description: str


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

# Each rule: (label, compiled_pattern, severity, description)
# Patterns are designed for low false-positive rates on code diffs.

_SECRET_RULES: list[tuple[str, re.Pattern[str], str, str]] = [
    # --- Cloud provider keys ---
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "high",
        "AWS access key ID",
    ),
    (
        "aws_secret_key",
        re.compile(r"""(?i)(?:aws_secret_access_key|aws_secret|secret_key)\s*[=:]\s*["']?([a-z\d/+=]{40})["']?"""),
        "high",
        "AWS secret access key",
    ),
    (
        "gcp_service_account",
        re.compile(r'(?i)"type"\s*:\s*"service_account"'),
        "high",
        "GCP service account JSON key",
    ),
    # --- Platform tokens ---
    (
        "github_token",
        re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_\w{36,255}\b"),
        "high",
        "GitHub personal access token or app token",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[bporas]-[A-Za-z\d\-]{10,255}\b"),
        "high",
        "Slack API token",
    ),
    (
        "stripe_key",
        re.compile(r"\b[sr]k_(live|test)_[A-Za-z\d]{16,255}\b"),
        "high",
        "Stripe API key",
    ),
    # --- Generic secrets ---
    (
        "private_key",
        re.compile(r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"),
        "high",
        "Private key (PEM format)",
    ),
    (
        "generic_api_key",
        re.compile(r"""(?i)(?:api_?key|api_secret|(?:access|auth|secret)_token)\s*[=:]\s*["']([\w\-/.+=]{16,})["']"""),
        "high",
        "Generic API key or secret token assignment",
    ),
    (
        "high_entropy_assignment",
        re.compile(
            r"""(?ix)
            (?:secret|token|key|credential|password)[a-z\d_\-]*
            \s*[=:]\s*["']
            ([\w+/=\-]{24,})
            ["']
            """
        ),
        "high",
        "High-entropy secret-like assignment",
    ),
    (
        "password_assignment",
        re.compile(r"""(?i)(?:password|passwd|pwd)\s*[=:]\s*["']([^"'\s]{4,})["']"""),
        "high",
        "Hardcoded password assignment",
    ),
    (
        "connection_string",
        re.compile(r"""(?i)(?:postgres|mysql|mongodb|redis|amqp|mssql)://[^\s"']{10,}"""),
        "high",
        "Database or service connection string with credentials",
    ),
    (
        "bearer_token",
        re.compile(r"""(?i)(?:authorization|bearer)\s*[=:]\s*["']?Bearer\s+[\w\-/.+=]{20,}["']?"""),
        "high",
        "Bearer authentication token",
    ),
    (
        "jwt_token",
        re.compile(r"\beyJ[\w-]{20,}\.[\w-]{20,}\.[\w-]{20,}\b"),
        "high",
        "JSON Web Token",
    ),
    # --- PII ---
    (
        "email_address",
        re.compile(r"\b[\w.%+\-]+@[A-Za-z\d.\-]+\.[A-Za-z]{2,}\b"),
        "medium",
        "Email address",
    ),
    (
        "phone_number",
        re.compile(r"\b(?:\+\d{1,3}[\s\-])?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
        "medium",
        "Phone number",
    ),
    (
        "ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "medium",
        "US Social Security Number",
    ),
    (
        "credit_card_number",
        re.compile(r"\b(?:\d[ -]*){13,19}\b"),
        "medium",
        "Credit card number",
    ),
]

# Lines matching these patterns are whitelisted (example values, test fixtures).
_ALLOWLIST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)example\.com|example\.org|example\.net"),
    re.compile(r"(?i)test@|user@|admin@|noreply@|no-reply@"),
    re.compile(r"(?i)placeholder|changeme|your[-_]?api[-_]?key|xxxx"),
    re.compile(r"(?i)localhost|127\.0\.0\.1|0\.0\.0\.0"),
    re.compile(r"(?i)password[^\n]{0,200}=[^\n]{0,200}['\"](?:test|password|changeme|secret|admin)['\"]"),
]
_DEFAULT_ALLOWLIST_PREFIXES: tuple[str, ...] = ("FAKE", "TEST", "EXAMPLE", "DUMMY", "PLACEHOLDER", "LOCALHOST")


# ---------------------------------------------------------------------------
# Scanning functions
# ---------------------------------------------------------------------------


def _matches_ignore_path(path: str | None, ignore_paths: list[str] | None) -> bool:
    """Return True when *path* matches any configured ignore glob."""
    if path is None or not ignore_paths:
        return False
    normalized = path.replace("\\", "/")
    return any(fnmatch(normalized, pattern) or normalized.startswith(pattern.rstrip("/")) for pattern in ignore_paths)


def _contains_allowlist_prefix(line: str, allowlist_prefixes: list[str] | None) -> bool:
    """Return True when the line contains a known fake/test placeholder prefix."""
    prefixes = allowlist_prefixes or list(_DEFAULT_ALLOWLIST_PREFIXES)
    prefix_pattern = "|".join(re.escape(prefix) for prefix in prefixes)
    return bool(
        re.search(
            rf"""(?ix)
            (?:["']|=|:)\s*
            (?:{prefix_pattern})
            (?:[:\-\w./]*)?
            (?:["']|$)
            """,
            line,
        )
    )


def _has_mixed_case_and_digits(text: str) -> bool:
    """Return True if text contains uppercase, lowercase, and digit characters."""
    return any(c.isupper() for c in text) and any(c.islower() for c in text) and any(c.isdigit() for c in text)


def _looks_like_credit_card(match_text: str) -> bool:
    """Return True if a candidate number passes a Luhn checksum."""
    digits = [int(char) for char in match_text if char.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        value = digit
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value
    return checksum % 10 == 0


def _is_allowlisted(line: str, allowlist_prefixes: list[str] | None = None) -> bool:
    """Return True if the line matches a known false-positive pattern."""
    return any(p.search(line) for p in _ALLOWLIST_PATTERNS) or _contains_allowlist_prefix(line, allowlist_prefixes)


def _check_rule_match(
    rule_label: str,
    pattern: re.Pattern[str],
    line: str,
) -> re.Match[str] | None:
    """Check a single rule against a line, applying rule-specific filters.

    Returns the match object if the rule fires, or None if it does not apply.
    """
    m = pattern.search(line)
    if not m:
        return None
    if rule_label == "credit_card_number" and not _looks_like_credit_card(m.group(0)):
        return None
    if rule_label == "high_entropy_assignment":
        value = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
        if not _has_mixed_case_and_digits(value):
            return None
    return m


def _build_redacted_excerpt(line: str, m: re.Match[str]) -> str:
    """Build a redacted excerpt around a regex match — never stores raw secrets."""
    start = max(0, m.start() - 10)
    end = min(len(line), m.end() + 10)
    raw_excerpt = line[start:end]
    rel_start = m.start() - start
    rel_end = m.end() - start
    redacted = raw_excerpt[:rel_start] + "***" + raw_excerpt[rel_end:]
    if len(redacted) > 60:
        redacted = redacted[:57] + "..."
    return redacted


def _scan_line(
    line: str,
    line_num: int,
    seen_rules: set[str],
    allowlist_prefixes: list[str] | None,
    findings: list[SecretFinding],
) -> None:
    """Scan a single line against all secret rules, appending any findings."""
    if _is_allowlisted(line, allowlist_prefixes):
        return
    for rule_label, pattern, severity, description in _SECRET_RULES:
        if rule_label in seen_rules:
            continue
        m = _check_rule_match(rule_label, pattern, line)
        if m is None:
            continue
        redacted = _build_redacted_excerpt(line, m)
        findings.append(
            SecretFinding(
                rule=rule_label,
                severity=severity,
                line_number=line_num,
                redacted_match=redacted,
                description=description,
            )
        )
        seen_rules.add(rule_label)


def scan_text(
    text: str,
    *,
    path: str | None = None,
    ignore_paths: list[str] | None = None,
    allowlist_prefixes: list[str] | None = None,
) -> list[SecretFinding]:
    """Scan arbitrary text for secrets and PII.

    Args:
        text: Content to scan (file content, commit message, etc.).
        path: Optional source path. Used for ignore-path filtering.
        ignore_paths: Optional glob-like path ignore list.
        allowlist_prefixes: Optional fake/test/example prefixes.

    Returns:
        List of SecretFinding objects, one per detection.
    """
    if _matches_ignore_path(path, ignore_paths):
        return []

    findings: list[SecretFinding] = []
    seen_rules: set[str] = set()

    for line_num, line in enumerate(text.splitlines(), start=1):
        _scan_line(line, line_num, seen_rules, allowlist_prefixes, findings)

    return findings


def scan_diff(
    diff_text: str,
    *,
    allowlist_prefixes: list[str] | None = None,
) -> list[SecretFinding]:
    """Scan a unified diff for secrets and PII in added lines only.

    Only lines starting with ``+`` (additions) are checked.  Removed lines
    (``-``) and context lines are ignored — we only care about new secrets
    being introduced.

    Args:
        diff_text: Unified diff output (e.g. from ``git diff``).

    Returns:
        List of SecretFinding objects found in added lines.
    """
    findings: list[SecretFinding] = []
    seen_rules: set[str] = set()
    diff_line_num = 0

    for raw_line in diff_text.splitlines():
        diff_line_num += 1

        # Only scan added lines (skip diff headers, context, removals)
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue

        line = raw_line[1:]  # strip the leading "+"
        _scan_line(line, diff_line_num, seen_rules, allowlist_prefixes, findings)

    return findings


def format_findings(findings: list[SecretFinding]) -> str:
    """Format findings into a human-readable report for quality gate output.

    Args:
        findings: List of findings from ``scan_text`` or ``scan_diff``.

    Returns:
        Multi-line string summarising all findings, suitable for gate detail.
    """
    if not findings:
        return "No secrets or PII detected."

    lines = [f"PII/Secret gate: {len(findings)} finding(s)"]
    for f in findings:
        lines.append(f"  [{f.severity.upper()}] {f.rule} (line {f.line_number}): {f.description} — {f.redacted_match}")
    return "\n".join(lines)
