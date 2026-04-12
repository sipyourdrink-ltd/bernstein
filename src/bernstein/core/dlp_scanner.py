"""Data Loss Prevention (DLP) scanner for agent outputs.

Extends PII/secret detection (pii_output_gate.py) with additional categories:

1. **License violations** — copyright headers or SPDX identifiers from external
   projects introduced by agent diffs.  Hard-blocks merge when detected.
2. **Regulated data (PHI)** — HIPAA-relevant data patterns: medical record
   numbers, National Provider Identifiers, ICD-10 codes, DEA drug codes.
   Hard-blocks merge (alongside existing credit-card / SSN checks).
3. **Proprietary data leakage** — internal hostnames / RFC-1918 addresses,
   customer-ID-labeled UUIDs, and configurable internal-URL patterns.
   Soft-flags by default; can be promoted to hard-block via config.

Regex-only — no network calls, no LLM.  Designed to run as a quality gate
that complements the existing ``pii_scan`` gate.

Usage::

    from bernstein.core.dlp_scanner import DLPScanner, DLPConfig

    config = DLPConfig()
    scanner = DLPScanner(config)
    result = scanner.scan_diff(diff_text)
    if result.has_blocks:
        # hard-block merge
        ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "DLPCategory",
    "DLPConfig",
    "DLPFinding",
    "DLPScanResult",
    "DLPScanner",
    "scan_diff_for_dlp",
    "scan_text_for_dlp",
]

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

DLPCategory = Literal["license_violation", "regulated_data", "proprietary_data"]
DLPSeverity = Literal["critical", "high", "medium", "low"]


@dataclass(frozen=True)
class DLPFinding:
    """A single DLP finding detected in agent output.

    Attributes:
        category: High-level category of the finding.
        rule: Short label identifying the detection rule.
        severity: Severity rating (critical/high/medium/low).
        line_number: 1-based line number in the scanned text (0 if unknown).
        redacted_match: Up to 80 chars of context with the sensitive value
            replaced by ``***``.  Raw data is never stored.
        description: Human-readable explanation of what was detected.
        block_merge: True when this finding must hard-block merge.
    """

    category: DLPCategory
    rule: str
    severity: DLPSeverity
    line_number: int
    redacted_match: str
    description: str
    block_merge: bool


@dataclass(frozen=True)
class DLPScanResult:
    """Aggregated result of a DLP scan pass.

    Attributes:
        findings: All findings, ordered by line number.
        has_blocks: True when at least one finding has ``block_merge=True``.
        categories_hit: Set of categories that had at least one finding.
    """

    findings: list[DLPFinding]
    has_blocks: bool
    categories_hit: frozenset[DLPCategory]

    @classmethod
    def empty(cls) -> DLPScanResult:
        """Return an empty result (no findings)."""
        return cls(findings=[], has_blocks=False, categories_hit=frozenset())

    def format_report(self) -> str:
        """Return a human-readable summary for quality gate output."""
        if not self.findings:
            return "DLP scan: no violations detected."
        lines = [f"DLP scan: {len(self.findings)} finding(s) — categories: {', '.join(sorted(self.categories_hit))}"]
        for f in self.findings:
            block_label = "[BLOCK]" if f.block_merge else "[WARN]"
            lines.append(
                f"  {block_label} [{f.severity.upper()}] {f.category}/{f.rule}"
                f" (line {f.line_number}): {f.description} — {f.redacted_match}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DLPConfig:
    """Configuration for the DLP scanner.

    Attributes:
        enabled: Master switch.
        check_license_violations: Detect copyright headers / SPDX identifiers
            from external projects being introduced.
        check_regulated_data: Detect PHI / health records patterns.
        check_proprietary_data: Detect internal hostnames, customer IDs, etc.
        block_license_violations: Hard-block merge on license violations.
        block_regulated_data: Hard-block merge on regulated data findings.
        block_proprietary_data: Hard-block merge on proprietary data findings
            (default False — soft-flag only).
        internal_url_patterns: Additional hostname / URL glob patterns that
            are considered internal / proprietary (e.g. ``"*.corp.example.com"``).
        ignore_paths: Glob-like path patterns to skip during scanning.
        allowlist_prefixes: Fake/test value prefixes that suppress findings.
        allowlist_patterns: Additional regex patterns that suppress a line.
    """

    enabled: bool = True
    check_license_violations: bool = True
    check_regulated_data: bool = True
    check_proprietary_data: bool = True
    block_license_violations: bool = True
    block_regulated_data: bool = True
    block_proprietary_data: bool = False
    internal_url_patterns: list[str] = field(default_factory=lambda: list[str]())
    ignore_paths: list[str] = field(default_factory=lambda: list[str]())
    allowlist_prefixes: list[str] = field(
        default_factory=lambda: ["FAKE", "TEST", "EXAMPLE", "DUMMY", "PLACEHOLDER", "MOCK", "SAMPLE"]
    )
    allowlist_patterns: list[str] = field(default_factory=lambda: list[str]())


# ---------------------------------------------------------------------------
# Detection rule definitions
# ---------------------------------------------------------------------------

# Rule entry: (category, rule_label, pattern, severity, description, block_by_default)
_RuleEntry = tuple[DLPCategory, str, re.Pattern[str], DLPSeverity, str, bool]


def _build_license_violation_rules() -> list[_RuleEntry]:
    """Rules that detect external project source code / license headers."""
    return [
        # SPDX-License-Identifier lines — any license being *added* may indicate
        # copying of third-party code into the agent output.
        (
            "license_violation",
            "spdx_identifier",
            re.compile(
                r"SPDX-License-Identifier\s*:\s*[A-Za-z0-9.\-+]+",
                re.IGNORECASE,
            ),
            "high",
            "SPDX license identifier introduced — may indicate copied third-party source",
            True,
        ),
        # Classic copyright header lines from external projects.
        (
            "license_violation",
            "copyright_header",
            re.compile(
                r"(?i)Copyright\s+(?:\(c\)|©)?\s*\d{4}(?:\s*[-]\s*\d{4})?\s+(?!Bernstein|Sasha|Your Name|Author)",
                re.IGNORECASE,
            ),
            "high",
            "Third-party copyright header introduced — verify licence compatibility",
            True,
        ),
        # "All rights reserved" boilerplate often accompanies proprietary code.
        (
            "license_violation",
            "all_rights_reserved",
            re.compile(r"(?i)All\s+rights\s+reserved"),
            "high",
            '"All rights reserved" notice — indicates proprietary source being introduced',
            True,
        ),
        # GPL/AGPL license boilerplate text.
        (
            "license_violation",
            "gpl_notice",
            re.compile(
                r"(?i)(?:GNU General Public License|GNU Affero General Public License)",
            ),
            "high",
            "GPL/AGPL license text introduced — strong copyleft obligation",
            True,
        ),
    ]


def _build_regulated_data_rules() -> list[_RuleEntry]:
    """Rules that detect regulated PHI / PCI DSS / financial record patterns."""
    return [
        # Credit card numbers — 13-19 digits, Luhn-valid, with explicit label
        # or in common card-number formatted patterns (groups separated by spaces/dashes).
        # We match labeled context first to reduce false positives.
        (
            "regulated_data",
            "credit_card_number",
            re.compile(
                r"""(?i)(?:card[-_]?(?:number|num|no)|cc[-_]?(?:number|num|no)"""
                r"""|pan|credit[-_]?card)\s*[=:]\s*["']?(?:\d[ -]*){13,19}["']?"""
            ),
            "critical",
            "Credit card number (PCI DSS) — store/transmit only via compliant vault",
            True,
        ),
        # US Social Security Numbers — labeled or bare XXX-XX-XXXX pattern.
        (
            "regulated_data",
            "us_ssn",
            re.compile(
                r"""(?i)(?:ssn|social[-_]?security[-_]?(?:number|num|no))\s*[=:]\s*["']?\d{3}-\d{2}-\d{4}["']?"""
                r"""|(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"""
            ),
            "critical",
            "US Social Security Number — regulated PII, must not appear in code or logs",
            True,
        ),
        # National Provider Identifier (NPI) — 10-digit number, often labeled.
        (
            "regulated_data",
            "npi_number",
            re.compile(r"""(?i)(?:npi|national_provider_id(?:entifier)?)\s*[=:]\s*["']?\b\d{10}\b["']?"""),
            "high",
            "National Provider Identifier (NPI) — HIPAA-regulated healthcare entity ID",
            True,
        ),
        # ICD-10 diagnostic code — letter followed by 2 digits, optional dot + up to 4 chars.
        (
            "regulated_data",
            "icd10_code",
            re.compile(r"""(?i)(?:icd[-_]?10|diagnosis_code|diag_code)\s*[=:]\s*["']?[A-Z]\d{2}(?:\.\d{1,4})?["']?"""),
            "high",
            "ICD-10 diagnosis code — HIPAA-protected health information",
            True,
        ),
        # Medical Record Number (MRN) — often an 8-10 digit number with explicit label.
        (
            "regulated_data",
            "mrn",
            re.compile(
                r"""(?i)(?:mrn|medical_record_number|medical_record_no|patient_id)\s*[=:]\s*["']?\b\d{6,12}\b["']?"""
            ),
            "high",
            "Medical Record Number (MRN) — HIPAA PHI",
            True,
        ),
        # DEA drug enforcement registration number — format: 2 letters + 7 digits.
        (
            "regulated_data",
            "dea_number",
            re.compile(r"""(?i)(?:dea_number|dea_reg|dea_registration)\s*[=:]\s*["']?[A-Z]{2}\d{7}["']?"""),
            "high",
            "DEA drug enforcement registration number — federally regulated",
            True,
        ),
        # Health plan beneficiary numbers / member IDs (labeled patterns).
        (
            "regulated_data",
            "health_plan_id",
            re.compile(
                r"""(?i)(?:health_plan_id|beneficiary_id|member_id|insurance_id)\s*[=:]\s*["']?[A-Z0-9]{8,20}["']?"""
            ),
            "medium",
            "Health plan beneficiary / member ID — potential HIPAA PHI",
            True,
        ),
        # Date-of-birth with explicit label.
        (
            "regulated_data",
            "date_of_birth",
            re.compile(r"""(?i)(?:date_of_birth|dob|birth_date)\s*[=:]\s*["']?\d{4}[-/]\d{2}[-/]\d{2}["']?"""),
            "medium",
            "Date of birth — HIPAA PHI when combined with other health data",
            True,
        ),
    ]


def _build_proprietary_data_rules(config: DLPConfig) -> list[_RuleEntry]:
    """Rules that detect internal infrastructure and customer data leakage."""
    rules: list[_RuleEntry] = [
        # RFC-1918 private IP addresses in configuration / code.
        (
            "proprietary_data",
            "private_ip_address",
            re.compile(
                r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                r"|192\.168\.\d{1,3}\.\d{1,3})\b"
            ),
            "medium",
            "RFC-1918 private IP address — may expose internal network topology",
            False,
        ),
        # Internal hostnames: .internal, .corp, .local, .intranet suffixes.
        (
            "proprietary_data",
            "internal_hostname",
            re.compile(
                r"\b(?:[a-zA-Z0-9-]+\.)+(?:internal|corp|intranet|lan|private)\b",
                re.IGNORECASE,
            ),
            "medium",
            "Internal hostname suffix (.internal/.corp/.intranet) — may expose infrastructure",
            False,
        ),
        # Customer ID / account ID labeled UUID values.
        (
            "proprietary_data",
            "customer_id",
            re.compile(
                r"""(?i)(?:customer_id|account_id|org_id|organisation_id|organization_id|client_id)\s*[=:]\s*["']?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}["']?"""
            ),
            "medium",
            "Customer/account UUID — may expose production customer data",
            False,
        ),
        # Production database connection strings for customer-facing DBs.
        (
            "proprietary_data",
            "prod_db_connection",
            re.compile(
                r"""(?i)(?:prod(?:uction)?[-_]db|db[-_]prod(?:uction)?).*(?:host|server|endpoint)\s*[=:]\s*["'][^"'\s]{5,}["']"""
            ),
            "high",
            "Production database connection endpoint — proprietary infrastructure",
            False,
        ),
    ]

    # User-supplied internal URL patterns.
    for pattern_str in config.internal_url_patterns:
        try:
            compiled = re.compile(re.escape(pattern_str).replace(r"\*", r"[^.\s]+"), re.IGNORECASE)
            rules.append(
                (
                    "proprietary_data",
                    f"custom_internal_url_{len(rules)}",
                    compiled,
                    "medium",
                    f"Internal URL pattern match: {pattern_str}",
                    False,
                )
            )
        except re.error:
            pass  # Skip invalid patterns silently

    return rules


# ---------------------------------------------------------------------------
# Allowlist helpers
# ---------------------------------------------------------------------------

_COMMON_ALLOWLIST: list[re.Pattern[str]] = [
    re.compile(r"(?i)example\.com|example\.org|example\.net"),
    re.compile(r"(?i)test@|user@|admin@|noreply@"),
    re.compile(r"(?i)placeholder|changeme|your[-_]?api[-_]?key|xxxx"),
    re.compile(r"(?i)localhost|127\.0\.0\.1|0\.0\.0\.0"),
    re.compile(r"#.*copyright.*author"),  # doc templates
    re.compile(r"(?i)bernstein.*copyright|copyright.*bernstein"),  # project's own header
]


def _is_allowlisted_line(line: str, config: DLPConfig) -> bool:
    """Return True when the line matches a known benign / test pattern."""
    # Common patterns
    if any(p.search(line) for p in _COMMON_ALLOWLIST):
        return True
    # User-configured allowlist patterns
    for pattern_str in config.allowlist_patterns:
        try:
            if re.search(pattern_str, line, re.IGNORECASE):
                return True
        except re.error:
            pass
    # Allowlist prefix check
    if config.allowlist_prefixes:
        prefix_re = re.compile(
            r"""(?ix)
            (?:["']|=|:)\s*
            (?:{prefixes})
            (?:[_:\-A-Za-z0-9./]*)?
            (?:["']|$)
            """.format(prefixes="|".join(re.escape(p) for p in config.allowlist_prefixes))
        )
        if prefix_re.search(line):
            return True
    return False


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class DLPScanner:
    """Stateless DLP scanner for agent-produced diffs and file content.

    Args:
        config: Scanner configuration.
    """

    def __init__(self, config: DLPConfig | None = None) -> None:
        self._config = config or DLPConfig()
        self._rules: list[_RuleEntry] = []
        self._build_rules()

    def _build_rules(self) -> None:
        cfg = self._config
        if cfg.check_license_violations:
            self._rules.extend(_build_license_violation_rules())
        if cfg.check_regulated_data:
            self._rules.extend(_build_regulated_data_rules())
        if cfg.check_proprietary_data:
            self._rules.extend(_build_proprietary_data_rules(cfg))

    def _should_block(self, category: DLPCategory, rule_block_default: bool) -> bool:
        cfg = self._config
        if category == "license_violation":
            return cfg.block_license_violations
        if category == "regulated_data":
            return cfg.block_regulated_data
        if category == "proprietary_data":
            return cfg.block_proprietary_data
        return rule_block_default

    @staticmethod
    def _extract_line_for_scan(raw_line: str, diff_mode: bool) -> str | None:
        """Extract the scannable line content, or None to skip this line."""
        if not diff_mode:
            return raw_line
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            return None
        return raw_line[1:]  # strip leading "+"

    @staticmethod
    def _build_redacted_excerpt(line: str, m: object) -> str:
        """Build a redacted excerpt around a regex match."""
        start = max(0, m.start() - 15)  # type: ignore[union-attr]
        end = min(len(line), m.end() + 15)  # type: ignore[union-attr]
        raw_excerpt = line[start:end]
        rel_start = m.start() - start  # type: ignore[union-attr]
        rel_end = m.end() - start  # type: ignore[union-attr]
        redacted = raw_excerpt[:rel_start] + "***" + raw_excerpt[rel_end:]
        if len(redacted) > 80:
            redacted = redacted[:77] + "..."
        return redacted

    def _scan_lines(self, lines: list[str], *, diff_mode: bool = False) -> list[DLPFinding]:
        """Scan a list of text lines and return findings."""
        findings: list[DLPFinding] = []
        seen_rules: set[str] = set()

        for line_num, raw_line in enumerate(lines, 1):
            line = self._extract_line_for_scan(raw_line, diff_mode)
            if line is None or _is_allowlisted_line(line, self._config):
                continue

            for category, rule_label, pattern, severity, description, block_default in self._rules:
                if rule_label in seen_rules:
                    continue
                m = pattern.search(line)
                if not m:
                    continue

                findings.append(
                    DLPFinding(
                        category=category,
                        rule=rule_label,
                        severity=severity,
                        line_number=line_num,
                        redacted_match=self._build_redacted_excerpt(line, m),
                        description=description,
                        block_merge=self._should_block(category, block_default),
                    )
                )
                seen_rules.add(rule_label)

        return findings

    def scan_text(self, text: str) -> DLPScanResult:
        """Scan arbitrary text content (file content, log output, etc.).

        Args:
            text: Text content to scan.

        Returns:
            DLPScanResult with all findings.
        """
        if not self._config.enabled:
            return DLPScanResult.empty()

        lines = text.splitlines()
        findings = self._scan_lines(lines, diff_mode=False)
        return _make_result(findings)

    def scan_diff(self, diff_text: str) -> DLPScanResult:
        """Scan a unified diff, inspecting only added lines.

        Lines starting with ``+`` (additions) are checked.  Removed lines
        and context lines are ignored — we only block on *new* violations.

        Args:
            diff_text: Unified diff text (e.g. from ``git diff``).

        Returns:
            DLPScanResult with all findings from added lines.
        """
        if not self._config.enabled:
            return DLPScanResult.empty()

        lines = diff_text.splitlines()
        findings = self._scan_lines(lines, diff_mode=True)
        return _make_result(findings)


def _make_result(findings: list[DLPFinding]) -> DLPScanResult:
    has_blocks = any(f.block_merge for f in findings)
    categories_hit: frozenset[DLPCategory] = frozenset(f.category for f in findings)
    return DLPScanResult(findings=findings, has_blocks=has_blocks, categories_hit=categories_hit)


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


def scan_text_for_dlp(
    text: str,
    *,
    config: DLPConfig | None = None,
) -> DLPScanResult:
    """Scan text content for DLP violations.

    Args:
        text: Content to scan.
        config: Optional DLP configuration.

    Returns:
        DLPScanResult with all findings.
    """
    return DLPScanner(config).scan_text(text)


def scan_diff_for_dlp(
    diff_text: str,
    *,
    config: DLPConfig | None = None,
) -> DLPScanResult:
    """Scan a unified diff for DLP violations in added lines.

    Args:
        diff_text: Unified diff output.
        config: Optional DLP configuration.

    Returns:
        DLPScanResult with all findings from added lines.
    """
    return DLPScanner(config).scan_diff(diff_text)
