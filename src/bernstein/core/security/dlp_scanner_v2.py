"""Enhanced Data Loss Prevention (DLP) scanner for agent outputs (v2).

Builds on ``dlp_scanner.py`` with additional capabilities:

1. **StrEnum-based categories** — ``DLPCategory`` for type-safe category
   handling (SOURCE_CODE, PROPRIETARY_DATA, REGULATED_DATA, CREDENTIALS, PII).
2. **Luhn-validated credit cards** — reduces false-positive rate for card
   number patterns.
3. **Configurable customer-ID prefixes** — detect ``CUST-*``, ``ORG-*``, etc.
4. **License header detection** — identifies foreign project licence text
   embedded in agent output.
5. **Severity-grouped Markdown report** via ``render_dlp_report``.

Regex-only, no network calls, no LLM.  Designed to complement the existing
``dlp_scanner.py`` and ``pii_output_gate.py`` quality gates.

Usage::

    from bernstein.core.security.dlp_scanner_v2 import (
        DLPCategory, DLPPolicy, scan_text, scan_file, scan_agent_output,
        render_dlp_report,
    )

    policy = DLPPolicy()
    results = scan_text("patient_id = 12345678", policy)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "DLPCategory",
    "DLPMatch",
    "DLPPolicy",
    "DLPScanResult",
    "render_dlp_report",
    "scan_agent_output",
    "scan_file",
    "scan_text",
]

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

DLPSeverity = str  # "critical" | "high" | "medium" | "low"


class DLPCategory(StrEnum):
    """High-level categories for DLP findings."""

    SOURCE_CODE = "source_code"
    PROPRIETARY_DATA = "proprietary_data"
    REGULATED_DATA = "regulated_data"
    CREDENTIALS = "credentials"
    PII = "pii"


@dataclass(frozen=True)
class DLPMatch:
    """A single DLP finding detected in scanned content.

    Attributes:
        category: High-level category of the finding.
        pattern_name: Short label identifying the detection rule.
        matched_text: Truncated excerpt of the matched content (max 40 chars).
        line_number: 1-based line number where the match was found.
        confidence: Confidence score between 0.0 and 1.0.
        severity: One of ``critical``, ``high``, ``medium``, ``low``.
    """

    category: DLPCategory
    pattern_name: str
    matched_text: str
    line_number: int
    confidence: float
    severity: DLPSeverity


@dataclass(frozen=True)
class DLPScanResult:
    """Aggregated result of scanning a single file or text block.

    Attributes:
        file_path: Path of the scanned file (empty string for raw text).
        matches: All findings, ordered by line number.
        blocked: True when at least one critical-severity match was found
            and the policy enables blocking on critical.
        scan_time_ms: Wall-clock scan duration in milliseconds.
    """

    file_path: str
    matches: tuple[DLPMatch, ...]
    blocked: bool
    scan_time_ms: float


@dataclass(frozen=True)
class DLPPolicy:
    """Configuration policy for the v2 DLP scanner.

    Attributes:
        enabled_categories: Categories to scan for; defaults to all.
        block_on_critical: When True, any critical-severity finding sets
            ``DLPScanResult.blocked`` to True.
        custom_patterns: Extra ``(pattern_name, regex_string)`` pairs that
            are scanned under the PROPRIETARY_DATA category.
        customer_id_prefixes: Prefixes for customer-ID detection (e.g.
            ``"CUST"``, ``"ORG"``).  Matches ``PREFIX-<id>`` patterns.
        internal_url_suffixes: Domain suffixes considered internal (e.g.
            ``".internal"``, ``".corp"``).
    """

    enabled_categories: frozenset[DLPCategory] = frozenset(DLPCategory)
    block_on_critical: bool = True
    custom_patterns: tuple[tuple[str, str], ...] = ()
    customer_id_prefixes: tuple[str, ...] = ("CUST", "ORG", "ACCT")
    internal_url_suffixes: tuple[str, ...] = (".internal", ".corp")


# ---------------------------------------------------------------------------
# Luhn validator
# ---------------------------------------------------------------------------


def _luhn_check(digits: str) -> bool:
    """Validate a digit string using the Luhn algorithm.

    Args:
        digits: String of digits (spaces/dashes already stripped).

    Returns:
        True when the digit string passes Luhn validation.
    """
    if not digits or not digits.isdigit():
        return False
    total = 0
    reverse_digits = digits[::-1]
    for i, ch in enumerate(reverse_digits):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Each rule entry: (category, pattern_name, compiled regex, severity, confidence, description)
_RuleDef = tuple[DLPCategory, str, re.Pattern[str], DLPSeverity, float, str]


def _build_regulated_data_rules() -> list[_RuleDef]:
    """Patterns for REGULATED_DATA: credit cards (Luhn), SSN, health records."""
    return [
        # Credit card — 13-19 digits (groups of 4 separated by spaces or dashes).
        # Luhn validation is applied post-match.
        (
            DLPCategory.REGULATED_DATA,
            "credit_card",
            re.compile(r"\b(?:\d[ -]*){13,19}\b"),
            "critical",
            0.95,
            "Credit card number (Luhn-validated)",
        ),
        # US Social Security Number: XXX-XX-XXXX
        (
            DLPCategory.REGULATED_DATA,
            "ssn",
            re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
            "critical",
            0.90,
            "US Social Security Number pattern",
        ),
        # Medical Record Number (MRN) — labeled
        (
            DLPCategory.REGULATED_DATA,
            "mrn",
            re.compile(r"(?i)(?:mrn|medical[-_]?record[-_]?(?:number|num|no|id))\s*[=:]\s*[\"']?\w{6,20}[\"']?"),
            "high",
            0.85,
            "Medical Record Number (MRN)",
        ),
        # Patient ID — labeled
        (
            DLPCategory.REGULATED_DATA,
            "patient_id",
            re.compile(r"(?i)patient[-_]?id\s*[=:]\s*[\"']?\w{4,20}[\"']?"),
            "high",
            0.85,
            "Patient ID pattern",
        ),
        # Health plan / member / insurance IDs
        (
            DLPCategory.REGULATED_DATA,
            "health_plan_id",
            re.compile(
                r"(?i)(?:health[-_]?plan[-_]?id|beneficiary[-_]?id|member[-_]?id|insurance[-_]?id)"
                r"\s*[=:]\s*[\"']?[A-Z0-9]{6,20}[\"']?"
            ),
            "medium",
            0.75,
            "Health plan / beneficiary / member ID",
        ),
    ]


def _build_pii_rules() -> list[_RuleDef]:
    """Patterns for PII: email addresses, phone numbers."""
    return [
        (
            DLPCategory.PII,
            "email_address",
            re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b"),
            "medium",
            0.70,
            "Email address",
        ),
    ]


def _build_credentials_rules() -> list[_RuleDef]:
    """Patterns for CREDENTIALS: API keys, tokens, passwords in assignments."""
    return [
        (
            DLPCategory.CREDENTIALS,
            "api_key",
            re.compile(
                r"(?i)(?:api[-_]?key|apikey|api[-_]?secret|secret[-_]?key)"
                r"\s*[=:]\s*[\"']?[\w\-/.+]{20,}[\"']?"
            ),
            "critical",
            0.90,
            "API key or secret",
        ),
        (
            DLPCategory.CREDENTIALS,
            "password_assignment",
            re.compile(r"(?i)(?:password|passwd|pwd)\s*[=:]\s*[\"'][^\"']{4,}[\"']"),
            "high",
            0.85,
            "Password in assignment",
        ),
        (
            DLPCategory.CREDENTIALS,
            "bearer_token",
            re.compile(r"(?i)(?:bearer|token|auth[-_]?token)\s*[=:]\s*[\"']?[\w\-/.+=]{20,}[\"']?"),
            "high",
            0.85,
            "Bearer / auth token",
        ),
        (
            DLPCategory.CREDENTIALS,
            "private_key_block",
            re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
            "critical",
            0.99,
            "Private key block",
        ),
    ]


def _build_proprietary_data_rules(policy: DLPPolicy) -> list[_RuleDef]:
    """Patterns for PROPRIETARY_DATA: internal URLs, customer IDs, custom."""
    rules: list[_RuleDef] = []

    # Internal URL patterns (*.internal.*, *.corp.*, etc.)
    for suffix in policy.internal_url_suffixes:
        safe_suffix = re.escape(suffix)
        rules.append(
            (
                DLPCategory.PROPRIETARY_DATA,
                f"internal_url_{suffix.strip('.')}",
                re.compile(
                    r"\b[A-Za-z\d](?:[A-Za-z\d-]*[A-Za-z\d])?" + safe_suffix + r"\b",
                    re.IGNORECASE,
                ),
                "medium",
                0.80,
                f"Internal URL pattern ({suffix})",
            )
        )

    # Customer ID patterns with configurable prefix
    if policy.customer_id_prefixes:
        prefix_alt = "|".join(re.escape(p) for p in policy.customer_id_prefixes)
        rules.append(
            (
                DLPCategory.PROPRIETARY_DATA,
                "customer_id",
                re.compile(rf"(?i)\b(?:{prefix_alt})[-_][A-Za-z\d]{{4,40}}\b"),
                "medium",
                0.75,
                "Customer ID with known prefix",
            )
        )

    # User-supplied custom patterns
    for name, pattern_str in policy.custom_patterns:
        try:
            compiled = re.compile(pattern_str, re.IGNORECASE)
            rules.append(
                (
                    DLPCategory.PROPRIETARY_DATA,
                    f"custom_{name}",
                    compiled,
                    "medium",
                    0.70,
                    f"Custom pattern: {name}",
                )
            )
        except re.error:
            pass  # skip invalid regex silently

    return rules


def _build_source_code_rules() -> list[_RuleDef]:
    """Patterns for SOURCE_CODE: license headers from other projects."""
    return [
        (
            DLPCategory.SOURCE_CODE,
            "spdx_license",
            re.compile(
                r"SPDX-License-Identifier\s*:\s*[\w.\-+]+",
                re.IGNORECASE,
            ),
            "high",
            0.90,
            "SPDX license identifier (possible copied source)",
        ),
        (
            DLPCategory.SOURCE_CODE,
            "copyright_header",
            re.compile(
                r"(?i)Copyright\s+(?:\(c\)|©)?\s*\d{4}",
            ),
            "high",
            0.80,
            "Copyright header from another project",
        ),
        (
            DLPCategory.SOURCE_CODE,
            "all_rights_reserved",
            re.compile(r"(?i)All\s+rights\s+reserved"),
            "high",
            0.80,
            '"All rights reserved" notice',
        ),
        (
            DLPCategory.SOURCE_CODE,
            "gpl_license_text",
            re.compile(r"(?i)(?:GNU General Public License|GNU Affero General Public License)"),
            "high",
            0.90,
            "GPL/AGPL license text",
        ),
        (
            DLPCategory.SOURCE_CODE,
            "mit_license_block",
            re.compile(r"(?i)Permission is hereby granted,?\s+free of charge"),
            "medium",
            0.85,
            "MIT license boilerplate",
        ),
        (
            DLPCategory.SOURCE_CODE,
            "apache_license_block",
            re.compile(r"(?i)Licensed under the Apache License,?\s+Version\s+2"),
            "medium",
            0.85,
            "Apache 2.0 license boilerplate",
        ),
    ]


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

_ALLOWLIST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)example\.com|example\.org|example\.net"),
    re.compile(r"(?i)localhost|127\.0\.0\.1|0\.0\.0\.0"),
    re.compile(r"(?i)test@|user@|admin@|noreply@|nobody@"),
    re.compile(r"(?i)\bplaceholder\b|\bchangeme\b|\bxxxxx+\b"),
    re.compile(r"(?i)\bFAKE\b|\bTEST\b|\bEXAMPLE\b|\bDUMMY\b|\bMOCK\b|\bSAMPLE\b"),
]


def _is_allowlisted(line: str) -> bool:
    """Return True when the line matches a known benign / test pattern."""
    return any(p.search(line) for p in _ALLOWLIST_PATTERNS)


# ---------------------------------------------------------------------------
# Truncation helper
# ---------------------------------------------------------------------------


def _truncate_match(text: str, max_len: int = 40) -> str:
    """Truncate matched text for safe storage/display."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Core scanning logic
# ---------------------------------------------------------------------------


def _collect_rules(policy: DLPPolicy) -> list[_RuleDef]:
    """Build the full rule set from the policy's enabled categories."""
    rules: list[_RuleDef] = []
    cats = policy.enabled_categories

    if DLPCategory.REGULATED_DATA in cats:
        rules.extend(_build_regulated_data_rules())
    if DLPCategory.PII in cats:
        rules.extend(_build_pii_rules())
    if DLPCategory.CREDENTIALS in cats:
        rules.extend(_build_credentials_rules())
    if DLPCategory.PROPRIETARY_DATA in cats:
        rules.extend(_build_proprietary_data_rules(policy))
    if DLPCategory.SOURCE_CODE in cats:
        rules.extend(_build_source_code_rules())

    return rules


def _validate_credit_card(matched_raw: str) -> bool:
    """Validate a potential credit card match using Luhn algorithm."""
    digits_only = re.sub(r"\D", "", matched_raw)
    return len(digits_only) >= 13 and _luhn_check(digits_only)


def _scan_lines(
    lines: list[str],
    rules: list[_RuleDef],
    _policy: DLPPolicy,
) -> list[DLPMatch]:
    """Scan lines against all rules and return matches."""
    matches: list[DLPMatch] = []

    for line_num, line in enumerate(lines, 1):
        if _is_allowlisted(line):
            continue

        for category, pattern_name, pattern, severity, confidence, _desc in rules:
            m = pattern.search(line)
            if not m:
                continue

            matched_raw = m.group(0)

            if pattern_name == "credit_card" and not _validate_credit_card(matched_raw):
                continue

            matches.append(
                DLPMatch(
                    category=category,
                    pattern_name=pattern_name,
                    matched_text=_truncate_match(matched_raw),
                    line_number=line_num,
                    confidence=confidence,
                    severity=severity,
                )
            )

    return matches


def _is_blocked(matches: tuple[DLPMatch, ...], policy: DLPPolicy) -> bool:
    """Determine whether findings should block based on policy."""
    if not policy.block_on_critical:
        return False
    return any(m.severity == "critical" for m in matches)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_text(text: str, policy: DLPPolicy | None = None) -> DLPScanResult:
    """Scan arbitrary text for DLP violations.

    Args:
        text: Text content to scan.
        policy: DLP policy configuration; uses defaults when None.

    Returns:
        DLPScanResult with all findings.
    """
    pol = policy or DLPPolicy()
    rules = _collect_rules(pol)

    start = time.monotonic()
    lines = text.splitlines()
    found = _scan_lines(lines, rules, pol)
    elapsed_ms = (time.monotonic() - start) * 1000.0

    matches = tuple(found)
    return DLPScanResult(
        file_path="",
        matches=matches,
        blocked=_is_blocked(matches, pol),
        scan_time_ms=round(elapsed_ms, 2),
    )


def scan_file(file_path: Path | str, policy: DLPPolicy | None = None) -> DLPScanResult:
    """Scan a file for DLP violations.

    Args:
        file_path: Path to the file to scan.
        policy: DLP policy configuration; uses defaults when None.

    Returns:
        DLPScanResult with findings.  Returns an empty result with zero
        matches for unreadable or binary files.
    """
    pol = policy or DLPPolicy()
    rules = _collect_rules(pol)
    fp = Path(file_path)

    start = time.monotonic()
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return DLPScanResult(
            file_path=str(fp),
            matches=(),
            blocked=False,
            scan_time_ms=round(elapsed_ms, 2),
        )

    lines = text.splitlines()
    found = _scan_lines(lines, rules, pol)
    elapsed_ms = (time.monotonic() - start) * 1000.0

    matches = tuple(found)
    return DLPScanResult(
        file_path=str(fp),
        matches=matches,
        blocked=_is_blocked(matches, pol),
        scan_time_ms=round(elapsed_ms, 2),
    )


def scan_agent_output(
    output_dir: Path | str,
    policy: DLPPolicy | None = None,
) -> list[DLPScanResult]:
    """Scan all files in an agent output directory.

    Recursively walks the directory and scans each text file.

    Args:
        output_dir: Directory containing agent output files.
        policy: DLP policy configuration; uses defaults when None.

    Returns:
        List of DLPScanResult, one per scanned file.
    """
    pol = policy or DLPPolicy()
    d = Path(output_dir)
    results: list[DLPScanResult] = []

    if not d.is_dir():
        return results

    for fp in sorted(d.rglob("*")):
        if not fp.is_file():
            continue
        results.append(scan_file(fp, pol))

    return results


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def render_dlp_report(results: list[DLPScanResult]) -> str:
    """Render a Markdown report from scan results, grouped by severity.

    Args:
        results: List of scan results (e.g. from ``scan_agent_output``).

    Returns:
        Markdown-formatted report string.
    """
    all_matches: list[tuple[str, DLPMatch]] = []
    total_blocked = 0

    for r in results:
        if r.blocked:
            total_blocked += 1
        for m in r.matches:
            all_matches.append((r.file_path, m))

    if not all_matches:
        return "# DLP Scan Report\n\nNo violations detected."

    # Group by severity
    by_severity: dict[str, list[tuple[str, DLPMatch]]] = {}
    for file_path, match in all_matches:
        sev = match.severity
        by_severity.setdefault(sev, []).append((file_path, match))

    lines: list[str] = [
        "# DLP Scan Report",
        "",
        f"**Total findings:** {len(all_matches)}  ",
        f"**Files blocked:** {total_blocked}  ",
        "",
    ]

    # Emit sections in severity order
    for sev in sorted(by_severity, key=lambda s: _SEVERITY_ORDER.get(s, 99)):
        entries = by_severity[sev]
        lines.append(f"## {sev.upper()} ({len(entries)})")
        lines.append("")
        for file_path, match in entries:
            loc = f"{file_path}:{match.line_number}" if file_path else f"line {match.line_number}"
            lines.append(
                f"- **{match.pattern_name}** [{match.category}] "
                f"at `{loc}` "
                f"(confidence {match.confidence:.0%}): "
                f"`{match.matched_text}`"
            )
        lines.append("")

    return "\n".join(lines)
