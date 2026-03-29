"""Memory Sanitization Firewall — input moderation for agent memory.

Scans all data entering agent memory for:
- Prompt injection / memory poisoning (complementing memory_integrity patterns)
- PII: SSNs, credit card numbers, email addresses, phone numbers, IP addresses
- Composite trust scoring across all signal categories

Entries with a trust score below ``TRUST_THRESHOLD`` are quarantined rather than
written to the lessons store.  Every quarantine event is appended to
``.sdd/memory/quarantine.jsonl`` for a complete, queryable audit trail.

Usage
-----
::

    from bernstein.core.memory_sanitizer import MemoryFirewall, TRUST_THRESHOLD

    fw = MemoryFirewall(sdd_dir=Path(".sdd"))
    result = fw.scan(
        content="Agent lesson text",
        tags=["auth", "security"],
        source_agent="agent-backend",
        confidence=0.85,
    )
    if result.trust_score < TRUST_THRESHOLD:
        fw.quarantine(entry_dict, result)
    else:
        # proceed to file_lesson()
        ...
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.memory_integrity import detect_memory_poisoning

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRUST_THRESHOLD: float = 0.70
"""Minimum trust score for a memory entry to be accepted.

Entries with ``trust_score < TRUST_THRESHOLD`` are routed to quarantine.
"""

# ---------------------------------------------------------------------------
# PII detection patterns
# ---------------------------------------------------------------------------

# Each rule: (label, compiled pattern, severity_deduction)
# severity_deduction is the amount subtracted from the trust score (0–1 scale).
_PII_RULES: list[tuple[str, re.Pattern[str], float]] = [
    (
        "ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        0.50,
    ),
    (
        "credit_card",
        re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"),
        0.50,
    ),
    (
        "email_address",
        re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"),
        0.20,
    ),
    (
        "phone_number",
        re.compile(r"\b(?:\+\d{1,3}[\s\-])?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
        0.20,
    ),
    (
        "ipv4_address",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
        ),
        0.10,
    ),
    (
        "date_of_birth",
        # MM/DD/YYYY or DD-MM-YYYY style dates that look like DOB fields
        re.compile(
            r"(?i)\bdob\b.*?\b\d{1,2}[/\-]\d{1,2}[/\-]\d{4}\b"
            r"|\b\d{1,2}[/\-]\d{1,2}[/\-]\d{4}\b.*?\bdob\b"
        ),
        0.30,
    ),
    (
        "national_id",
        # Generic patterns: "ID: 123456789" or "passport: AB123456"
        re.compile(r"(?i)\b(?:national[\s_]?id|passport[\s_]?(?:no|number|#))\s*[:\-]?\s*[A-Z0-9]{6,12}\b"),
        0.40,
    ),
]

# ---------------------------------------------------------------------------
# Injection / poisoning pattern deductions (separate from memory_integrity)
# These augment detect_memory_poisoning() with per-deduction weighting.
# ---------------------------------------------------------------------------

_INJECTION_DEDUCTION_PER_SCORE_POINT: float = 0.15
"""Trust deduction per weighted score point from detect_memory_poisoning."""

_MAX_INJECTION_DEDUCTION: float = 0.80
"""Cap on total injection-related trust deduction."""

_UNKNOWN_SOURCE_DEDUCTION: float = 0.10
"""Deduction when source_agent is empty or None — provenance unverifiable."""

_PINNED_CONFIDENCE_DEDUCTION: float = 0.05
"""Deduction when confidence is exactly 1.0 — signals artificial inflation."""

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PiiMatch:
    """A single PII match found in content or tags.

    Attributes:
        label: Human-readable label for the PII type (e.g. ``"ssn"``).
        severity_deduction: Amount deducted from the trust score.
        redacted_excerpt: Up to 40 chars around the match, with the match
            replaced by ``[REDACTED]`` so the audit log never stores raw PII.
    """

    label: str
    severity_deduction: float
    redacted_excerpt: str


@dataclass(frozen=True)
class SanitizationResult:
    """Result of the Memory Sanitization Firewall scan.

    Attributes:
        trust_score: Composite score 0–1.  Values below ``TRUST_THRESHOLD``
            mean the entry should be quarantined, not filed.
        accepted: True when ``trust_score >= TRUST_THRESHOLD``.
        pii_matches: PII patterns found in the content.
        poison_score: Raw weighted score from ``detect_memory_poisoning``.
        poison_rules: Matched injection-rule labels.
        deductions: Itemised list of ``(reason, amount)`` deductions that
            were applied to produce ``trust_score``.
        quarantine_reason: Human-readable summary for quarantine log, or
            empty string when ``accepted`` is True.
        scan_id: UUID identifying this scan for cross-referencing audit logs.
        scanned_at: Unix timestamp of the scan.
    """

    trust_score: float
    accepted: bool
    pii_matches: list[PiiMatch]
    poison_score: int
    poison_rules: list[str]
    deductions: list[tuple[str, float]]
    quarantine_reason: str
    scan_id: str
    scanned_at: float


@dataclass
class QuarantinedMemoryEntry:
    """A memory entry that was rejected by the firewall.

    Attributes:
        quarantine_id: Unique ID for this quarantine event.
        scan_id: ID from the corresponding SanitizationResult.
        source_agent: Agent that attempted to file the entry.
        content_preview: First 200 chars of the content (PII-safe via
            ``redacted_excerpt`` — raw content is never stored here; callers
            must pass pre-redacted text or omit it).
        tags: Tags supplied with the entry.
        confidence: Confidence value supplied by the source agent.
        trust_score: Final trust score that caused rejection.
        quarantine_reason: Human-readable reason.
        deductions: Itemised deductions applied during scoring.
        pii_labels: Labels of detected PII types (not the values).
        poison_rules: Labels of matched injection rules.
        quarantined_at: Unix timestamp.
        quarantined_at_iso: ISO-8601 timestamp for human readability.
    """

    quarantine_id: str
    scan_id: str
    source_agent: str
    content_preview: str
    tags: list[str]
    confidence: float
    trust_score: float
    quarantine_reason: str
    deductions: list[tuple[str, float]]
    pii_labels: list[str]
    poison_rules: list[str]
    quarantined_at: float
    quarantined_at_iso: str


# ---------------------------------------------------------------------------
# Core firewall
# ---------------------------------------------------------------------------


class MemoryFirewall:
    """Input moderation gate for all data entering agent memory.

    All scanning is performed locally — no network calls, no external APIs.
    PII never leaves the process boundary.

    Args:
        sdd_dir: Path to the ``.sdd`` directory.  The quarantine log is
            written to ``<sdd_dir>/memory/quarantine.jsonl``.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._quarantine_path = sdd_dir / "memory" / "quarantine.jsonl"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(
        self,
        content: str,
        tags: list[str],
        source_agent: str,
        confidence: float = 0.8,
    ) -> SanitizationResult:
        """Scan a candidate memory entry and return a trust-scored result.

        Checks are performed entirely in-process; no data leaves the machine.

        Args:
            content: The lesson text to scan.
            tags: Tags associated with the entry.
            source_agent: Agent ID submitting the entry.
            confidence: Declared confidence score (0–1).

        Returns:
            SanitizationResult with trust_score, matches, and
            quarantine_reason.
        """
        scan_id = str(uuid.uuid4())
        scanned_at = time.time()
        deductions: list[tuple[str, float]] = []
        trust = 1.0

        # --- 1. Prompt injection / memory poisoning ---
        poison_result = detect_memory_poisoning(content, tags, confidence)
        if poison_result.score > 0:
            injection_ded = min(
                poison_result.score * _INJECTION_DEDUCTION_PER_SCORE_POINT,
                _MAX_INJECTION_DEDUCTION,
            )
            trust -= injection_ded
            deductions.append((f"injection (score={poison_result.score})", injection_ded))

        # --- 2. PII detection ---
        combined = content + "\n" + " ".join(tags)
        pii_matches = _detect_pii(combined)
        pii_labels_seen: set[str] = set()
        for match in pii_matches:
            if match.label not in pii_labels_seen:
                trust -= match.severity_deduction
                deductions.append((f"pii:{match.label}", match.severity_deduction))
                pii_labels_seen.add(match.label)

        # --- 3. Unknown provenance ---
        if not source_agent or not source_agent.strip():
            trust -= _UNKNOWN_SOURCE_DEDUCTION
            deductions.append(("unknown_source_agent", _UNKNOWN_SOURCE_DEDUCTION))

        # --- 4. Artificially pinned confidence (separate from poison score) ---
        if confidence >= 1.0:
            trust -= _PINNED_CONFIDENCE_DEDUCTION
            deductions.append(("confidence_pinned_at_1.0", _PINNED_CONFIDENCE_DEDUCTION))

        # Clamp to [0, 1]
        trust = max(0.0, min(1.0, trust))
        accepted = trust >= TRUST_THRESHOLD

        if not accepted:
            parts = [f"trust_score={trust:.3f}"]
            if poison_result.is_suspicious:
                parts.append(f"injection:{'; '.join(poison_result.matched_rules)}")
            if pii_matches:
                parts.append(f"pii:{', '.join(m.label for m in pii_matches)}")
            quarantine_reason = "; ".join(parts)
        else:
            quarantine_reason = ""

        return SanitizationResult(
            trust_score=round(trust, 4),
            accepted=accepted,
            pii_matches=pii_matches,
            poison_score=poison_result.score,
            poison_rules=poison_result.matched_rules,
            deductions=deductions,
            quarantine_reason=quarantine_reason,
            scan_id=scan_id,
            scanned_at=scanned_at,
        )

    def quarantine(
        self,
        result: SanitizationResult,
        source_agent: str,
        content: str,
        tags: list[str],
        confidence: float,
    ) -> str:
        """Append a rejected entry to the quarantine log.

        The content stored in the log is capped at 200 chars and PII
        excerpts are already redacted by the scanner — raw PII is never
        written to disk.

        Args:
            result: The SanitizationResult from ``scan()``.
            source_agent: Agent that submitted the entry.
            content: Original content (stored as a truncated preview only).
            tags: Tags associated with the entry.
            confidence: Declared confidence score.

        Returns:
            The quarantine_id for the new log entry.
        """
        now = result.scanned_at
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

        # Build a safe content preview — redact any PII spans
        preview = _redact_pii(content)[:200]

        entry = QuarantinedMemoryEntry(
            quarantine_id=str(uuid.uuid4()),
            scan_id=result.scan_id,
            source_agent=source_agent,
            content_preview=preview,
            tags=list(tags),
            confidence=confidence,
            trust_score=result.trust_score,
            quarantine_reason=result.quarantine_reason,
            deductions=result.deductions,
            pii_labels=list({m.label for m in result.pii_matches}),
            poison_rules=result.poison_rules,
            quarantined_at=now,
            quarantined_at_iso=iso,
        )

        self._quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._quarantine_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry)) + "\n")
        except OSError as exc:
            logger.error("Failed to write quarantine entry: %s", exc)
            raise

        logger.warning(
            "Memory entry quarantined [qid=%s scan=%s agent=%s trust=%.3f]: %s",
            entry.quarantine_id,
            result.scan_id,
            source_agent,
            result.trust_score,
            result.quarantine_reason,
        )
        return entry.quarantine_id

    def load_quarantine(self) -> list[QuarantinedMemoryEntry]:
        """Return all quarantined entries from the audit log.

        Returns:
            List of QuarantinedMemoryEntry objects in append order.
        """
        if not self._quarantine_path.exists():
            return []

        entries: list[QuarantinedMemoryEntry] = []
        try:
            with open(self._quarantine_path, encoding="utf-8") as f:
                for lineno, raw in enumerate(f, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        data: dict[str, Any] = json.loads(raw)
                        entries.append(
                            QuarantinedMemoryEntry(
                                quarantine_id=str(data.get("quarantine_id", "")),
                                scan_id=str(data.get("scan_id", "")),
                                source_agent=str(data.get("source_agent", "")),
                                content_preview=str(data.get("content_preview", "")),
                                tags=list(data.get("tags", [])),
                                confidence=float(data.get("confidence", 0.0)),
                                trust_score=float(data.get("trust_score", 0.0)),
                                quarantine_reason=str(data.get("quarantine_reason", "")),
                                deductions=list(data.get("deductions", [])),
                                pii_labels=list(data.get("pii_labels", [])),
                                poison_rules=list(data.get("poison_rules", [])),
                                quarantined_at=float(data.get("quarantined_at", 0.0)),
                                quarantined_at_iso=str(data.get("quarantined_at_iso", "")),
                            )
                        )
                    except (json.JSONDecodeError, KeyError, ValueError) as exc:
                        logger.debug("Skipped malformed quarantine entry at line %d: %s", lineno, exc)
        except OSError as exc:
            logger.warning("Failed to read quarantine log: %s", exc)

        return entries


# ---------------------------------------------------------------------------
# Convenience function: scan-and-gate
# ---------------------------------------------------------------------------


def sanitize_memory_entry(
    sdd_dir: Path,
    content: str,
    tags: list[str],
    source_agent: str,
    confidence: float = 0.8,
) -> SanitizationResult:
    """Scan a memory entry and quarantine it if below the trust threshold.

    This is the primary entrypoint for callers.  It creates a ``MemoryFirewall``,
    runs the scan, and automatically writes to the quarantine log if the entry is
    rejected.

    Args:
        sdd_dir: Path to the ``.sdd`` directory.
        content: Lesson content to scan.
        tags: Tags for the entry.
        source_agent: Agent submitting the entry.
        confidence: Declared confidence score.

    Returns:
        SanitizationResult — check ``.accepted`` before proceeding to
        ``file_lesson()``.
    """
    fw = MemoryFirewall(sdd_dir)
    result = fw.scan(content, tags, source_agent, confidence)

    if not result.accepted:
        fw.quarantine(result, source_agent, content, tags, confidence)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_pii(text: str) -> list[PiiMatch]:
    """Scan *text* for PII patterns; return one match per rule category.

    Only the first match per rule category is returned.  The matched span is
    replaced by ``[REDACTED]`` in the excerpt stored in the result so that
    no raw PII enters the audit log.

    Args:
        text: Combined content + tags string to scan.

    Returns:
        List of PiiMatch objects, one per triggered rule.
    """
    matches: list[PiiMatch] = []
    for label, pattern, severity in _PII_RULES:
        m = pattern.search(text)
        if m:
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            excerpt_raw = text[start:end]
            # Replace the exact match with [REDACTED]
            relative_start = m.start() - start
            relative_end = m.end() - start
            excerpt = excerpt_raw[:relative_start] + "[REDACTED]" + excerpt_raw[relative_end:]
            matches.append(PiiMatch(label=label, severity_deduction=severity, redacted_excerpt=excerpt))
    return matches


def _redact_pii(text: str) -> str:
    """Replace all PII matches in *text* with ``[REDACTED]``.

    Used to produce safe content previews for the quarantine log.

    Args:
        text: Raw content string.

    Returns:
        Text with all detected PII spans replaced by ``[REDACTED]``.
    """
    result = text
    for _label, pattern, _severity in _PII_RULES:
        result = pattern.sub("[REDACTED]", result)
    return result
