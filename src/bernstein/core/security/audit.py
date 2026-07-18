"""Immutable HMAC-chained audit log.

Every audit event carries an HMAC that chains to the previous event's HMAC,
forming a tamper-evident sequence.  Daily log rotation produces one JSONL
file per day; the chain carries across file boundaries.

Security: the HMAC key lives OUTSIDE the audit log directory so
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
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

_JSONL_GLOB = "*.jsonl"

#: Glob for archived (gzip-compressed) daily segments under the archive
#: subdirectory.  ``archive`` writes ``<YYYY-MM-DD>.jsonl.gz``; the verifier
#: and chain-recovery paths treat these as first-class chain links rather
#: than out-of-band cold storage (issue #1835).
_ARCHIVED_GLOB = "*.jsonl.gz"

logger = logging.getLogger(__name__)

_GENESIS_HMAC = "0" * 64

DEFAULT_RETENTION_DAYS = 90

#: Environment variable that overrides the audit key path.
AUDIT_KEY_ENV = "BERNSTEIN_AUDIT_KEY_PATH"

#: Required mode for the audit key file (0600 - owner read/write only).
_REQUIRED_KEY_MODE = 0o600

# ---------------------------------------------------------------------------
# Audit event-type constants
# ---------------------------------------------------------------------------
# Canonical event-type strings emitted into the HMAC-chained log.  Centralised
# here so producers and consumers reference the same identifiers.

#: Issue #1109 - emitted whenever a retry spawns a fresh agent process with
#: no accumulated state because the task opted into
#: ``agent_restart_between_retries``.
AGENT_FRESH_RESTART_ON_RETRY = "agent_fresh_restart_on_retry"

#: Emitted once per spawn with the resolved response-style profile and the
#: SHA-256 of the rendered style addendum, so the profile applied to a task
#: is reconstructable from the audit chain alongside its cost ledger entry.
TASK_RESPONSE_PROFILE = "task_response_profile"

#: Issue #1799 - emitted once per step appended to an agent's hash-chained
#: replay journal. Carries the step ``seq`` and ``step_hash`` in details so
#: the audit-slice extractor can correlate audit events to journal entries
#: without rehashing the chain.
REPLAY_STEP = "replay.step"

#: Issue #1799 - emitted when ``session fork --from-step`` materialises a
#: sibling worktree branched at a per-step parent hash. The chain becomes
#: a tree at this event-type; details carry parent + child step hashes.
REPLAY_FORK = "replay.fork"

#: Issue #1799 - emitted when ``replay export`` writes a portable receipt
#: to disk. Details carry the receipt head hash and step count.
REPLAY_EXPORT = "replay.export"

#: Issue #1799 - emitted when ``replay publish`` writes a redacted receipt
#: outside ``.sdd/runtime/``. Details carry both the original and the
#: re-anchored (redacted) head hashes so the audit trail records the
#: privacy transform.
REPLAY_PUBLISH = "replay.publish"

#: Issue #2162 - emitted when a per-agent sandbox session is provisioned.
#: Details carry the sandbox session id, image, and backend name.
SANDBOX_SESSION_CREATE = "sandbox.session_create"

#: Issue #2162 - emitted when an adapter command is submitted to a sandbox
#: session. Details carry the sandbox session id, adapter name, and a
#: SHA-256 hash of the command (the raw argv embeds prompt paths and model
#: names; the hash keeps the chain verifiable without recording them).
SANDBOX_EXEC_START = "sandbox.exec_start"

#: Issue #2162 - emitted when a sandbox exec future resolves. Details carry
#: the sandbox session id and the exit code (or ``cancelled``/``error``).
SANDBOX_EXEC_END = "sandbox.exec_end"

#: Issue #2162 - emitted when a per-agent sandbox session is destroyed.
SANDBOX_SESSION_DESTROY = "sandbox.session_destroy"


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

    Skipped on Windows, where POSIX permission bits do not apply: NTFS
    enforces access through ACLs rather than ``chmod``-style modes, and
    ``Path.stat().st_mode`` returns ``0o666`` for any user-owned file
    regardless of the actual ACL. Enforcing the POSIX rule there would
    flag every Windows install as insecure for no security reason.

    Raises:
        AuditKeyPermissionError: If group or world bits are set on the
            file (POSIX systems only).
    """
    if sys.platform == "win32":
        return

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
    # Create with restrictive mode from the start - never widen then narrow.
    try:
        fd = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _REQUIRED_KEY_MODE)
    except FileExistsError:
        # Another thread/process won the first-boot race between the
        # exists() check above and this O_EXCL create. Their key is as
        # good as ours; adopt it instead of failing the caller.
        _enforce_key_permissions(resolved)
        return resolved.read_bytes().strip()
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    # Re-assert mode in case umask or filesystem behavior dropped bits.
    resolved.chmod(_REQUIRED_KEY_MODE)
    logger.info("Generated new audit HMAC key at %s", resolved)
    return key


def _empty_str_list() -> list[str]:
    return []


def _empty_details() -> dict[str, Any]:
    return {}


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

    archived: list[str] = field(default_factory=_empty_str_list)
    archive_dir: str = ""
    skipped: list[str] = field(default_factory=_empty_str_list)


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
    details: dict[str, Any] = field(default_factory=_empty_details)
    prev_hmac: str = _GENESIS_HMAC
    hmac: str = ""


def _compute_hmac(key: bytes, prev_hmac: str, entry: dict[str, Any]) -> str:
    """Compute HMAC-SHA256 over the previous HMAC concatenated with the canonical JSON payload."""
    payload = prev_hmac + json.dumps(entry, sort_keys=True)
    return _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _split_jsonl_bytes(raw_bytes: bytes) -> list[bytes]:
    """Strictly split a JSONL file's raw bytes on ``b"\\n"`` only.

    ``str.splitlines()`` treats ``\\n``, ``\\r``, ``\\v``, ``\\f``, NEL, and
    the unicode line/paragraph separators as equivalent; flipping a single
    byte of the terminator (e.g. ``0x0A`` → ``0x0B``) leaves the lines
    intact at the parser layer, which would defeat tamper-evidence. By
    splitting on ``b"\\n"`` only and refusing to accept any other line
    separator we surface that flip as either a malformed-bytes line or a
    canonical-form mismatch downstream.
    """
    parts = raw_bytes.split(b"\n")
    # ``write_bytes(json.dumps(...) + "\n")`` always ends the file with a
    # newline → split() yields an empty trailing element which we drop.
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def _verify_log_bytes(
    raw_bytes: bytes,
    display_name: str,
    prev_hmac: str,
    key: bytes,
    errors: list[str],
) -> str:
    """Verify the JSONL entries in ``raw_bytes``, appending errors.

    ``display_name`` is the name used in every error message so callers can
    point at either a live ``*.jsonl`` file or an archived ``*.jsonl.gz``
    segment.  Verification is byte-for-byte identical for both: an archived
    segment is decompressed to its original bytes and run through the same
    canonicalisation, ``prev_hmac`` linkage, and HMAC checks, so archived
    history stays exactly as tamper-evident as live history (issue #1835).
    """
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        # The writer always terminates with ``\n``; absence is itself
        # tamper-evidence (e.g. ``\n`` flipped to ``\v`` at EOF). Continue
        # into the per-line loop so a truncated last record is still
        # surfaced as ``invalid JSON`` for callers that key on that
        # message (test_partial_last_line_flagged_as_invalid_json).
        errors.append(f"{display_name}: missing trailing newline")

    for line_no, raw_line in enumerate(_split_jsonl_bytes(raw_bytes), start=1):
        if raw_line == b"":
            continue
        try:
            parsed_entry = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append(f"{display_name}:{line_no}: invalid JSON - {exc}")
            continue
        if not isinstance(parsed_entry, dict):
            errors.append(f"{display_name}:{line_no}: entry is not a JSON object")
            continue
        entry = cast("dict[str, Any]", parsed_entry)

        # Tamper-evidence beyond JSON: ``json.loads`` accepts incidental
        # whitespace (e.g. a trailing ``\r`` after ``}``) which would
        # silently survive a single-byte flip of the line terminator. We
        # re-canonicalise and require a byte-for-byte match against the
        # on-disk line, so any non-canonical bytes inside the line are
        # surfaced as a verification failure.
        canonical = json.dumps(entry, sort_keys=True).encode()
        if canonical != raw_line:
            errors.append(f"{display_name}:{line_no}: non-canonical line bytes")
            continue

        stored_hmac = str(entry.pop("hmac", ""))
        entry_prev = str(entry.get("prev_hmac", ""))
        # Constant-time compare on the chain link - verification is offline
        # but a leaky compare in audit code is a CodeQL/Bandit smell and
        # masks regressions when the same helper is later reused on a
        # network surface.
        if not _hmac.compare_digest(entry_prev, prev_hmac):
            errors.append(
                f"{display_name}:{line_no}: prev_hmac mismatch (expected {prev_hmac[:16]}…, got {entry_prev[:16]}…)"
            )

        expected_hmac = _compute_hmac(key, prev_hmac, entry)
        if not _hmac.compare_digest(stored_hmac, expected_hmac):
            errors.append(
                f"{display_name}:{line_no}: HMAC mismatch (expected {expected_hmac[:16]}…, got {stored_hmac[:16]}…)"
            )

        prev_hmac = stored_hmac
    return prev_hmac


def _verify_log_file(log_path: Path, prev_hmac: str, key: bytes, errors: list[str]) -> str:
    """Verify all entries in a single live JSONL log file, appending errors."""
    return _verify_log_bytes(log_path.read_bytes(), log_path.name, prev_hmac, key, errors)


def _read_archived_segment(gz_path: Path, errors: list[str]) -> bytes | None:
    """Decompress an archived ``*.jsonl.gz`` segment to its original bytes.

    A truncated or corrupt archive (e.g. a crash mid-``archive``) degrades to
    a clear, named error rather than an uncaught ``gzip``/``OSError``
    traceback, keeping ``verify`` a total function over a possibly-damaged
    archive directory.

    Returns:
        The decompressed bytes, or ``None`` if the segment is unreadable
        (in which case an error has been appended to ``errors``).
    """
    try:
        with gzip.open(gz_path, "rb") as fh:
            return fh.read()
    except (OSError, EOFError) as exc:
        errors.append(f"{gz_path.name}: unreadable archived segment - {exc}")
        return None


def _chain_tail_from_bytes(raw_bytes: bytes) -> str | None:
    """Return the last ``hmac`` in ``raw_bytes`` (scanning in reverse), or None.

    Shared by live-file and archived-segment chain recovery so both resolve
    the tip with the *same byte-strict framing the verifier uses*.  The bytes
    are split on ``b"\\n"`` only (via :func:`_split_jsonl_bytes`), never with
    ``str.splitlines()``; the latter treats ``\\v``, ``\\f``, ``\\r``, NEL,
    and the unicode line/paragraph separators as record boundaries, so an
    inter-line ``\\n`` -> ``\\v`` flip (the mutation pinned by
    ``test_interline_newline_flip_is_detected``) would be split into two
    clean records by recovery while the verifier glues them into one
    malformed line and rejects the chain.  Recovery must agree with the
    verifier on where records begin and end, otherwise a fresh ``AuditLog``
    could resume from a tail ``verify()`` already considers tampered and keep
    appending valid-HMAC events onto a broken chain (issue #1853).

    A candidate record qualifies as the tip only if it parses cleanly *and*
    its bytes equal the canonical re-serialisation
    (``json.dumps(entry, sort_keys=True)``) - the identical check
    :func:`_verify_log_bytes` applies - *and* it is a JSON object carrying an
    ``hmac`` field.  Records that are blank, malformed, non-canonical, or not
    an ``hmac``-bearing object are skipped, so recovery falls back to the last
    byte-strict-valid record.  This tolerance keeps the genuine crash-recovery
    case working: a truncated final line (writer crash mid-write, no trailing
    ``\\n``) is skipped and recovery resumes from the last well-formed record,
    exactly as the legitimate truncation path does today.
    """
    for raw_line in reversed(_split_jsonl_bytes(raw_bytes)):
        if raw_line == b"":
            continue
        try:
            parsed_entry = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # ``json.loads`` on raw bytes raises ``UnicodeDecodeError`` (not
            # ``JSONDecodeError``) when a flipped byte yields invalid UTF-8;
            # both mean the record is unusable, so skip it and keep scanning
            # rather than letting the decode error wedge ``AuditLog`` startup.
            continue
        if not isinstance(parsed_entry, dict):
            continue
        entry = cast("dict[str, Any]", parsed_entry)
        # Mirror the verifier's byte-for-byte canonical check so recovery and
        # verification agree on record framing: a single-byte tamper that
        # survives ``json.loads`` (e.g. injected whitespace) is non-canonical
        # and is skipped rather than adopted as the tail.
        if json.dumps(entry, sort_keys=True).encode() != raw_line:
            continue
        if "hmac" in entry:
            return str(entry["hmac"])
    return None


def _archived_segment_paths(audit_dir: Path, policy: RetentionPolicy | None = None) -> list[Path]:
    """Return archived ``*.jsonl.gz`` segments ordered by their embedded date.

    Ordering is load-bearing: the verifier must replay archived segments in
    chronological order *before* the live ``*.jsonl`` files so ``prev_hmac``
    linkage is continuous from genesis to tail.  The sort key is the
    ``YYYY-MM-DD`` date parsed from the filename (``<date>.jsonl.gz``); the
    full name is the tie-breaker so two segments that somehow share a date
    still order deterministically.  Files whose name does not start with a
    parseable date sort last (by name) so a hand-renamed archive cannot
    silently jump ahead of dated segments and forge a false ordering.
    """
    policy = policy or RetentionPolicy()
    archive_dir = audit_dir / policy.archive_subdir
    if not archive_dir.is_dir():
        return []

    def _date_key(path: Path) -> tuple[int, str, str]:
        # ``<date>.jsonl.gz`` -> stem ``<date>.jsonl`` -> ``Path.stem`` again
        # is brittle, so derive the date token from the leading filename part.
        date_token = path.name.split(".", 1)[0]
        try:
            datetime.strptime(date_token, "%Y-%m-%d")
        except ValueError:
            # Undated/renamed segments sort after all dated ones.
            return (1, "", path.name)
        return (0, date_token, path.name)

    return sorted(archive_dir.glob(_ARCHIVED_GLOB), key=_date_key)


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
            resolved by :func:`load_or_create_audit_key` - which by default
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
        """Walk existing logs in reverse to find the last valid HMAC.

        Walks every live ``*.jsonl`` file from newest to oldest, then falls
        back to archived ``*.jsonl.gz`` segments (also newest-date first),
        scanning each segment under the *same byte-strict ``b"\\n"`` framing
        the verifier uses* (see :func:`_chain_tail_from_bytes`), and returns
        the first record that parses to a canonical JSON object carrying an
        ``hmac`` field.  Using the verifier's framing means recovery cannot
        adopt a tail ``verify()`` would reject - e.g. an inter-line ``\\n``
        flipped to ``\\v`` - and silently keep appending onto a broken chain
        (issue #1853).  Earlier-only inspection of the lex-last file would
        silently fork the chain when that file is empty/truncated (e.g. a
        freshly-rotated day with no events yet, or a writer crash mid-line) -
        see test_truncated_last_file_does_not_fork_chain in
        tests/property/test_audit_chain_bughunt.py.  Including archived
        segments means a writer reopening a log whose only events have aged
        into the archive resumes from the true tip instead of forking back
        to genesis (issue #1835).
        """
        live_files = sorted(self._audit_dir.glob(_JSONL_GLOB), reverse=True)
        for log_path in live_files:
            tip = _chain_tail_from_bytes(log_path.read_bytes())
            if tip is not None:
                return tip

        archived_files = _archived_segment_paths(self._audit_dir)
        archived_files.reverse()
        for gz_path in archived_files:
            try:
                with gzip.open(gz_path, "rb") as fh:
                    raw = fh.read()
            except (OSError, EOFError):
                # A corrupt archived segment cannot yield a trustworthy tip;
                # skip it and keep looking at older segments.
                continue
            tip = _chain_tail_from_bytes(raw)
            if tip is not None:
                return tip
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

        # ``newline=""`` disables Python's universal-newline translation so
        # the literal ``\n`` we append survives byte-for-byte on Windows
        # (where text mode would otherwise rewrite it to ``\r\n``). The
        # verifier reads bytes and re-canonicalises against ``\n``-only
        # frames; without this the ``\r`` stays inside each split line and
        # the byte-equality tamper check trips.
        with log_path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(entry_dict, sort_keys=True) + "\n")

        self._prev_hmac = computed_hmac
        return event

    # -- verify -------------------------------------------------------------

    def verify(self) -> tuple[bool, list[str]]:
        """Walk archived then live JSONL segments and verify the HMAC chain.

        Archived ``*.jsonl.gz`` segments are replayed first, in chronological
        (filename-date) order, then the live ``*.jsonl`` files, so the
        ``prev_hmac`` linkage is continuous from genesis to tail across the
        retention/archive boundary (issue #1835).  A flipped byte inside a
        ``.gz`` segment, or a deleted segment, surfaces as an HMAC/linkage
        error naming the segment rather than passing silently.

        Returns:
            ``(valid, errors)`` where *valid* is True when the entire chain
            is intact and *errors* lists any violations found.
        """
        errors: list[str] = []
        archived = _archived_segment_paths(self._audit_dir)
        live_files = sorted(self._audit_dir.glob(_JSONL_GLOB))
        if not archived and not live_files:
            return True, []

        prev_hmac = _GENESIS_HMAC
        for gz_path in archived:
            raw = _read_archived_segment(gz_path, errors)
            if raw is None:
                # Cannot establish linkage past an unreadable segment; the
                # error is already recorded, so stop rather than mis-seed the
                # live files from a wrong (genesis) prev_hmac.
                return False, errors
            prev_hmac = _verify_log_bytes(raw, gz_path.name, prev_hmac, self._key, errors)
        for log_path in live_files:
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

            # Crash-safe compress: write to a sibling temp file and atomically
            # rename into place, so a crash mid-archive never leaves a partial
            # ``.gz`` that the verifier would later read (the original
            # ``.jsonl`` is only unlinked once the full ``.gz`` is on disk).
            tmp_path = gz_path.with_name(f"{gz_path.name}.tmp")
            with log_path.open("rb") as f_in, gzip.open(tmp_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            tmp_path.replace(gz_path)

            log_path.unlink()
            archived.append(log_path.name)
            logger.info("Archived audit log %s -> %s", log_path.name, gz_path.name)

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
        include_archived: bool = False,
    ) -> list[AuditEvent]:
        """Filter audit events by type, actor, and/or time range.

        Args:
            event_type: If set, only return events matching this type.
            actor: If set, only return events from this actor.
            since: ISO 8601 lower bound (inclusive).
            until: ISO 8601 upper bound (inclusive).
            include_archived: Also read archived ``*.jsonl.gz`` segments,
                replayed in chronological order *before* the live files, the
                same way :meth:`verify` walks them (#1835). Default False
                keeps the hot path reading only live segments.

                Callers that reason about *linkage* between events - rather
                than about individual events - need this: after retention an
                event's predecessor commonly lives in an archived segment, so
                a live-only read makes an intact chain look broken. Reading
                archives costs a decompress per segment, so it belongs on the
                paths that need completeness, not on every query.

        Returns:
            List of matching AuditEvent instances (chronological order).
        """
        results: list[AuditEvent] = []
        sources: list[bytes | None] = []
        if include_archived:
            discard: list[str] = []
            sources.extend(_read_archived_segment(gz, discard) for gz in _archived_segment_paths(self._audit_dir))
        sources.extend(path.read_bytes() for path in sorted(self._audit_dir.glob(_JSONL_GLOB)))

        for blob in sources:
            if blob is None:
                # Unreadable archive segment; verify() reports it properly.
                continue
            # Strict decode, deliberately. Invalid UTF-8 in a segment is
            # evidence - of a truncated write, corruption, or tampering - and
            # ``verify`` hashes the raw bytes. Substituting U+FFFD here would
            # return a clean-looking record that does not match the bytes on
            # disk, so the projection and the verifier would disagree about
            # what the log says. Raising matches the previous ``read_text()``
            # behaviour exactly.
            for raw_line in blob.decode("utf-8").splitlines():
                raw = raw_line.strip()
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
