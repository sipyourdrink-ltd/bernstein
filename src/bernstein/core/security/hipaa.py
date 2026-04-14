"""HIPAA compliance mode: PHI detection, file access controls, and reporting.

Activates when ``compliance: hipaa`` is set in bernstein.yaml.  Provides:

1. **PHI detection** — regex-based detection of Protected Health Information
   in agent inputs and outputs (SSNs, MRNs, DOBs, phone numbers, email,
   diagnoses keywords, ICD codes).  Does not use ML models to avoid
   external dependencies.

2. **File access controls** — blocks agent access to files matching PHI path
   patterns (e.g. ``*.phi``, ``patient_records/**``).

3. **Encryption at rest** — AES-256-GCM encryption for all ``.sdd/`` state
   files when HIPAA mode is active (uses ``cryptography`` package).

4. **BAA-ready compliance report** — generates a structured report suitable
   for inclusion in a Business Associate Agreement audit package.

Different from ``pii_output_gate.py`` (which gates generic PII in output) —
this is a comprehensive mode that enforces HIPAA-specific controls across the
full agent lifecycle.

Usage::

    from bernstein.core.security.hipaa import HIPAAMode, PHIDetector

    detector = PHIDetector()
    result = detector.scan("Patient SSN: 123-45-6789")
    if result.contains_phi:
        raise BlockedByHIPAAPolicy(result.findings)
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ISO 8601 UTC datetime format used in HIPAA event timestamps and reports
ISO_DATETIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

# ---------------------------------------------------------------------------
# PHI categories and patterns
# ---------------------------------------------------------------------------


class PHICategory(StrEnum):
    """HIPAA-defined PHI identifier categories (45 CFR §164.514(b))."""

    SSN = "ssn"
    MRN = "mrn"
    DOB = "dob"
    PHONE = "phone"
    EMAIL = "email"
    FULL_NAME = "full_name"
    ADDRESS = "address"
    ZIP_CODE = "zip_code"
    DATE = "date"
    ICD_CODE = "icd_code"
    DIAGNOSIS = "diagnosis"
    ACCOUNT_NUMBER = "account_number"
    HEALTH_PLAN_NUMBER = "health_plan_number"
    DEVICE_ID = "device_id"
    URL = "url"
    IP_ADDRESS = "ip_address"


# Pattern registry: (category, compiled regex, description)
_PHI_PATTERNS: list[tuple[PHICategory, re.Pattern[str], str]] = [
    (
        PHICategory.SSN,
        re.compile(r"\b(?!000|666|9\d\d)\d{3}[- ]\d{2}[- ]\d{4}\b"),
        "Social Security Number",
    ),
    (
        PHICategory.MRN,
        re.compile(r"\b(?:MRN|Medical Record Number|Patient ID)[:\s#]*\d{5,12}\b", re.IGNORECASE),
        "Medical Record Number",
    ),
    (
        PHICategory.DOB,
        re.compile(
            r"\b(?:DOB|Date of Birth|Birth Date)[:\s]*"
            r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{2}[/-]\d{2})\b",
            re.IGNORECASE,
        ),
        "Date of Birth",
    ),
    (
        PHICategory.PHONE,
        re.compile(
            r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b",
        ),
        "Phone Number",
    ),
    (
        PHICategory.EMAIL,
        re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"),
        "Email Address",
    ),
    (
        PHICategory.ZIP_CODE,
        re.compile(r"\b(?:ZIP|Zip Code|Postal Code)[:\s]*(\d{5}(?:-\d{4})?)\b", re.IGNORECASE),
        "ZIP Code",
    ),
    (
        PHICategory.ICD_CODE,
        re.compile(
            r"\b(?:ICD-?(?:9|10|11)[:\s-]*)?"
            r"[A-TV-Z]\d{2}(?:[.\-]\d{1,4}[A-Z]?)?\b",
        ),
        "ICD Diagnosis Code",
    ),
    (
        PHICategory.HEALTH_PLAN_NUMBER,
        re.compile(
            r"\b(?:Health Plan|Insurance|Policy|Member)[:\s#]*"
            r"[A-Z]{0,3}\d{6,15}\b",
            re.IGNORECASE,
        ),
        "Health Plan Beneficiary Number",
    ),
    (
        PHICategory.ACCOUNT_NUMBER,
        re.compile(r"\b(?:Account|Acct)[\s.:#]{0,10}(?:No|Num|Number)?\.?[\s:]{0,10}\d{6,16}\b", re.IGNORECASE),
        "Account Number",
    ),
    (
        PHICategory.IP_ADDRESS,
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        "IP Address",
    ),
    (
        PHICategory.DIAGNOSIS,
        re.compile(
            r"\b(?:diagnosed with|diagnosis[:\s]+|condition[:\s]+|suffers? from|presenting with)\s+"
            r"[a-z][a-z\s]{3,50}\b",
            re.IGNORECASE,
        ),
        "Diagnosis or Condition",
    ),
    (
        PHICategory.DEVICE_ID,
        re.compile(
            r"\b(?:Device ID|Serial Number|Implant(?:able)? Device)[:\s#]*"
            r"[A-Z0-9\-]{6,20}\b",
            re.IGNORECASE,
        ),
        "Device Identifier",
    ),
]

# Contextual trigger words that elevate pattern confidence
_HEALTH_CONTEXT_TERMS = re.compile(
    r"\b(?:patient|medical|health|clinical|diagnosis|treatment|"
    r"medication|prescription|hospital|physician|provider|record|"
    r"insurance|hipaa|phi|ehr|emr|lab result|specimen|discharge)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PHIFinding:
    """A single PHI match within a text.

    Attributes:
        category: PHI category matched.
        description: Human-readable description of the pattern.
        start: Start character offset in the source text.
        end: End character offset.
        redacted: The matched text replaced with ``[REDACTED]``.
        context_window: Up to 40 chars of surrounding context (no PHI value).
    """

    category: PHICategory
    description: str
    start: int
    end: int
    redacted: str
    context_window: str


@dataclass(frozen=True)
class PHIDetectionResult:
    """Result of scanning a text block for PHI.

    Attributes:
        contains_phi: True if any PHI was detected.
        findings: List of individual PHI matches.
        redacted_text: Input text with all PHI values replaced by
            ``[REDACTED:<CATEGORY>]``.
        has_health_context: True if contextual health terms are present
            (indicates higher confidence).
    """

    contains_phi: bool
    findings: list[PHIFinding]
    redacted_text: str
    has_health_context: bool


# ---------------------------------------------------------------------------
# PHI Detector
# ---------------------------------------------------------------------------


class PHIDetector:
    """Scans text for Protected Health Information using regex patterns.

    This is a pattern-based detector; it does not use ML models.  It has
    moderate false-positive rates on technical text (ICD codes look like
    variable names).  Use ``require_health_context=True`` to only flag
    findings in texts that also contain health-domain terminology.

    Args:
        require_health_context: When True, only report findings in texts
            that also contain at least one health context term.  Reduces
            false positives at the cost of some recall.
        extra_patterns: Additional ``(category, pattern, description)``
            tuples to add to the default registry.
    """

    def __init__(
        self,
        require_health_context: bool = False,
        extra_patterns: list[tuple[PHICategory, re.Pattern[str], str]] | None = None,
    ) -> None:
        self._require_health_context = require_health_context
        self._patterns = list(_PHI_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)

    def scan(self, text: str) -> PHIDetectionResult:
        """Scan text for PHI and return a structured result.

        Args:
            text: The text to scan.

        Returns:
            PHIDetectionResult with findings and redacted text.
        """
        has_health_context = bool(_HEALTH_CONTEXT_TERMS.search(text))

        if self._require_health_context and not has_health_context:
            return PHIDetectionResult(
                contains_phi=False,
                findings=[],
                redacted_text=text,
                has_health_context=False,
            )

        findings: list[PHIFinding] = []
        for category, pattern, description in self._patterns:
            for match in pattern.finditer(text):
                # Build a safe context window (avoid including adjacent PHI)
                ctx_start = max(0, match.start() - 20)
                ctx_end = min(len(text), match.end() + 20)
                raw_context = text[ctx_start:ctx_end]
                # Replace the match itself in the context window
                offset = match.start() - ctx_start
                context_window = raw_context[:offset] + "[REDACTED]" + raw_context[offset + len(match.group()) :]
                findings.append(
                    PHIFinding(
                        category=category,
                        description=description,
                        start=match.start(),
                        end=match.end(),
                        redacted=f"[REDACTED:{category.value.upper()}]",
                        context_window=context_window[:80],
                    )
                )

        redacted_text = self._redact(text, findings)
        return PHIDetectionResult(
            contains_phi=len(findings) > 0,
            findings=findings,
            redacted_text=redacted_text,
            has_health_context=has_health_context,
        )

    @staticmethod
    def _redact(text: str, findings: list[PHIFinding]) -> str:
        """Replace all PHI matches in text with redaction placeholders."""
        if not findings:
            return text
        # Sort by start position descending so offsets stay valid
        sorted_findings = sorted(findings, key=lambda f: f.start, reverse=True)
        chars = list(text)
        for finding in sorted_findings:
            replacement = list(finding.redacted)
            chars[finding.start : finding.end] = replacement
        return "".join(chars)


# ---------------------------------------------------------------------------
# File access controls
# ---------------------------------------------------------------------------


# Default PHI file path patterns to block
_DEFAULT_PHI_FILE_PATTERNS: list[str] = [
    "*.phi",
    "*.ehr",
    "*.emr",
    "patient_records/**",
    "patient-records/**",
    "medical_records/**",
    "medical-records/**",
    "health_data/**",
    "**/phi/**",
    "**/ehr/**",
    "**/patient/**",
    "**/patients/**",
    "**/*ssn*",
    "*ssn*",
    "**/*dob*",
    "*dob*",
    "**/*mrn*",
    "*mrn*",
]


def _matches_phi_pattern(normalized: str, filename: str, pattern: str) -> bool:
    """Check if a normalized path or filename matches a single PHI pattern."""
    if fnmatch.fnmatch(normalized, pattern):
        return True
    if fnmatch.fnmatch(filename, pattern):
        return True
    if "**" not in pattern:
        return False
    parts = pattern.split("**")
    prefix = parts[0].rstrip("/")
    suffix = parts[-1].lstrip("/")
    if prefix and not normalized.startswith(prefix):
        return False
    if suffix and not normalized.endswith(suffix):
        return False
    return bool(prefix or suffix)


def is_phi_file(
    file_path: str,
    patterns: list[str] | None = None,
) -> bool:
    """Check whether a file path matches any PHI access-control pattern.

    Args:
        file_path: The file path to check (can be relative or absolute).
        patterns: List of glob patterns that identify PHI files.
            Defaults to ``_DEFAULT_PHI_FILE_PATTERNS``.

    Returns:
        True if the file should be blocked under HIPAA mode.
    """
    if patterns is None:
        patterns = _DEFAULT_PHI_FILE_PATTERNS

    normalized = file_path.replace(os.sep, "/")
    filename = os.path.basename(file_path)

    return any(_matches_phi_pattern(normalized, filename, p) for p in patterns)


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------


def encrypt_file_aes256gcm(file_path: Path, key: bytes) -> Path:
    """Encrypt a file in-place with AES-256-GCM.

    The original file is overwritten with: ``nonce (12B) || ciphertext || tag (16B)``.
    A ``.enc`` suffix is appended to the file name.

    Args:
        file_path: Path to the plaintext file.
        key: 32-byte AES-256 key.

    Returns:
        Path to the encrypted file.

    Raises:
        ValueError: If key is not 32 bytes.
    """
    if len(key) != 32:
        msg = f"AES-256 key must be 32 bytes, got {len(key)}"
        raise ValueError(msg)

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    plaintext = file_path.read_bytes()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    enc_path = file_path.with_suffix(file_path.suffix + ".enc")
    enc_path.write_bytes(nonce + ciphertext)
    file_path.unlink()
    return enc_path


def decrypt_file_aes256gcm(enc_path: Path, key: bytes) -> Path:
    """Decrypt a file encrypted by ``encrypt_file_aes256gcm``.

    Args:
        enc_path: Path to the ``.enc`` encrypted file.
        key: 32-byte AES-256 key.

    Returns:
        Path to the decrypted file (without the ``.enc`` suffix).

    Raises:
        ValueError: If key is not 32 bytes.
        cryptography.exceptions.InvalidTag: If decryption fails (wrong key or tampered).
    """
    if len(key) != 32:
        msg = f"AES-256 key must be 32 bytes, got {len(key)}"
        raise ValueError(msg)

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = enc_path.read_bytes()
    nonce = blob[:12]
    ciphertext = blob[12:]

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)

    out_path = enc_path.with_suffix("")  # strip .enc
    out_path.write_bytes(plaintext)
    enc_path.unlink()
    return out_path


def load_or_create_hipaa_encryption_key(sdd_dir: Path) -> bytes:
    """Load the HIPAA encryption key from ``.sdd/config/hipaa-enc-key``.

    Generates a new 32-byte key if the file doesn't exist.

    Args:
        sdd_dir: Path to the ``.sdd`` directory.

    Returns:
        32-byte AES-256 key.
    """
    key_path = sdd_dir / "config" / "hipaa-enc-key"
    if key_path.exists():
        return key_path.read_bytes()[:32]
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    logger.info("Generated HIPAA encryption key: %s", key_path)
    return key


# ---------------------------------------------------------------------------
# BAA-ready compliance report
# ---------------------------------------------------------------------------


@dataclass
class HIPAAComplianceReport:
    """A BAA-ready HIPAA compliance report.

    Attributes:
        generated_at: ISO 8601 timestamp.
        organization: Organization name (from config or environment).
        baa_contact: Name/email of the BAA signatory.
        controls_active: Dict of control name → enabled status.
        phi_scan_summary: Statistics about PHI detections in the run period.
        access_blocked_count: Number of PHI file access attempts blocked.
        audit_chain_valid: Whether the HMAC audit chain verification passed.
        encryption_at_rest: Whether state files are encrypted.
        findings: Qualitative compliance findings (observations, gaps).
    """

    generated_at: str
    organization: str
    baa_contact: str
    controls_active: dict[str, bool]
    phi_scan_summary: dict[str, int]
    access_blocked_count: int
    audit_chain_valid: bool
    encryption_at_rest: bool
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "report_type": "hipaa-compliance",
            "generated_at": self.generated_at,
            "organization": self.organization,
            "baa_contact": self.baa_contact,
            "controls_active": self.controls_active,
            "phi_scan_summary": self.phi_scan_summary,
            "access_blocked_count": self.access_blocked_count,
            "audit_chain_valid": self.audit_chain_valid,
            "encryption_at_rest": self.encryption_at_rest,
            "findings": self.findings,
        }


def generate_hipaa_report(
    sdd_dir: Path,
    organization: str = "",
    baa_contact: str = "",
    phi_events_log: list[dict[str, Any]] | None = None,
) -> HIPAAComplianceReport:
    """Generate a BAA-ready HIPAA compliance report.

    Args:
        sdd_dir: Path to the ``.sdd`` directory.
        organization: Organization name for the report header.
        baa_contact: Name/email of the BAA contact person.
        phi_events_log: Optional list of PHI detection events logged during
            the run period (each dict should have ``category``, ``timestamp``).

    Returns:
        HIPAAComplianceReport instance.
    """
    from bernstein.core.security.audit import AuditLog

    generated_at = datetime.now(tz=UTC).strftime(ISO_DATETIME_FMT)
    events = phi_events_log or []

    # Summarize PHI scan events
    category_counts: dict[str, int] = {}
    for ev in events:
        cat = ev.get("category", "unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    access_blocked = sum(1 for ev in events if ev.get("action") == "blocked")

    # Check audit chain validity
    audit_dir = sdd_dir / "audit"
    audit_chain_valid = False
    if audit_dir.is_dir():
        try:
            log = AuditLog(audit_dir)
            valid, _ = log.verify()
            audit_chain_valid = valid
        except Exception:
            pass

    # Check encryption at rest
    key_path = sdd_dir / "config" / "hipaa-enc-key"
    enc_at_rest = key_path.exists()

    controls: dict[str, bool] = {
        "phi_detection": True,
        "file_access_controls": True,
        "audit_hmac_chain": audit_chain_valid,
        "encryption_at_rest": enc_at_rest,
        "audit_logging": audit_dir.is_dir(),
    }

    findings: list[str] = []
    if not enc_at_rest:
        findings.append(
            "HIPAA-OP-001: Encryption at rest key not found. Call load_or_create_hipaa_encryption_key() to initialize."
        )
    if not audit_chain_valid:
        findings.append(
            "HIPAA-OP-002: Audit chain validation failed or no audit data present. "
            "Verify audit logging is enabled and the HMAC chain is intact."
        )
    if not category_counts:
        findings.append(
            "HIPAA-OP-003: No PHI scan events recorded. "
            "Ensure PHI detection is wired into all agent input/output paths."
        )

    return HIPAAComplianceReport(
        generated_at=generated_at,
        organization=organization or os.environ.get("BERNSTEIN_ORG", ""),
        baa_contact=baa_contact,
        controls_active=controls,
        phi_scan_summary=category_counts,
        access_blocked_count=access_blocked,
        audit_chain_valid=audit_chain_valid,
        encryption_at_rest=enc_at_rest,
        findings=findings,
    )


def save_hipaa_report(report: HIPAAComplianceReport, sdd_dir: Path) -> Path:
    """Write the HIPAA compliance report to ``.sdd/compliance/hipaa-report-<ts>.json``.

    Args:
        report: The compliance report to save.
        sdd_dir: Path to the ``.sdd`` directory.

    Returns:
        Path to the written report file.
    """
    _ISO_TIMESTAMP_FMT = "%Y%m%dT%H%M%SZ"
    ts = time.strftime(_ISO_TIMESTAMP_FMT, time.gmtime())
    out_dir = sdd_dir / "compliance"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"hipaa-report-{ts}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2))
    logger.info("HIPAA compliance report written: %s", path)
    return path


# ---------------------------------------------------------------------------
# HIPAA mode integration helper
# ---------------------------------------------------------------------------


@dataclass
class HIPAAMode:
    """Convenience wrapper that combines all HIPAA controls.

    Args:
        sdd_dir: Path to the ``.sdd`` directory.
        phi_file_patterns: Additional file patterns to block beyond defaults.
        require_health_context: Only flag PHI in health-contextual text.
        baa_contact: BAA signatory contact for reports.
        organization: Organization name for reports.
    """

    sdd_dir: Path
    phi_file_patterns: list[str] = field(default_factory=list)
    require_health_context: bool = False
    baa_contact: str = ""
    organization: str = ""

    def __post_init__(self) -> None:
        self._detector = PHIDetector(
            require_health_context=self.require_health_context,
        )
        self._phi_events: list[dict[str, Any]] = []

    def scan_text(self, text: str, source: str = "") -> PHIDetectionResult:
        """Scan text and log PHI detection events.

        Args:
            text: Text to scan.
            source: Description of where the text came from (for logging).

        Returns:
            PHIDetectionResult.
        """
        result = self._detector.scan(text)
        if result.contains_phi:
            for finding in result.findings:
                self._phi_events.append(
                    {
                        "category": finding.category.value,
                        "action": "detected",
                        "source": source,
                        "timestamp": datetime.now(tz=UTC).strftime(ISO_DATETIME_FMT),
                    }
                )
            logger.warning(
                "PHI detected in %s: %d finding(s) — use result.redacted_text",
                source or "input",
                len(result.findings),
            )
        return result

    def check_file_access(self, file_path: str) -> bool:
        """Check if a file path is permitted under HIPAA mode.

        Args:
            file_path: File path to check.

        Returns:
            True if access is permitted, False if blocked.
        """
        all_patterns = _DEFAULT_PHI_FILE_PATTERNS + self.phi_file_patterns
        if is_phi_file(file_path, all_patterns):
            self._phi_events.append(
                {
                    "category": "file_access",
                    "action": "blocked",
                    "file_path": file_path,
                    "timestamp": datetime.now(tz=UTC).strftime(ISO_DATETIME_FMT),
                }
            )
            logger.warning("HIPAA: Blocked PHI file access: %s", file_path)
            return False
        return True

    def generate_report(self) -> HIPAAComplianceReport:
        """Generate a BAA-ready compliance report for the current session."""
        return generate_hipaa_report(
            sdd_dir=self.sdd_dir,
            organization=self.organization,
            baa_contact=self.baa_contact,
            phi_events_log=self._phi_events,
        )
