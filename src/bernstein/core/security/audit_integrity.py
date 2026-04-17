"""ENT-003: Audit log integrity verification on startup.

Verifies the last N audit log entries on orchestrator startup, checking
that the HMAC chain is intact. Warns if any entries have been tampered
with or if the chain is broken.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.security.audit import (
    AUDIT_KEY_ENV,
    AuditKeyPermissionError,
    _default_audit_key_path,
    _enforce_key_permissions,
)

if TYPE_CHECKING:
    from pathlib import Path

_ISO_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

logger = logging.getLogger(__name__)

DEFAULT_VERIFY_COUNT = 100

# HMAC genesis sentinel (must match audit.py's _GENESIS_HMAC).
_GENESIS_HMAC = "0" * 64


@dataclass(frozen=True)
class IntegrityCheckResult:
    """Result of an audit log integrity check.

    Attributes:
        valid: True if all checked entries pass verification.
        entries_checked: Number of entries that were verified.
        entries_total: Total number of entries found across all log files.
        errors: List of human-readable error descriptions.
        warnings: List of non-fatal warnings.
        checked_at: ISO 8601 timestamp of when the check ran.
        duration_ms: Time taken for the check in milliseconds.
    """

    valid: bool
    entries_checked: int
    entries_total: int
    errors: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    checked_at: str = ""
    duration_ms: float = 0.0


def _load_tail_entries(audit_dir: Path, count: int) -> list[tuple[str, int, dict[str, Any]]]:
    """Load the last *count* entries from audit log files.

    Reads files in reverse chronological order (newest first) and
    collects entries until *count* is reached.

    Args:
        audit_dir: Directory containing ``YYYY-MM-DD.jsonl`` files.
        count: Maximum number of entries to load.

    Returns:
        List of ``(filename, line_number, parsed_entry)`` in chronological order.
    """
    log_files = sorted(audit_dir.glob("*.jsonl"), reverse=True)
    if not log_files:
        return []

    collected: list[tuple[str, int, dict[str, Any]]] = []
    for log_path in log_files:
        lines = log_path.read_text().strip().splitlines()
        for line_no in range(len(lines), 0, -1):
            raw = lines[line_no - 1].strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                collected.append((log_path.name, line_no, entry))
            except json.JSONDecodeError:
                collected.append((log_path.name, line_no, {"__parse_error": True}))
            if len(collected) >= count:
                break
        if len(collected) >= count:
            break

    # Return in chronological order (oldest first).
    collected.reverse()
    return collected


def _count_all_entries(audit_dir: Path) -> int:
    """Count total entries across all audit log files.

    Args:
        audit_dir: Directory containing ``YYYY-MM-DD.jsonl`` files.

    Returns:
        Total number of non-empty lines across all log files.
    """
    total = 0
    for log_path in sorted(audit_dir.glob("*.jsonl")):
        for line in log_path.read_text().splitlines():
            if line.strip():
                total += 1
    return total


def _compute_hmac(key: bytes, prev_hmac: str, entry: dict[str, Any]) -> str:
    """Compute HMAC-SHA256 matching audit.py's _compute_hmac."""
    import hashlib
    import hmac

    payload = prev_hmac + json.dumps(entry, sort_keys=True)
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _load_audit_key(audit_dir: Path) -> bytes | None:
    """Load the HMAC key for integrity verification.

    Resolution order:

    1. ``$BERNSTEIN_AUDIT_KEY_PATH`` environment variable.
    2. XDG state default (``~/.local/state/bernstein/audit.key``).
    3. Legacy location ``<audit_dir>/../config/audit-key`` — retained as a
       read-only fallback so systems that have not yet migrated can still
       verify their chain. Permissions are enforced in all cases.

    Args:
        audit_dir: Audit log directory. Used only for the legacy fallback.

    Returns:
        Key bytes, or ``None`` if no key file is found.

    Raises:
        AuditKeyPermissionError: If the key file exists but is readable by
            anyone besides its owner.
    """
    primary = _default_audit_key_path()
    if primary.exists():
        _enforce_key_permissions(primary)
        return primary.read_bytes().strip()

    legacy = audit_dir.parent / "config" / "audit-key"
    if legacy.exists():
        _enforce_key_permissions(legacy)
        logger.warning(
            "Loading audit key from legacy co-located path %s. "
            "Move it to %s (or set %s) to restore tamper-evidence guarantees.",
            legacy,
            primary,
            AUDIT_KEY_ENV,
        )
        return legacy.read_bytes().strip()

    return None


def _verify_entry_chain(
    entries: list[tuple[str, int, dict[str, Any]]],
    key: bytes,
    errors: list[str],
) -> tuple[str | None, int]:
    """Verify HMAC chain linkage and integrity for a list of entries.

    Returns:
        Tuple of (last_hmac, entries_checked).
    """
    prev_hmac: str | None = None
    checked = 0
    for filename, line_no, entry in entries:
        if entry.get("__parse_error"):
            errors.append(f"{filename}:{line_no}: unparseable JSON")
            continue

        stored_hmac = entry.get("hmac", "")
        entry_prev_hmac = entry.get("prev_hmac", "")

        if prev_hmac is not None and entry_prev_hmac != prev_hmac:
            errors.append(
                f"{filename}:{line_no}: chain broken — "
                f"prev_hmac {entry_prev_hmac[:16]}... != expected {prev_hmac[:16]}..."
            )

        payload = {k: v for k, v in entry.items() if k != "hmac"}
        expected_hmac = _compute_hmac(key, entry_prev_hmac, payload)

        if stored_hmac != expected_hmac:
            errors.append(
                f"{filename}:{line_no}: HMAC mismatch — "
                f"stored {stored_hmac[:16]}... != computed {expected_hmac[:16]}..."
            )

        prev_hmac = stored_hmac
        checked += 1
    return prev_hmac, checked


def _log_integrity_result(errors: list[str], checked: int, duration_ms: float) -> None:
    """Log the outcome of an integrity check."""
    if errors:
        logger.warning(
            "Audit integrity check FAILED: %d error(s) in last %d entries",
            len(errors),
            checked,
        )
        for err in errors:
            logger.warning("  %s", err)
    else:
        logger.info(
            "Audit integrity check passed: %d entries verified in %.1fms",
            checked,
            duration_ms,
        )


def verify_audit_integrity(
    audit_dir: Path,
    count: int = DEFAULT_VERIFY_COUNT,
    key: bytes | None = None,
) -> IntegrityCheckResult:
    """Verify the HMAC chain of the last *count* audit log entries.

    Checks:
    1. Each entry's ``prev_hmac`` links to the previous entry's ``hmac``.
    2. Each entry's ``hmac`` matches the recomputed HMAC of its payload.

    If the audit directory does not exist or has no log files, returns
    a valid result with zero entries checked and a warning.

    Args:
        audit_dir: Directory containing audit log files.
        count: Number of tail entries to verify (default 100).
        key: HMAC key bytes. If None, loaded via ``_load_audit_key`` which
            consults ``$BERNSTEIN_AUDIT_KEY_PATH`` and the XDG default.

    Returns:
        IntegrityCheckResult with verification outcome.

    Raises:
        AuditKeyPermissionError: If the key file exists but has insecure
            permissions. This is surfaced to callers so the orchestrator
            refuses to start on a compromised key.
    """
    start = time.monotonic()
    errors: list[str] = []
    warnings: list[str] = []

    if not audit_dir.is_dir():
        return IntegrityCheckResult(
            valid=True,
            entries_checked=0,
            entries_total=0,
            warnings=["Audit directory does not exist; skipping integrity check"],
            checked_at=time.strftime(_ISO_TIMESTAMP_FMT, time.gmtime()),
            duration_ms=0.0,
        )

    if key is None:
        key = _load_audit_key(audit_dir)
    if key is None:
        return IntegrityCheckResult(
            valid=True,
            entries_checked=0,
            entries_total=0,
            warnings=["HMAC key not found; cannot verify audit integrity"],
            checked_at=time.strftime(_ISO_TIMESTAMP_FMT, time.gmtime()),
            duration_ms=0.0,
        )

    entries = _load_tail_entries(audit_dir, count)
    total = _count_all_entries(audit_dir)

    if not entries:
        return IntegrityCheckResult(
            valid=True,
            entries_checked=0,
            entries_total=total,
            warnings=["No audit entries found to verify"],
            checked_at=time.strftime(_ISO_TIMESTAMP_FMT, time.gmtime()),
            duration_ms=(time.monotonic() - start) * 1000,
        )

    # Verify the chain within our window.
    _prev_hmac, checked = _verify_entry_chain(entries, key, errors)

    duration_ms = (time.monotonic() - start) * 1000
    _log_integrity_result(errors, checked, duration_ms)

    return IntegrityCheckResult(
        valid=len(errors) == 0,
        entries_checked=checked,
        entries_total=total,
        errors=errors,
        warnings=warnings,
        checked_at=time.strftime(_ISO_TIMESTAMP_FMT, time.gmtime()),
        duration_ms=duration_ms,
    )


def verify_on_startup(
    sdd_dir: Path,
    count: int = DEFAULT_VERIFY_COUNT,
) -> IntegrityCheckResult:
    """Convenience entry point for orchestrator startup verification.

    Args:
        sdd_dir: The ``.sdd`` directory root.
        count: Number of tail entries to verify.

    Returns:
        IntegrityCheckResult.

    Raises:
        AuditKeyPermissionError: If the audit key file is readable by
            anyone besides its owner. Orchestrator must refuse to start.
    """
    audit_dir = sdd_dir / "audit"
    try:
        result = verify_audit_integrity(audit_dir, count=count)
    except AuditKeyPermissionError:
        logger.exception("AUDIT KEY REJECTED: insecure permissions on HMAC key; refusing to start.")
        raise

    if not result.valid:
        logger.warning(
            "AUDIT INTEGRITY WARNING: %d error(s) detected in the audit log. "
            "The HMAC chain may have been tampered with.",
            len(result.errors),
        )
    return result
