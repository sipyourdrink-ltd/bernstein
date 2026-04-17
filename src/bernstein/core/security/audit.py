"""Immutable HMAC-chained audit log.

Every audit event carries an HMAC that chains to the previous event's HMAC,
forming a tamper-evident sequence.  Daily log rotation produces one JSONL
file per day; the chain carries across file boundaries.

Security (audit-043): the HMAC key lives OUTSIDE the audit log directory so
an attacker with write access to ``.sdd/audit/*.jsonl`` cannot also read or
rotate the signing key. The default key location is
``$XDG_STATE_HOME/bernstein/audit.key`` (falling back to
``~/.local/state/bernstein/audit.key``) and is overridable via the
``BERNSTEIN_AUDIT_KEY_PATH`` environment variable. The key file is required
to be mode ``0600``; a world- or group-readable key is treated as a hard
error at load time.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import hmac as _hmac
import json
import logging
import os
import secrets
import shutil
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_JSONL_GLOB = "*.jsonl"

logger = logging.getLogger(__name__)

_GENESIS_HMAC = "0" * 64

DEFAULT_RETENTION_DAYS = 90

#: Environment variable that overrides the audit key path.
AUDIT_KEY_ENV = "BERNSTEIN_AUDIT_KEY_PATH"

#: Required mode for the audit key file (0600 — owner read/write only).
_REQUIRED_KEY_MODE = 0o600


class AuditKeyPermissionError(RuntimeError):
    """Raised when the audit key file has permissions looser than 0600."""


def _default_audit_key_path() -> Path:
    """Return the default HMAC key path outside of ``.sdd/``.

    Resolution order:

    1. ``$BERNSTEIN_AUDIT_KEY_PATH`` (explicit override).
    2. ``$XDG_STATE_HOME/bernstein/audit.key`` if ``XDG_STATE_HOME`` is set.
    3. ``~/.local/state/bernstein/audit.key`` (XDG default).
    """
    override = os.environ.get(AUDIT_KEY_ENV)
    if override:
        return Path(override).expanduser()

    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return base / "bernstein" / "audit.key"


def _enforce_key_permissions(key_path: Path) -> None:
    """Ensure the key file is readable only by its owner (mode 0600).

    Raises:
        AuditKeyPermissionError: If group or world bits are set on the file.
    """
    try:
        file_mode = stat.S_IMODE(key_path.stat().st_mode)
    except OSError as exc:  # pragma: no cover - filesystem race
        raise AuditKeyPermissionError(f"Cannot stat audit key {key_path}: {exc}") from exc

    if file_mode & 0o077:
        raise AuditKeyPermissionError(
            f"Audit key {key_path} has insecure permissions {file_mode:04o}; "
            f"required {_REQUIRED_KEY_MODE:04o} (owner-only)."
        )


def load_or_create_audit_key(key_path: Path | None = None) -> bytes:
    """Load the audit HMAC key, generating one on first boot if absent.

    The key path is resolved by the following precedence:

    1. Explicit ``key_path`` argument.
    2. ``$BERNSTEIN_AUDIT_KEY_PATH`` environment variable.
    3. ``$XDG_STATE_HOME/bernstein/audit.key`` (or the XDG default).

    On first boot, a fresh 32-byte hex key is generated, the parent directory
    is created with mode ``0700``, and the key file is written with mode
    ``0600``. On subsequent boots the existing permissions are enforced.

    Args:
        key_path: Optional explicit override. Useful for tests.

    Returns:
        The raw key bytes suitable for ``hmac.new``.

    Raises:
        AuditKeyPermissionError: If the existing key file is readable by
            anyone besides its owner.
    """
    resolved = key_path if key_path is not None else _default_audit_key_path()

    if resolved.exists():
        _enforce_key_permissions(resolved)
        return resolved.read_bytes().strip()

    parent = resolved.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Best-effort harden the directory: owner-only if we just created it.
    with contextlib.suppress(PermissionError, OSError):
        parent.chmod(0o700)

    key = secrets.token_hex(32).encode()
    # Create with restrictive mode from the start — never widen then narrow.
    fd = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _REQUIRED_KEY_MODE)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    # Re-assert mode in case umask or filesystem behavior dropped bits.
    resolved.chmod(_REQUIRED_KEY_MODE)
    logger.info("Generated new audit HMAC key at %s", resolved)
    return key


@dataclass(frozen=True)
class RetentionPolicy:
    """Configurable audit log retention and auto-archive settings.

    Attributes:
        retention_days: Number of days to keep uncompressed log files.
            Logs older than this are compressed and moved to the archive.
        archive_subdir: Name of the subdirectory under audit_dir for archives.
    """

    retention_days: int = DEFAULT_RETENTION_DAYS
    archive_subdir: str = "archive"


@dataclass(frozen=True)
class ArchiveResult:
    """Result of an archive operation.

    Attributes:
        archived: List of original log file names that were archived.
        archive_dir: Path to the archive directory.
        skipped: List of file names skipped (already archived or too recent).
    """

    archived: list[str] = field(default_factory=list)
    archive_dir: str = ""
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AuditEvent:
    """A single HMAC-chained audit log entry.

    Attributes:
        timestamp: ISO 8601 timestamp of the event.
        event_type: Category of the event (e.g. "task.transition").
        actor: Who/what triggered the event.
        resource_type: Type of resource affected (e.g. "task", "agent").
        resource_id: ID of the affected resource.
        details: Arbitrary structured data about the event.
        prev_hmac: HMAC of the preceding event in the chain.
        hmac: HMAC of this event (covers all fields above).
    """

    timestamp: str
    event_type: str
    actor: str
    resource_type: str
    resource_id: str
    details: dict[str, Any] = field(default_factory=dict)
    prev_hmac: str = _GENESIS_HMAC
    hmac: str = ""


def _compute_hmac(key: bytes, prev_hmac: str, entry: dict[str, Any]) -> str:
    """Compute HMAC-SHA256 over the previous HMAC concatenated with the canonical JSON payload."""
    payload = prev_hmac + json.dumps(entry, sort_keys=True)
    return _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _verify_log_file(log_path: Path, prev_hmac: str, key: bytes, errors: list[str]) -> str:
    """Verify all entries in a single JSONL log file, appending errors."""
    for line_no, raw in enumerate(log_path.read_text().splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"{log_path.name}:{line_no}: invalid JSON — {exc}")
            continue

        stored_hmac = entry.pop("hmac", "")
        if entry.get("prev_hmac") != prev_hmac:
            errors.append(
                f"{log_path.name}:{line_no}: prev_hmac mismatch "
                f"(expected {prev_hmac[:16]}…, got {str(entry.get('prev_hmac', ''))[:16]}…)"
            )

        expected_hmac = _compute_hmac(key, prev_hmac, entry)
        if stored_hmac != expected_hmac:
            errors.append(
                f"{log_path.name}:{line_no}: HMAC mismatch (expected {expected_hmac[:16]}…, got {stored_hmac[:16]}…)"
            )

        prev_hmac = stored_hmac
    return prev_hmac


def _matches_query_filters(
    entry: dict[str, Any],
    event_type: str | None,
    actor: str | None,
    since: str | None,
    until: str | None,
) -> bool:
    """Return True if entry passes all query filters."""
    if event_type and entry.get("event_type") != event_type:
        return False
    if actor and entry.get("actor") != actor:
        return False
    ts = entry.get("timestamp", "")
    if since and ts < since:
        return False
    return not (until and ts > until)


class AuditLog:
    """Append-only HMAC-chained audit log with daily rotation.

    Args:
        audit_dir: Directory for daily JSONL log files.
        key: HMAC key bytes.  If ``None``, the key is loaded from the path
            resolved by :func:`load_or_create_audit_key` — which by default
            lives *outside* ``audit_dir`` so a log-writer cannot also read
            or rotate the signing key.
        key_path: Optional explicit key file path. Overrides the environment
            variable ``BERNSTEIN_AUDIT_KEY_PATH``. Ignored if ``key`` is
            provided directly.

    Raises:
        AuditKeyPermissionError: If the resolved key file exists on disk but
            is readable by anyone besides its owner.
    """

    def __init__(
        self,
        audit_dir: Path,
        key: bytes | None = None,
        *,
        key_path: Path | None = None,
    ) -> None:
        self._audit_dir = audit_dir
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        if key is not None:
            self._key = key
        else:
            self._key = load_or_create_audit_key(key_path)
        self._prev_hmac = self._recover_chain_tail()

    # -- chain recovery -----------------------------------------------------

    def _recover_chain_tail(self) -> str:
        """Walk existing logs to find the last HMAC in the chain."""
        log_files = sorted(self._audit_dir.glob(_JSONL_GLOB))
        if not log_files:
            return _GENESIS_HMAC
        last_file = log_files[-1]
        lines = last_file.read_text().strip().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and "hmac" in entry:
                    return str(entry["hmac"])
            except (json.JSONDecodeError, KeyError):
                pass  # Malformed audit line; continue scanning
        return _GENESIS_HMAC

    # -- write --------------------------------------------------------------

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Create an audit event, compute its HMAC, and append to the daily log.

        Args:
            event_type: Category of the event.
            actor: Who/what triggered the event.
            resource_type: Type of resource affected.
            resource_id: ID of the affected resource.
            details: Optional structured data about the event.

        Returns:
            The newly created AuditEvent with computed HMAC.
        """
        ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        entry_dict: dict[str, Any] = {
            "timestamp": ts,
            "event_type": event_type,
            "actor": actor,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "details": details or {},
            "prev_hmac": self._prev_hmac,
        }
        computed_hmac = _compute_hmac(self._key, self._prev_hmac, entry_dict)

        event = AuditEvent(
            timestamp=ts,
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            prev_hmac=self._prev_hmac,
            hmac=computed_hmac,
        )

        entry_dict["hmac"] = computed_hmac
        day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        log_path = self._audit_dir / f"{day}.jsonl"

        with log_path.open("a") as fh:
            fh.write(json.dumps(entry_dict, sort_keys=True) + "\n")

        self._prev_hmac = computed_hmac
        return event

    # -- verify -------------------------------------------------------------

    def verify(self) -> tuple[bool, list[str]]:
        """Walk all JSONL files and verify the HMAC chain.

        Returns:
            ``(valid, errors)`` where *valid* is True when the entire chain
            is intact and *errors* lists any violations found.
        """
        errors: list[str] = []
        log_files = sorted(self._audit_dir.glob(_JSONL_GLOB))
        if not log_files:
            return True, []

        prev_hmac = _GENESIS_HMAC
        for log_path in log_files:
            prev_hmac = _verify_log_file(log_path, prev_hmac, self._key, errors)

        return len(errors) == 0, errors

    # -- retention & archive ------------------------------------------------

    def archive(self, policy: RetentionPolicy | None = None) -> ArchiveResult:
        """Compress and archive log files older than the retention window.

        Files whose date (parsed from the ``YYYY-MM-DD.jsonl`` filename) is
        older than ``policy.retention_days`` are gzip-compressed into the
        archive subdirectory.  The original ``.jsonl`` file is removed after
        a successful compress.

        Args:
            policy: Retention settings.  Uses defaults if ``None``.

        Returns:
            An ``ArchiveResult`` describing what was archived.
        """
        policy = policy or RetentionPolicy()
        archive_dir = self._audit_dir / policy.archive_subdir
        archive_dir.mkdir(parents=True, exist_ok=True)

        cutoff = datetime.now(tz=UTC).date() - timedelta(days=policy.retention_days)

        archived: list[str] = []
        skipped: list[str] = []

        for log_path in sorted(self._audit_dir.glob(_JSONL_GLOB)):
            stem = log_path.stem  # e.g. "2025-12-01"
            try:
                file_date = datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=UTC).date()
            except ValueError:
                skipped.append(log_path.name)
                continue

            if file_date >= cutoff:
                skipped.append(log_path.name)
                continue

            gz_path = archive_dir / f"{log_path.name}.gz"
            if gz_path.exists():
                skipped.append(log_path.name)
                continue

            with log_path.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

            log_path.unlink()
            archived.append(log_path.name)
            logger.info("Archived audit log %s → %s", log_path.name, gz_path.name)

        return ArchiveResult(
            archived=archived,
            archive_dir=str(archive_dir),
            skipped=skipped,
        )

    # -- query --------------------------------------------------------------

    def query(
        self,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[AuditEvent]:
        """Filter audit events by type, actor, and/or time range.

        Args:
            event_type: If set, only return events matching this type.
            actor: If set, only return events from this actor.
            since: ISO 8601 lower bound (inclusive).
            until: ISO 8601 upper bound (inclusive).

        Returns:
            List of matching AuditEvent instances (chronological order).
        """
        results: list[AuditEvent] = []
        log_files = sorted(self._audit_dir.glob(_JSONL_GLOB))

        for log_path in log_files:
            for raw in log_path.read_text().splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if not _matches_query_filters(entry, event_type, actor, since, until):
                    continue

                results.append(
                    AuditEvent(
                        timestamp=entry.get("timestamp", ""),
                        event_type=entry.get("event_type", ""),
                        actor=entry.get("actor", ""),
                        resource_type=entry.get("resource_type", ""),
                        resource_id=entry.get("resource_id", ""),
                        details=entry.get("details", {}),
                        prev_hmac=entry.get("prev_hmac", ""),
                        hmac=entry.get("hmac", ""),
                    )
                )

        return results
