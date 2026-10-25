"""PII and secret detection gate for agent output.

Scans code diffs and file contents produced by agents before merge to catch
leaked secrets, API keys, passwords, and PII.  Designed to run as a quality
gate - returns structured findings that hard-block merge when secrets are found.

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

from bernstein.core.security.luhn import luhn_check

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
        block_merge: True when this finding must block the ``pii_scan`` gate:
            every ``"high"`` secret, plus the regulated-data rules in
            ``BLOCKING_PII_RULES``, whatever their severity.
    """

    rule: str
    severity: str
    line_number: int
    redacted_match: str
    description: str
    block_merge: bool = False


#: PII rules that block although they are graded ``"medium"``: a US Social
#: Security number or a payment card number in a diff is a compliance incident,
#: not a quality note. The ``pii_scan`` gate keyed its verdict on
#: ``severity == "high"`` alone, so both passed while a private key blocked
#: (#6191). Email addresses and phone numbers stay warnings.
BLOCKING_PII_RULES: frozenset[str] = frozenset({"ssn", "credit_card_number"})


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

# Known benign values (example domains, test addresses, placeholders, local
# hosts). A match is exempt only when it overlaps one of these, i.e. when the
# benign marker is part of the flagged value: see :func:`_benign_mask`.
_ALLOWLIST_PATTERNS: list[re.Pattern[str]] = [
    # Anchored at both ends, so a real host that merely contains a marker
    # (``counterexample.com``, ``db.myexample.org``, ``example.com.au``) is not
    # exempt; a subdomain (``api.example.com``) or a sentence-final ``.`` is.
    re.compile(r"(?i)(?<![\w\-])example\.(?:com|org|net)(?![\w\-]|\.[\w\-])"),
    # Anchored to the start of the local part, so ``sysadmin@`` is not read as
    # ``admin@``. ``:`` is excluded too, because a URL's password sits right
    # after one: neither a ``latest@`` password nor a password that *is* a
    # marker (``svc:admin@``) is read as a benign address.
    re.compile(r"(?i)(?<![\w.%+\-:])(?:test|user|admin|noreply|no-reply)@"),
    # Whole names, bounded exactly like the hosts below: ``-`` and ``_`` join
    # words and a ``.`` before another label continues a host name, so a
    # ``/placeholders`` database, a ``placeholder-proxy`` host, an
    # ``app-changeme-2`` database or a ``db.changeme.prod.internal`` host is a
    # real name that contains a marker, not a marker. (A connection-string
    # match covers the whole URL, so an exempt word anywhere inside it would
    # exempt the credential.) A fixture value that *starts* with
    # ``placeholder`` is exempt through the value prefix. ``your_api_key`` is
    # a whole marker here, not a prefix inside a host or database token.
    # Fixture values such as ``"your_api_key_here"`` instead use the whole-value
    # prefix pattern below, including with operator-configured prefixes.
    # ``xxxx`` is a masking run and stays loose.
    re.compile(
        r"(?i)(?<![\w\-])(?:placeholder|changeme)(?![\w\-]|\.[\w\-])"
        r"|(?<![\w\-.])your[-_]?api[-_]?key(?![\w\-]|\.[\w\-])"
        r"|xxxx"
    ),
    # Anchored at both ends like the example domains, so ``localhost-proxy``,
    # ``app_localhost``, ``localhost.prod.internal``, ``10.0.0.0`` (which
    # contains ``0.0.0.0``) and ``127.0.0.10`` are real hosts, not markers.
    re.compile(r"(?i)(?<![\w\-])(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?![\w\-]|\.[\w\-])"),
    # Only the assignment itself, so the span cannot stretch over a real secret
    # sitting between ``password`` and some later test-looking value.
    re.compile(r"(?i)password\s*=\s*['\"](?:test|password|changeme|secret|admin)['\"]"),
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


def _allowlist_prefix_pattern(allowlist_prefixes: list[str] | None) -> re.Pattern[str]:
    """Return the pattern for a value that starts with a fake/test placeholder prefix."""
    prefixes = allowlist_prefixes or list(_DEFAULT_ALLOWLIST_PREFIXES)
    # This built-in marker already applies independently of operator prefixes.
    # Keep its optional separator spellings, but scope suffixes to a value.
    prefix_pattern = r"your[-_]?api[-_]?key|" + "|".join(re.escape(prefix) for prefix in prefixes)
    return re.compile(
        rf"""(?ix)
        (?:["']|=|:)\s*
        (?:{prefix_pattern})
        (?:[:\-\w./]*)?
        (?:["']|$)
        """
    )


def _has_mixed_case_and_digits(text: str) -> bool:
    """Return True if text contains uppercase, lowercase, and digit characters."""
    return any(c.isupper() for c in text) and any(c.islower() for c in text) and any(c.isdigit() for c in text)


def _looks_like_credit_card(match_text: str) -> bool:
    """Return True if a candidate number passes a Luhn checksum.

    Both length bounds are this call site's own: the rule matches a labelled
    13-19 digit run, so it needs each end. Only the checksum is shared.

    The forward walk this replaced doubled ``index % 2 == len(digits) % 2``,
    which is the same rule as the shared helper's reverse walk doubling the
    odd indices - forward index ``i`` is reverse index ``n-1-i``, and
    ``n-1-i`` is odd exactly when ``i % 2 == n % 2``. Spelling it once removes
    the need to re-derive that in order to compare the two.
    """
    digits = "".join(char for char in match_text if char.isdigit())
    if not (13 <= len(digits) <= 19):
        return False
    return luhn_check(digits)


def _benign_mask(line: str, allowlist_prefixes: list[str] | None) -> bytearray:
    """Return a per-character mask of *line*: ``1`` where the allowlist marks a benign value.

    A match is suppressed only when it overlaps a marked character -- when the
    benign marker is part of the flagged value (``"postgres://...@localhost"``,
    ``user@example.com``, ``"TEST_..."``). Applied to the whole line, a
    ``user@`` address, a ``# placeholder`` comment or a ``localhost`` note
    anywhere on it would hide a real token, SSN or card number beside it.

    Built once per line, so checking a match costs its own length rather than
    one comparison per benign span: a minified bundle or a source map is one
    long line dense with both, and the gate runs with no timeout.
    """
    mask = bytearray(len(line))
    matches = [match for pattern in _ALLOWLIST_PATTERNS for match in pattern.finditer(line)]
    matches.extend(_allowlist_prefix_pattern(allowlist_prefixes).finditer(line))
    for match in matches:
        start, end = match.span()
        mask[start:end] = b"\x01" * (end - start)
    return mask


def _overlaps(span: tuple[int, int], mask: bytearray) -> bool:
    """True when *span* covers at least one character *mask* marks benign."""
    start, end = span
    return mask.find(1, start, end) != -1


def _passes_rule_filters(
    rule_label: str,
    m: re.Match[str],
) -> bool:
    """Return True if a match survives rule-specific post-filters.

    Some rules (credit cards, high-entropy assignments) have additional checks
    beyond the regex itself. This is split from the matching step so callers can
    iterate multiple matches per line and still apply the filters per-match.
    """
    if rule_label == "credit_card_number" and not _looks_like_credit_card(m.group(0)):
        return False
    if rule_label == "high_entropy_assignment":
        value = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
        if not _has_mixed_case_and_digits(value):
            return False
    return True


def _build_redacted_excerpt(line: str, m: re.Match[str]) -> str:
    """Build a redacted excerpt around a regex match - never stores raw secrets."""
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
    seen_matches: set[tuple[str, int, int, int]],
    allowlist_prefixes: list[str] | None,
    findings: list[SecretFinding],
) -> None:
    """Scan a single line against all secret rules, appending any findings.

    Deduplication keys on ``(rule, line_num, match_start, match_end)`` so that
    multiple distinct secrets of the same rule (e.g. two different AWS keys) are
    each reported, while exact duplicates (same rule hitting the same span -
    possible if scans ever overlap) are not counted twice. This prevents the
    scanner-evasion bug where a single rule's dedup would silently drop the
    second/third secret of the same type.

    A match whose span overlaps a benign value (see :func:`_benign_mask`) is
    skipped; every other match on the line is still reported.
    """
    benign = _benign_mask(line, allowlist_prefixes)
    for rule_label, pattern, severity, description in _SECRET_RULES:
        for m in pattern.finditer(line):
            if _overlaps(m.span(), benign) or not _passes_rule_filters(rule_label, m):
                continue
            match_key = (rule_label, line_num, m.start(), m.end())
            if match_key in seen_matches:
                continue
            redacted = _build_redacted_excerpt(line, m)
            findings.append(
                SecretFinding(
                    rule=rule_label,
                    severity=severity,
                    line_number=line_num,
                    redacted_match=redacted,
                    description=description,
                    block_merge=severity == "high" or rule_label in BLOCKING_PII_RULES,
                )
            )
            seen_matches.add(match_key)


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
    seen_matches: set[tuple[str, int, int, int]] = set()

    for line_num, line in enumerate(text.splitlines(), start=1):
        _scan_line(line, line_num, seen_matches, allowlist_prefixes, findings)

    return findings


def scan_diff(
    diff_text: str,
    *,
    allowlist_prefixes: list[str] | None = None,
) -> list[SecretFinding]:
    """Scan a unified diff for secrets and PII in added lines only.

    Only lines starting with ``+`` (additions) are checked.  Removed lines
    (``-``) and context lines are ignored - we only care about new secrets
    being introduced.

    Args:
        diff_text: Unified diff output (e.g. from ``git diff``).

    Returns:
        List of SecretFinding objects found in added lines.
    """
    findings: list[SecretFinding] = []
    seen_matches: set[tuple[str, int, int, int]] = set()
    diff_line_num = 0

    for raw_line in diff_text.splitlines():
        diff_line_num += 1

        # Only scan added lines (skip diff headers, context, removals)
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue

        line = raw_line[1:]  # strip the leading "+"
        _scan_line(line, diff_line_num, seen_matches, allowlist_prefixes, findings)

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
        lines.append(f"  [{f.severity.upper()}] {f.rule} (line {f.line_number}): {f.description} - {f.redacted_match}")
    return "\n".join(lines)
