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
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

if sys.platform == "win32":
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

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

#: Issue #3014 - emitted when a requested container isolation boundary cannot
#: be provided (no container runtime CLI, SDK, or socket) and the spawn falls
#: back to a weaker boundary. Details carry the requested-vs-actual isolation
#: mode and the reason, so the audit chain records the downgrade as a
#: first-class decision instead of leaving it in a single log WARNING.
SANDBOX_ISOLATION_DOWNGRADE = "sandbox.isolation_downgrade"

#: Emitted when an append finds the newest segment ending in a non-verifying
#: suffix (a crash-torn partial line, bytes that fail their HMAC, or garbage
#: that is not valid UTF-8/JSON) and seals it: the terminator and this record
#: land in one write, so the evidence that a record was damaged cannot be
#: separated from the repair. Details carry ``segment``, ``byte_offset`` (the
#: segment size at seal time), ``verified_prefix_offset`` (the end of the last
#: verifiable record), ``torn_bytes_sha256``, and ``tear_class``.
EVENT_CHAIN_TORN_RECORD = "chain.torn_record"

#: Emitted by ``bernstein audit ack-tear`` when an operator signs off on tear
#: evidence. Appended to the chain itself, so clearing an alert is exactly as
#: tamper-evident as the damage was. Details carry ``segment`` and
#: ``byte_offset`` matching the tear, plus ``checkpoint_root`` when the
#: acknowledgement authorises sealing past a conflicting chain checkpoint.
EVENT_CHAIN_TEAR_ACKNOWLEDGED = "chain.tear_acknowledged"


class AuditKeyPermissionError(RuntimeError):
    """Raised when the audit key file has permissions looser than 0600."""


class AuditKeyMissingError(RuntimeError):
    """Raised when a read-only caller finds no audit key to load.

    Read-only verification paths must never mint key material: a freshly
    generated key cannot authenticate an existing chain, so the chain would
    fail verification and report a bogus tamper. Callers that only read the
    chain use :func:`load_audit_key` and surface this error instead.
    """


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


def load_audit_key(key_path: Path | None = None) -> bytes:
    """Load an existing audit HMAC key without ever creating one.

    The load-only counterpart to :func:`load_or_create_audit_key`, for commands
    that only read and verify the chain. It resolves the key path identically
    but creates no directory and no key file: a verifier that minted its own key
    would fail every HMAC check against a chain written under the real key and
    report that as tampering, turning a missing-key operator error into a false
    integrity alarm.

    Args:
        key_path: Optional explicit override. Useful for tests.

    Returns:
        The raw key bytes suitable for ``hmac.new``.

    Raises:
        AuditKeyMissingError: If no key file exists at the resolved path.
        AuditKeyPermissionError: If the key file is readable by anyone besides
            its owner.
    """
    resolved = key_path if key_path is not None else _default_audit_key_path()
    if not resolved.exists():
        raise AuditKeyMissingError(
            f"No audit HMAC key at {resolved}. This command only reads the audit chain and will not "
            f"create key material. Set ${AUDIT_KEY_ENV} to the key used to write the chain."
        )
    _enforce_key_permissions(resolved)
    return resolved.read_bytes().strip()


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


#: Per-audit-dir re-entrancy state for :func:`_chain_append_lock`.
#:
#: ``flock`` locks attach to an *open file description*, not to a process or a
#: thread, so a second ``os.open`` + ``flock(LOCK_EX)`` from the thread that
#: already holds the lock blocks on itself. That makes the naive way of turning
#: "read the head" and "append the record" into one atomic section -- wrapping
#: :meth:`AuditLog.log` in another ``_chain_append_lock`` -- a deadlock. The
#: per-thread depth counter lets the outermost acquisition own the ``flock``
#: while inner ones pass through, and the per-dir ``RLock`` keeps a *different*
#: thread out for the whole nested section, so re-entrancy never widens the
#: window it exists to close.
#:
#: Guards are keyed by the *resolved* audit dir and never evicted. Resolving is
#: load-bearing, not tidiness: two spellings of one directory would map to two
#: guards, and a thread that entered under one spelling and re-entered under the
#: other would see depth 0 and re-take the ``flock`` it already holds. Eviction
#: is unsafe for the same reason a lock cannot be recreated while held, and the
#: live set is one entry per audit dir a process actually writes to.
_APPEND_GUARDS: dict[str, threading.RLock] = {}
_APPEND_GUARDS_LOCK = threading.Lock()
_APPEND_DEPTH = threading.local()


def _append_guard(audit_key: str) -> threading.RLock:
    """Return the process-wide re-entrant guard for one audit dir."""
    with _APPEND_GUARDS_LOCK:
        guard = _APPEND_GUARDS.get(audit_key)
        if guard is None:
            guard = threading.RLock()
            _APPEND_GUARDS[audit_key] = guard
        return guard


def _inside_append_section(audit_dir: Path) -> bool:
    """Whether this thread currently holds *audit_dir*'s append section."""
    depths: dict[str, int] = getattr(_APPEND_DEPTH, "depths", None) or {}
    return depths.get(str(audit_dir.resolve()), 0) > 0


@contextlib.contextmanager
def _chain_append_lock(audit_dir: Path) -> Iterator[None]:
    """Serialise daily-log appends across processes via ``flock(LOCK_EX)``.

    The task server, the spawner, the orchestrator, and CLI commands are
    separate processes that append to the same ``.sdd/audit/<day>.jsonl``.
    In-process ``threading.Lock``s only serialise threads within one process;
    without an OS-level lock two processes recover the same chain tail in
    ``AuditLog.__init__`` and each append a record embedding the same stale
    ``prev_hmac``, forking the HMAC chain and breaking ``verify()`` for the
    whole daily log (issue #2791).

    A single stable lock file per audit dir serialises across day rollovers
    (a writer that has rolled to the next day still contends with one still on
    the previous day). Falls back to a no-op on platforms without ``fcntl``
    (Windows); the in-process locks callers may hold remain the only ordering
    there, matching how the lineage spine degrades.

    The section is re-entrant *within one thread*: a caller that must read the
    chain head and append the record that sits on it without an interleaving
    writer holds this lock across both, and the ``AuditLog.log`` inside it
    re-enters rather than deadlocking. Other threads and other processes still
    wait for the outermost section to end.
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_key = str(audit_dir.resolve())
    guard = _append_guard(audit_key)
    depths: dict[str, int] | None = getattr(_APPEND_DEPTH, "depths", None)
    if depths is None:
        depths = {}
        _APPEND_DEPTH.depths = depths

    with guard:
        depths[audit_key] = depths.get(audit_key, 0) + 1
        try:
            if depths[audit_key] > 1:
                # Already inside this thread's section: the outermost frame
                # holds the flock, so re-taking it would block on ourselves.
                yield
                return
            if fcntl is None:  # pragma: no cover - Windows path
                yield
                return
            lock_path = audit_dir / ".chain.lock"
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            depths[audit_key] -= 1
            if not depths[audit_key]:
                del depths[audit_key]


def _path_size(path: Path) -> int:
    """Return ``path`` size in bytes, or ``-1`` when it does not exist."""
    try:
        return path.stat().st_size
    except OSError:
        return -1


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


@dataclass(frozen=True)
class _LineFinding:
    """One non-verifying line, with enough position data to classify it later."""

    display_name: str
    line_no: int
    offset: int
    length: int
    kind: str
    messages: tuple[str, ...]


@dataclass(frozen=True)
class _ChainMarker:
    """A verified tear-evidence record (``chain.torn_record`` / acknowledgement)."""

    display_name: str
    line_no: int
    offset: int
    event_type: str
    details: dict[str, Any]
    hmac: str


@dataclass
class _ChainWalkContext:
    """Structured observations collected while :func:`_verify_log_bytes` walks.

    Purely additive: when no context is passed the walker behaves exactly as
    before. With one, tear-aware callers learn where each failure sits, where
    the last verifying record of each segment ends, and which verified records
    are tear evidence or acknowledgements - without a second pass.
    """

    failures: list[_LineFinding] = field(default_factory=list)
    markers: list[_ChainMarker] = field(default_factory=list)
    ok_end: dict[str, int] = field(default_factory=dict)
    total: dict[str, int] = field(default_factory=dict)
    missing_newline: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def _verify_log_bytes(
    raw_bytes: bytes,
    display_name: str,
    prev_hmac: str,
    key: bytes,
    errors: list[str],
    ctx: _ChainWalkContext | None = None,
) -> str:
    """Verify the JSONL entries in ``raw_bytes``, appending errors.

    ``display_name`` is the name used in every error message so callers can
    point at either a live ``*.jsonl`` file or an archived ``*.jsonl.gz``
    segment.  Verification is byte-for-byte identical for both: an archived
    segment is decompressed to its original bytes and run through the same
    canonicalisation, ``prev_hmac`` linkage, and HMAC checks, so archived
    history stays exactly as tamper-evident as live history (issue #1835).
    """
    if ctx is not None:
        if display_name not in ctx.order:
            ctx.order.append(display_name)
        ctx.total[display_name] = ctx.total.get(display_name, 0) + len(raw_bytes)
        ctx.ok_end.setdefault(display_name, 0)

    if raw_bytes and not raw_bytes.endswith(b"\n"):
        # The writer always terminates with ``\n``; absence is itself
        # tamper-evidence (e.g. ``\n`` flipped to ``\v`` at EOF). Continue
        # into the per-line loop so a truncated last record is still
        # surfaced as ``invalid JSON`` for callers that key on that
        # message (test_partial_last_line_flagged_as_invalid_json).
        message = f"{display_name}: missing trailing newline"
        errors.append(message)
        if ctx is not None:
            ctx.missing_newline[display_name] = message

    def _fail(line_no: int, offset: int, length: int, kind: str, messages: tuple[str, ...]) -> None:
        errors.extend(messages)
        if ctx is not None:
            ctx.failures.append(
                _LineFinding(
                    display_name=display_name,
                    line_no=line_no,
                    offset=offset,
                    length=length,
                    kind=kind,
                    messages=messages,
                )
            )

    # ``_split_jsonl_bytes`` splits on ``b"\n"`` only, so the start offset of
    # each line inside the segment is the running sum of prior line lengths
    # plus one byte per consumed separator. Tracking it lets an undecodable
    # line name the exact byte in the segment an operator would seek to.
    segment_offset = 0
    for line_no, raw_line in enumerate(_split_jsonl_bytes(raw_bytes), start=1):
        line_offset = segment_offset
        segment_offset += len(raw_line) + 1  # +1 for the split ``b"\n"``
        if raw_line == b"":
            continue
        try:
            parsed_entry = json.loads(raw_line)
        except UnicodeDecodeError as exc:
            # ``json.loads`` on raw bytes raises ``UnicodeDecodeError`` (not
            # ``JSONDecodeError``) when a byte sequence is not valid UTF-8.
            # Keep ``verify`` total: report the undecodable line and the byte
            # it fails at, then keep walking so the rest of this segment and
            # every later segment are still verified rather than one bad byte
            # taking the whole audit surface down. Undecodable bytes are
            # exactly what an operator runs ``verify`` to learn about, so this
            # is a loud failure, never a warning or a skipped segment.
            _fail(
                line_no,
                line_offset,
                len(raw_line),
                "undecodable",
                (f"{display_name}:{line_no}: undecodable bytes at offset {line_offset + exc.start} - {exc}",),
            )
            continue
        except json.JSONDecodeError as exc:
            _fail(
                line_no,
                line_offset,
                len(raw_line),
                "invalid_json",
                (f"{display_name}:{line_no}: invalid JSON - {exc}",),
            )
            continue
        if not isinstance(parsed_entry, dict):
            _fail(
                line_no,
                line_offset,
                len(raw_line),
                "not_object",
                (f"{display_name}:{line_no}: entry is not a JSON object",),
            )
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
            _fail(
                line_no,
                line_offset,
                len(raw_line),
                "non_canonical",
                (f"{display_name}:{line_no}: non-canonical line bytes",),
            )
            continue

        stored_hmac = str(entry.pop("hmac", ""))
        entry_prev = str(entry.get("prev_hmac", ""))
        line_messages: list[str] = []
        # Constant-time compare on the chain link - verification is offline
        # but a leaky compare in audit code is a CodeQL/Bandit smell and
        # masks regressions when the same helper is later reused on a
        # network surface.
        if not _hmac.compare_digest(entry_prev, prev_hmac):
            line_messages.append(
                f"{display_name}:{line_no}: prev_hmac mismatch (expected {prev_hmac[:16]}…, got {entry_prev[:16]}…)"
            )

        expected_hmac = _compute_hmac(key, prev_hmac, entry)
        if not _hmac.compare_digest(stored_hmac, expected_hmac):
            line_messages.append(
                f"{display_name}:{line_no}: HMAC mismatch (expected {expected_hmac[:16]}…, got {stored_hmac[:16]}…)"
            )

        if line_messages:
            _fail(line_no, line_offset, len(raw_line), "invalid_hmac", tuple(line_messages))
        elif ctx is not None:
            line_end = line_offset + len(raw_line)
            if line_end < len(raw_bytes):
                line_end += 1  # the terminator this line owns
            ctx.ok_end[display_name] = ctx.total.get(display_name, 0) - len(raw_bytes) + line_end
            event_type = str(entry.get("event_type", ""))
            if event_type in (EVENT_CHAIN_TORN_RECORD, EVENT_CHAIN_TEAR_ACKNOWLEDGED):
                details = entry.get("details")
                ctx.markers.append(
                    _ChainMarker(
                        display_name=display_name,
                        line_no=line_no,
                        offset=line_offset,
                        event_type=event_type,
                        details=dict(details) if isinstance(details, dict) else {},
                        hmac=stored_hmac,
                    )
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


def _record_is_locally_valid(key: bytes, entry: dict[str, Any]) -> bool:
    """Whether *entry*'s stored ``hmac`` matches its own stated ``prev_hmac``.

    A *local* check: it authenticates the record against the predecessor the
    record itself names, without walking the chain. Crash garbage that happens
    to parse as a canonical ``hmac``-bearing object still fails it, because
    producing a matching MAC requires the audit key. It deliberately does not
    prove the stated predecessor is the true chain head - that is ``verify``'s
    job - only that the record was minted by a key holder.
    """
    body = {k: v for k, v in entry.items() if k != "hmac"}
    expected = _compute_hmac(key, str(entry.get("prev_hmac", "")), body)
    return _hmac.compare_digest(str(entry.get("hmac", "")), expected)


def _chain_tail_from_bytes(raw_bytes: bytes, key: bytes | None = None) -> str | None:
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

    When *key* is provided the candidate must additionally pass
    :func:`_record_is_locally_valid`: crash-torn tails are arbitrary bytes, not
    clean prefixes, and a garbage tail can parse as a canonical ``hmac``-bearing
    object by accident. Without the MAC check recovery would adopt such a line
    as the head and every later append would chain onto bytes no key holder
    wrote. ``AuditLog`` always passes its key; the keyless form is kept for
    read-only callers that only need a best-effort tip.
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
        if "hmac" not in entry:
            continue
        if key is not None and not _record_is_locally_valid(key, entry):
            continue
        return str(entry["hmac"])
    return None


def _local_tail_state(raw: bytes, key: bytes) -> tuple[int, str | None]:
    """Return ``(verified_prefix_end, head_hmac)`` for one segment's bytes.

    Finds the *last* line that is canonical, ``hmac``-bearing, and locally
    MAC-valid (see :func:`_record_is_locally_valid`), scanning from the end so
    the cost is proportional to the damaged suffix, not the segment. The
    returned offset is the byte after that record (including its terminator
    when present), i.e. where the non-verifying suffix begins; the head is
    that record's ``hmac``, or ``None`` when the segment holds no locally
    valid record at all.
    """
    lines: list[tuple[int, bytes]] = []
    offset = 0
    for raw_line in _split_jsonl_bytes(raw):
        lines.append((offset, raw_line))
        offset += len(raw_line) + 1

    for line_start, raw_line in reversed(lines):
        if raw_line == b"":
            continue
        try:
            parsed = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        entry = cast("dict[str, Any]", parsed)
        if json.dumps(entry, sort_keys=True).encode() != raw_line:
            continue
        if "hmac" not in entry or not _record_is_locally_valid(key, entry):
            continue
        line_end = line_start + len(raw_line)
        if line_end < len(raw):
            line_end += 1  # the terminator this line owns
        return line_end, str(entry["hmac"])
    return 0, None


def _classify_torn_bytes(torn: bytes) -> str:
    """Classify a non-verifying tail per the crash-tear model.

    Crashed appends can leave arbitrary bytes, not just a clean prefix of the
    record, so every shape is named rather than assumed:

    * ``unterminated_record`` - the suffix is empty: the final record itself
      is valid and only its terminator was lost.
    * ``garbage_bytes`` - bytes that are not valid UTF-8, or damage spanning
      several apparent lines.
    * ``invalid_hmac`` - a single line that parses as a canonical
      ``hmac``-bearing object but fails its MAC (crash garbage can look like
      JSON; only a key holder can produce a matching MAC).
    * ``non_canonical`` - parses as JSON but is not the writer's byte framing.
    * ``partial_record`` - a single line that does not parse: the classic
      crash-mid-write fragment.
    """
    if not torn:
        return "unterminated_record"
    try:
        torn.decode("utf-8")
    except UnicodeDecodeError:
        return "garbage_bytes"
    fragments = [line for line in torn.split(b"\n") if line != b""]
    if len(fragments) != 1:
        return "garbage_bytes"
    try:
        parsed = json.loads(fragments[0])
    except json.JSONDecodeError:
        return "partial_record"
    if not isinstance(parsed, dict) or json.dumps(parsed, sort_keys=True).encode() != fragments[0]:
        return "non_canonical"
    return "invalid_hmac" if "hmac" in parsed else "non_canonical"


@dataclass(frozen=True)
class TearEvidence:
    """Durable evidence that a segment carried a non-verifying suffix.

    Reported by :meth:`AuditLog.verify_detailed` as a verdict distinct from
    chain corruption: a tear is crash-shaped damage at the tail (or its sealed
    remains), stays reported until an operator acknowledges it with
    ``bernstein audit ack-tear``, and never clears itself.

    Attributes:
        segment: Live segment file name the tear sits in.
        byte_offset: Segment size when the tear was observed or sealed; the
            offset an acknowledgement must name.
        verified_prefix_offset: End of the last verifiable record before the
            damage.
        tear_class: See :func:`_classify_torn_bytes`.
        sealed: Whether an :data:`EVENT_CHAIN_TORN_RECORD` record already
            anchors the evidence in the chain.
        acknowledged: Whether a matching acknowledgement record exists.
        raw_errors: The verifier messages this evidence subsumes.
    """

    segment: str
    byte_offset: int
    verified_prefix_offset: int
    tear_class: str
    sealed: bool
    acknowledged: bool
    raw_errors: tuple[str, ...] = ()

    def describe(self) -> str:
        state = "sealed" if self.sealed else "unsealed"
        ack = "acknowledged" if self.acknowledged else "UNACKNOWLEDGED"
        return (
            f"{self.segment}: {self.tear_class} tear at byte {self.byte_offset} "
            f"(verifiable prefix ends at {self.verified_prefix_offset}; {state}, {ack})"
        )


@dataclass
class ChainVerifyReport:
    """Outcome of :meth:`AuditLog.verify_detailed`.

    ``hard_errors`` are chain damage that is *not* tear-shaped (mid-history
    tampering, broken linkage with verified records after it); ``tears`` are
    crash-shaped tail damage, each durable until acknowledged.
    """

    hard_errors: list[str] = field(default_factory=list)
    tears: list[TearEvidence] = field(default_factory=list)

    @property
    def unacknowledged_tears(self) -> list[TearEvidence]:
        return [tear for tear in self.tears if not tear.acknowledged]

    @property
    def ok(self) -> bool:
        return not self.hard_errors and not self.unacknowledged_tears


class OutstandingTearError(RuntimeError):
    """Raised when an operation refuses to proceed over unacknowledged tears.

    Sealing over unacknowledged tear evidence would fold the damage into a
    fresh root and stop it being reportable; the seal refuses instead, until
    an operator acknowledges via ``bernstein audit ack-tear``.
    """

    def __init__(self, tears: list[TearEvidence]) -> None:
        self.tears = tears
        head = "; ".join(t.describe() for t in tears[:3])
        super().__init__(f"Audit chain carries unacknowledged tear evidence; refusing to proceed: {head}")


def _live_segment_name(display_name: str) -> str:
    """Map an archived display name back to the live segment name."""
    return display_name[: -len(".gz")] if display_name.endswith(".gz") else display_name


def _build_verify_report(errors: list[str], ctx: _ChainWalkContext) -> ChainVerifyReport:
    """Fold walk observations into hard errors plus classified tear evidence."""
    claimed: set[int] = set()
    claimed_messages: set[str] = set()
    tears: list[TearEvidence] = []
    seen_tears: set[tuple[str, int]] = set()

    # Sealed tears: a verified torn-record marker vouches for the exact
    # non-verifying span it recorded, in its own segment only. Anything a
    # marker does not cover stays a hard error.
    for marker in ctx.markers:
        if marker.event_type != EVENT_CHAIN_TORN_RECORD:
            continue
        segment = _live_segment_name(marker.display_name)
        if str(marker.details.get("segment", "")) != segment:
            continue
        byte_offset = int(marker.details.get("byte_offset", 0) or 0)
        prefix_end = int(marker.details.get("verified_prefix_offset", byte_offset) or 0)
        if (segment, byte_offset) in seen_tears:
            continue
        seen_tears.add((segment, byte_offset))
        run_messages: list[str] = []
        for index, failure in enumerate(ctx.failures):
            if index in claimed or failure.display_name != marker.display_name:
                continue
            if prefix_end <= failure.offset < byte_offset:
                claimed.add(index)
                run_messages.extend(failure.messages)
        claimed_messages.update(run_messages)
        tears.append(
            TearEvidence(
                segment=segment,
                byte_offset=byte_offset,
                verified_prefix_offset=prefix_end,
                tear_class=str(marker.details.get("tear_class", "unknown")),
                sealed=True,
                acknowledged=False,
                raw_errors=tuple(run_messages),
            )
        )

    # Unsealed tail tear: every non-verifying line after the last verifying
    # record of the chain's final segment, extending to end of file. Damage
    # with verified records after it is not a tail and stays hard. A chain
    # with no verifying record at all is not a tear either: a crash damages
    # the suffix of history that verified, so when nothing verifies the
    # damage is a broken or foreign chain (e.g. written under a different
    # key) and must stay a hard error rather than something an operator is
    # invited to acknowledge away.
    if ctx.order and any(ctx.ok_end.values()):
        final = ctx.order[-1]
        ok_end = ctx.ok_end.get(final, 0)
        tail_indices = [
            index
            for index, failure in enumerate(ctx.failures)
            if index not in claimed and failure.display_name == final and failure.offset >= ok_end
        ]
        newline_message = ctx.missing_newline.get(final)
        if tail_indices:
            run_messages = []
            if newline_message is not None:
                run_messages.append(newline_message)
                claimed_messages.add(newline_message)
            kinds: list[str] = []
            for index in tail_indices:
                claimed.add(index)
                run_messages.extend(ctx.failures[index].messages)
                kinds.append(ctx.failures[index].kind)
            claimed_messages.update(run_messages)
            segment = _live_segment_name(final)
            key = (segment, ctx.total.get(final, 0))
            if key not in seen_tears:
                seen_tears.add(key)
                tears.append(
                    TearEvidence(
                        segment=segment,
                        byte_offset=ctx.total.get(final, 0),
                        verified_prefix_offset=ok_end,
                        tear_class=_tail_tear_class(kinds, unterminated=newline_message is not None),
                        sealed=False,
                        acknowledged=False,
                        raw_errors=tuple(run_messages),
                    )
                )
        elif newline_message is not None:
            # The final fragment is a fully valid record that lost only its
            # terminator: nothing failed, but the tail is still torn and the
            # next append must seal it.
            claimed_messages.add(newline_message)
            segment = _live_segment_name(final)
            total = ctx.total.get(final, 0)
            if (segment, total) not in seen_tears:
                tears.append(
                    TearEvidence(
                        segment=segment,
                        byte_offset=total,
                        verified_prefix_offset=total,
                        tear_class="unterminated_record",
                        sealed=False,
                        acknowledged=False,
                        raw_errors=(newline_message,),
                    )
                )

    # Acknowledgements are verified chain records matched by exact
    # (segment, byte_offset); an operator must name the tear as verify
    # printed it.
    acked: set[tuple[str, int]] = set()
    for marker in ctx.markers:
        if marker.event_type != EVENT_CHAIN_TEAR_ACKNOWLEDGED:
            continue
        acked.add(
            (
                str(marker.details.get("segment", "")),
                int(marker.details.get("byte_offset", 0) or 0),
            )
        )
    tears = [
        TearEvidence(
            segment=tear.segment,
            byte_offset=tear.byte_offset,
            verified_prefix_offset=tear.verified_prefix_offset,
            tear_class=tear.tear_class,
            sealed=tear.sealed,
            acknowledged=(tear.segment, tear.byte_offset) in acked,
            raw_errors=tear.raw_errors,
        )
        for tear in tears
    ]

    hard_errors = [message for message in errors if message not in claimed_messages]
    return ChainVerifyReport(hard_errors=hard_errors, tears=tears)


def _tail_tear_class(kinds: list[str], *, unterminated: bool) -> str:
    """Name the damage class of an unsealed tail run from its failure kinds."""
    if "undecodable" in kinds:
        return "garbage_bytes"
    if len(kinds) != 1:
        return "garbage_bytes"
    kind = kinds[0]
    if kind == "invalid_json":
        return "partial_record" if unterminated else "garbage_bytes"
    if kind == "invalid_hmac":
        return "invalid_hmac"
    if kind in ("non_canonical", "not_object"):
        return "non_canonical"
    return "garbage_bytes"


@dataclass
class ChainScanCursor:
    """Resume point for an incremental authenticated chain scan.

    The audit chain is append-only and HMAC-linked, so a reader that has
    already authenticated a prefix can resume from the running ``prev_hmac``
    and verify only the bytes appended since. Without this, every reader that
    wants authenticated rows pays a full HMAC walk of the whole chain on every
    call, which turns an O(1) hot path into O(entire chain) (#2648).

    Attributes:
        prev_hmac: Chain digest at the end of the consumed prefix.
        consumed: Bytes already verified per segment, keyed by segment date
            stem so a live ``<date>.jsonl`` and its archived
            ``<date>.jsonl.gz`` counterpart are the same segment.
        order: Segment stems in the order they were consumed, used to detect
            history that changed underneath the cursor.
    """

    prev_hmac: str = _GENESIS_HMAC
    consumed: dict[str, int] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    complete: set[str] = field(default_factory=set)
    fingerprint: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass
class ChainScanResult:
    """Outcome of :meth:`AuditLog.scan_verified`."""

    ok: bool
    events: list[AuditEvent]
    cursor: ChainScanCursor
    errors: list[str] = field(default_factory=list)
    rescanned: bool = False


def _segment_stem(path: Path) -> str:
    """Return the date stem shared by a live segment and its archived form."""
    name = path.name
    return name[: -len(".jsonl.gz")] if name.endswith(".jsonl.gz") else name[: -len(".jsonl")]


#: Filename of the signed per-segment scan index inside the audit directory.
#: The index is derived data, so it lives beside the log rather than beside the
#: key; it is HMAC-signed with the audit key, so an attacker who can write the
#: audit directory cannot forge an entry that would be trusted (#2648).
_SEGMENT_INDEX_NAME = ".segment-index.json"

#: Bump when the stored entry shape changes so old indexes are ignored.
_SEGMENT_INDEX_VERSION = 1


def _sign_index_payload(payload: dict[str, Any], key: bytes) -> str:
    return _hmac.new(key, json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def _load_segment_index(audit_dir: Path, key: bytes, event_type: str) -> dict[str, Any]:
    """Return the verified per-segment index, or ``{}`` when unusable.

    Any failure (missing, unparsable, wrong version, wrong filter, or a bad
    signature) degrades to an empty index, which simply costs a full walk. The
    index can therefore never weaken verification: it is only ever consulted
    for segments whose signed fingerprint still matches what is on disk.
    """
    path = audit_dir / _SEGMENT_INDEX_NAME
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # ``read_text`` raises ``UnicodeDecodeError`` (not ``JSONDecodeError``)
        # on an index file that is not valid UTF-8. Fold it into the same
        # degrade-to-empty path as every other unusable index: the index can
        # only ever cost a full walk, never weaken verification.
        return {}
    if not isinstance(doc, dict):
        return {}
    payload = doc.get("payload")
    if not isinstance(payload, dict):
        return {}
    if doc.get("hmac") != _sign_index_payload(cast("dict[str, Any]", payload), key):
        return {}
    typed = cast("dict[str, Any]", payload)
    if typed.get("version") != _SEGMENT_INDEX_VERSION or typed.get("event_type") != event_type:
        return {}
    segments = typed.get("segments")
    return cast("dict[str, Any]", segments) if isinstance(segments, dict) else {}


def _store_segment_index(audit_dir: Path, key: bytes, event_type: str, segments: dict[str, Any]) -> None:
    """Atomically write the signed per-segment index (best effort)."""
    payload: dict[str, Any] = {
        "version": _SEGMENT_INDEX_VERSION,
        "event_type": event_type,
        "segments": segments,
    }
    doc = {"payload": payload, "hmac": _sign_index_payload(payload, key)}
    path = audit_dir / _SEGMENT_INDEX_NAME
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, path)
    except OSError:
        logger.debug("could not persist audit segment index at %s", path, exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink()


def _event_to_row(event: AuditEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp,
        "event_type": event.event_type,
        "actor": event.actor,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "details": event.details,
        "prev_hmac": event.prev_hmac,
        "hmac": event.hmac,
    }


def _row_to_event(row: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        timestamp=row.get("timestamp", ""),
        event_type=row.get("event_type", ""),
        actor=row.get("actor", ""),
        resource_type=row.get("resource_type", ""),
        resource_id=row.get("resource_id", ""),
        details=row.get("details", {}),
        prev_hmac=row.get("prev_hmac", ""),
        hmac=row.get("hmac", ""),
    )


def _decode_segment_text(raw: bytes) -> str:
    """Decode segment bytes for record projection, skipping undecodable lines.

    Strict per-line decoding: a line that is not valid UTF-8 cannot be a
    canonical record, is dropped whole, and is never smoothed over with
    replacement characters - so a projected record is always byte-exact
    against what the verifier hashed, and a tampered or crash-damaged line is
    never *returned* in laundered form, only omitted. Loudness stays with
    ``verify``/``verify_detailed``, which name the undecodable bytes and keep
    failing until the damage is acknowledged.

    This keeps every read surface total over a chain that legitimately
    carries sealed tear evidence: torn bytes are permanent (the log is
    append-only and a seal never truncates), so a whole-blob strict decode
    would turn one acknowledged crash into a forever-raising query path.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded: list[str] = []
        for line in raw.split(b"\n"):
            try:
                decoded.append(line.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        return "\n".join(decoded)


def _events_from_text(
    text: str,
    *,
    event_type: str | None,
    actor: str | None,
    since: str | None,
    until: str | None,
    resource_id: str | None = None,
) -> list[AuditEvent]:
    """Parse JSONL *text* into filtered :class:`AuditEvent` records.

    Shared by every :meth:`AuditLog.query` source so live and archived
    segments are decoded by exactly one code path.
    """
    events: list[AuditEvent] = []
    for raw_line in text.splitlines():
        raw = raw_line.strip()
        if not raw:
            continue
        # Line-level superset test: within a segment that does mention the id,
        # only parse the lines that could carry it. A record holds
        # ``resource_id`` as a field value only if that value appears verbatim
        # in the line, so a line without it is a definite miss and is skipped
        # before ``json.loads``. The exact comparison in
        # ``_matches_query_filters`` still rejects a coincidental substring
        # match, so this can never drop a genuine match.
        if resource_id and resource_id not in raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not _matches_query_filters(entry, event_type, actor, since, until, resource_id):
            continue
        events.append(
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
    return events


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
    resource_id: str | None = None,
) -> bool:
    """Return True if entry passes all query filters."""
    if event_type and entry.get("event_type") != event_type:
        return False
    if actor and entry.get("actor") != actor:
        return False
    if resource_id and entry.get("resource_id") != resource_id:
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
        # Tracks the day file and its byte length after this instance's last
        # append, so ``log`` can skip re-reading the tail from disk when no
        # other writer has touched the file since (issue #2791). ``None`` /
        # ``-1`` force a re-sync on the first append.
        self._synced_path: Path | None = None
        self._synced_size = -1

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
            tip = _chain_tail_from_bytes(log_path.read_bytes(), self._key)
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
            tip = _chain_tail_from_bytes(raw, self._key)
            if tip is not None:
                return tip
        return _GENESIS_HMAC

    # -- head ---------------------------------------------------------------

    @contextlib.contextmanager
    def append_transaction(self) -> Iterator[None]:
        """Hold the chain against other writers for a read-then-append section.

        :meth:`resync_head` is only meaningful inside this section: outside it,
        another writer can append between the read and the record that is
        supposed to sit on what was read. Re-entrant for the calling thread, so
        the :meth:`log` inside the section does not block on itself; exclusive
        against every other thread and process.
        """
        with _chain_append_lock(self._audit_dir):
            yield

    def resync_head(self) -> str:
        """Re-read the chain head from disk and return it.

        The cached head this instance carries reflects its *own* appends: a
        record another process wrote lands on disk without touching it. A caller
        that embeds the head into a payload it signs must read it through here,
        and must do so inside the same :func:`_chain_append_lock` section that
        appends the record, or another writer can overtake the value between the
        read and the append -- leaving a signature that names a chain position
        its own record does not occupy.

        Callers that only want to *observe* the head want
        :attr:`AuditChainStore.prev_chain_digest`; this method is for the caller
        that is about to write, and it enforces that rather than documenting it.
        It re-points ``log``'s fast path, so running it outside the section would
        let an append land between the read and the bookkeeping and leave the
        recorded size describing bytes the head does not cover -- ``log`` would
        then skip a re-sync it needed and append onto a stale head, which is the
        chain fork issue #2791 closed.

        Returns:
            The chain head as recovered from disk under the verifier's framing.

        Raises:
            RuntimeError: When called outside :meth:`append_transaction`.
        """
        if not _inside_append_section(self._audit_dir):
            raise RuntimeError(
                "resync_head() must be called inside append_transaction(); "
                "use AuditChainStore.prev_chain_digest to observe the head without appending"
            )
        # Shares ``log``'s (path, size) fast path, in both directions (see
        # ``_sync_for_append``). The day file is re-derived rather than
        # assumed, so a clock rollover between here and the append shows up as
        # a path mismatch and re-syncs. Any torn tail is sealed before the head
        # is read, so the value returned is the head the next append actually
        # chains onto rather than the one that preceded the seal - a caller
        # that embeds this head into a signed payload must never publish a
        # chain position its own record will not occupy.
        day_path = self._audit_dir / f"{datetime.now(tz=UTC).strftime('%Y-%m-%d')}.jsonl"
        self._sync_for_append(day_path)
        return self._prev_hmac

    def _sync_for_append(self, log_path: Path) -> None:
        """Bring the cached chain head in line with *log_path* before appending.

        Order matters: a torn tail is sealed *first*, and only then is the
        head read, because sealing appends its own evidence record and
        therefore moves the head. Reading first would publish a head the next
        append does not chain onto.

        Fast path: when the day file is byte-length-identical to what this
        instance's own last append left it at, no other writer has touched it,
        our own append always ends in ``b"\\n"`` so the tail cannot be torn,
        and both the tear probe and the full rescan are skipped. The probe
        must stay behind this branch: unconditionally it is a stat + open +
        seek + read on every append, which is a measurable fraction of append
        throughput.

        The slow path records what it synced, so a nested append inside the
        same ``append_transaction`` section takes the fast path instead of
        re-scanning the segment it just scanned. The caller must hold the
        chain append lock.
        """
        size = _path_size(log_path)
        if log_path == self._synced_path and size == self._synced_size:
            return
        # One read serves both the tear gate and head recovery, so the slow
        # path costs a single pass over the day segment - the same order as
        # the plain re-sync it replaces.
        try:
            raw = log_path.read_bytes()
        except OSError:
            raw = b""
        if raw:
            verified_prefix, local_head = _local_tail_state(raw, self._key)
            if verified_prefix != len(raw) or not raw.endswith(b"\n"):
                self._seal_torn_tail(log_path, raw, verified_prefix, local_head)
                return
            if local_head is not None:
                self._prev_hmac = local_head
                self._synced_path = log_path
                self._synced_size = len(raw)
                return
        self._prev_hmac = self._recover_chain_tail()
        self._synced_path = log_path
        self._synced_size = _path_size(log_path)

    def _mint_record(
        self,
        *,
        ts: str,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> tuple[AuditEvent, str]:
        """Build one record against the cached head; return it with its wire line.

        Minting and writing are separate so the tear seal can put a record and
        the terminator that precedes it into one ``write``. The returned line
        is the canonical serialisation including its terminator, so every
        writer emits byte-identical framing. Does not advance the head; the
        caller does that once the bytes are on disk.
        """
        entry_dict: dict[str, Any] = {
            "timestamp": ts,
            "event_type": event_type,
            "actor": actor,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "details": details,
            "prev_hmac": self._prev_hmac,
        }
        computed_hmac = _compute_hmac(self._key, self._prev_hmac, entry_dict)
        entry_dict["hmac"] = computed_hmac
        event = AuditEvent(
            timestamp=ts,
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            prev_hmac=self._prev_hmac,
            hmac=computed_hmac,
        )
        return event, json.dumps(entry_dict, sort_keys=True) + "\n"

    def _seal_torn_tail(self, log_path: Path, raw: bytes, verified_prefix: int, local_head: str | None) -> bool:
        """Seal a non-verifying tail so the next record cannot fuse onto it.

        A crashed append is not a clean prefix: file size can persist before
        data blocks, so the tail may hold a partial line, bytes that parse as
        JSON but fail their MAC, or garbage that is not UTF-8 at all. Without
        the seal, the next append writes directly onto an unterminated tail
        and the two fuse into one unparseable line - the record being written
        is destroyed and recovery resumes from a stale predecessor (#3130).

        Never repairs and never truncates: the log is append-only, so the torn
        bytes stay exactly where they are. The terminator and the
        :data:`EVENT_CHAIN_TORN_RECORD` evidence record land in **one write**:
        writing the terminator first and the evidence second leaves a
        reachable state (crash, SIGKILL, full volume) in which the segment is
        healed and nothing says it was ever damaged. A torn write of the
        combined buffer leaves a fresh unterminated fragment, which the next
        append seals and reports in turn.

        The evidence is fsynced before this returns: it is the only surviving
        statement that a record was lost, written into the same
        crash-truncatable tail it is evidence about. One fsync per tear is not
        a hot path - tears are crashes.

        Both the terminator and the record go into *this* segment, so a tear
        found across a UTC midnight cannot land its evidence in the next
        day's file. The caller must hold the chain append lock.

        Returns:
            Whether a tear was sealed (and therefore whether this appended).
        """
        # The caller has already established that the tail does not verify:
        # either the terminator is missing, or a *terminated* suffix fails
        # locally (crash garbage can parse and still fail its MAC). Sealing
        # both shapes matters - an unsealed terminated-invalid tail would let
        # the next append chain past it, after which the damage sits between
        # verified records and stops being classifiable as a tear.
        torn = raw[verified_prefix:]
        tear_class = _classify_torn_bytes(torn)

        # The evidence record must chain onto the value the *verifier's*
        # running prev will hold when it reaches the seal point, or the
        # record cannot verify and the evidence is unreadable. The verifier
        # adopts the stored ``hmac`` of every canonical-but-failing line it
        # walks through, so the seal mirrors that adoption over the torn
        # suffix, starting from the last locally valid head (falling back
        # across segments when this one holds no valid record at all).
        seal_prev = local_head if local_head is not None else self._recover_chain_tail()
        for torn_line in _split_jsonl_bytes(torn):
            if torn_line == b"":
                continue
            try:
                parsed = json.loads(torn_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(parsed, dict):
                continue
            entry = cast("dict[str, Any]", parsed)
            if json.dumps(entry, sort_keys=True).encode() != torn_line:
                continue
            seal_prev = str(entry.get("hmac", ""))
        self._prev_hmac = seal_prev
        event, line = self._mint_record(
            ts=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            event_type=EVENT_CHAIN_TORN_RECORD,
            actor="audit-log",
            resource_type="audit_segment",
            resource_id=log_path.name,
            details={
                "segment": log_path.name,
                "byte_offset": len(raw),
                "verified_prefix_offset": verified_prefix,
                "torn_bytes_sha256": hashlib.sha256(torn).hexdigest(),
                "tear_class": tear_class,
            },
        )
        terminator = b"" if raw.endswith(b"\n") else b"\n"
        buffer = terminator + line.encode("utf-8")
        with log_path.open("ab") as fh:
            fh.write(buffer)
            fh.flush()
            os.fsync(fh.fileno())

        # A short write leaves the segment ending in a fresh fragment.
        # Returning normally would let the caller append straight onto it and
        # fuse two records - the outcome sealing exists to prevent. Refusing
        # leaves the damage exactly as it was, and therefore still reportable.
        landed = _path_size(log_path)
        if landed != len(raw) + len(buffer):
            msg = (
                f"could not seal the torn tail of {log_path.name}: "
                f"{landed - len(raw)} of {len(buffer)} bytes were written; "
                "the segment is still torn and 'bernstein audit verify' still reports it"
            )
            raise OSError(msg)

        self._prev_hmac = event.hmac
        self._synced_path = log_path
        self._synced_size = landed
        return True

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
        with _chain_append_lock(self._audit_dir):
            # One clock reading feeds both the timestamp and the daily-file
            # name: two readings can straddle a UTC midnight and file an event
            # under a day that disagrees with its own timestamp, and the append
            # runs once per scheduling decision, so the second ``now`` is pure
            # hot-path cost (issue #2690).
            now = datetime.now(tz=UTC)
            ts = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            day = now.strftime("%Y-%m-%d")
            log_path = self._audit_dir / f"{day}.jsonl"

            # Re-sync the chain tail from disk under the cross-process lock so a
            # concurrent writer's appended head is chained onto rather than a
            # stale tail captured at construction (issue #2791), sealing any
            # crash-torn tail first so this record cannot fuse onto an
            # unterminated fragment (#3130). Fast path inside: when the day
            # file is byte-length-identical to what our own last append left
            # it at, no other process has appended and the cached head stands.
            self._sync_for_append(log_path)

            event, line = self._mint_record(
                ts=ts,
                event_type=event_type,
                actor=actor,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
            )

            # ``newline=""`` disables Python's universal-newline translation so
            # the literal ``\n`` we append survives byte-for-byte on Windows
            # (where text mode would otherwise rewrite it to ``\r\n``). The
            # verifier reads bytes and re-canonicalises against ``\n``-only
            # frames; without this the ``\r`` stays inside each split line and
            # the byte-equality tamper check trips.
            with log_path.open("a", encoding="utf-8", newline="") as fh:
                fh.write(line)
            computed_hmac = event.hmac

            self._prev_hmac = computed_hmac
            self._synced_path = log_path
            self._synced_size = _path_size(log_path)
        return event

    # -- verify -------------------------------------------------------------

    def scan_verified(
        self,
        cursor: ChainScanCursor | None = None,
        *,
        event_type: str | None = None,
    ) -> ChainScanResult:
        """Authenticate and read the chain at a cost bounded by what changed.

        Verifies exactly the segments it reads (archived then live, in the same
        order and with the same checks as :meth:`verify`). Two mechanisms keep
        it off the O(entire chain) path that a naive authenticated read forces:

        * **Cursor.** A caller that keeps the returned cursor pays only for the
          bytes appended since its last call.
        * **Signed segment index.** A cold caller (new process, empty cursor)
          adopts a previously verified prefix of each segment instead of
          re-walking it. Adoption is gated on a SHA-256 of the exact prefix
          bytes, so it is a cheap re-authentication rather than a trust
          assumption: a tampered prefix fails the digest and is fully
          re-verified. The index itself is HMAC-signed with the audit key, so an
          attacker with write access to the audit directory cannot forge an
          entry, and any unusable index simply degrades to a full walk (#2648).

        Args:
            cursor: Resume point from a previous call, or ``None`` to scan all.
            event_type: If set, only return events of this type, and enable the
                segment index (which retains only the filtered rows).

        Returns:
            A :class:`ChainScanResult` whose ``events`` are the newly consumed
            rows and whose ``cursor`` should be passed to the next call.
        """
        segments: list[tuple[str, Path, bool]] = [
            (_segment_stem(p), p, True) for p in _archived_segment_paths(self._audit_dir)
        ]
        segments += [(_segment_stem(p), p, False) for p in sorted(self._audit_dir.glob(_JSONL_GLOB))]

        # A cursor is only usable when what it consumed is still a prefix of
        # what is on disk, in the same order and no shorter.
        resume = cursor is not None
        if cursor is not None:
            stems = [stem for stem, _p, _a in segments]
            if not set(cursor.order).issubset(set(stems)) or stems[: len(cursor.order)] != cursor.order:
                resume = False

        active = cursor if resume and cursor is not None else ChainScanCursor()
        errors: list[str] = []
        events: list[AuditEvent] = []

        # A consumed segment that changed other than by appending invalidates
        # the cursor: the bytes behind the resume point are no longer the bytes
        # that were authenticated.
        if resume:
            for stem, path, _archived in segments:
                prior = active.fingerprint.get(stem)
                if prior is None:
                    continue
                try:
                    info = path.stat()
                except OSError:
                    resume = False
                    break
                if info.st_size < prior[0] or (stem in active.complete and (info.st_size, info.st_mtime_ns) != prior):
                    resume = False
                    break
            if not resume:
                return self.scan_verified(None, event_type=event_type)

        use_index = event_type is not None
        index: dict[str, Any] = _load_segment_index(self._audit_dir, self._key, event_type or "") if use_index else {}
        fresh_index: dict[str, Any] = {}
        index_changed = False

        for stem, path, archived in segments:
            if archived and stem in active.complete:
                continue

            already = active.consumed.get(stem, 0)
            start_hmac = active.prev_hmac
            # ``raw`` is the whole segment, held only when we actually need all
            # of it (a cold segment). When the cursor already covers a prefix we
            # seek past it instead, so a warm scan never re-reads the history it
            # has authenticated.
            raw: bytes | None = None
            adopted_rows: list[dict[str, Any]] = []

            if archived:
                raw = _read_archived_segment(path, errors)
                if raw is None:
                    return ChainScanResult(ok=False, events=events, cursor=active, errors=errors, rescanned=not resume)
                if already > len(raw):
                    return self.scan_verified(None, event_type=event_type)
                total = len(raw)
            else:
                try:
                    total = path.stat().st_size
                except OSError:
                    continue
                if already > total:
                    return self.scan_verified(None, event_type=event_type)
                if already == 0:
                    raw = path.read_bytes()
                    total = len(raw)

            if use_index and already == 0 and raw is not None:
                entry = index.get(stem)
                if isinstance(entry, dict):
                    covered = int(entry.get("byte_len", 0) or 0)
                    if (
                        0 < covered <= total
                        and entry.get("start_hmac") == start_hmac
                        and isinstance(entry.get("rows"), list)
                        and entry.get("prefix_sha256") == hashlib.sha256(raw[:covered]).hexdigest()
                    ):
                        # The prefix is byte-identical to what was verified, so
                        # adopt its result and verify only what came after.
                        adopted_rows = cast("list[dict[str, Any]]", entry["rows"])
                        events.extend(_row_to_event(r) for r in adopted_rows)
                        active.prev_hmac = str(entry.get("end_hmac", start_hmac))
                        already = covered

            if already == total and stem in active.consumed:
                if use_index and stem in index:
                    fresh_index[stem] = index[stem]
                continue

            if raw is not None:
                tail = raw[already:]
            else:
                with path.open("rb") as handle:
                    handle.seek(already)
                    tail = handle.read()

            active.prev_hmac = _verify_log_bytes(tail, path.name, active.prev_hmac, self._key, errors)
            # Per-line strict decode, matching ``query``: an undecodable line is
            # dropped whole, never smoothed with replacement characters, so a
            # projected row is always byte-exact against what
            # ``_verify_log_bytes`` above hashed - and the damage itself is
            # already recorded in ``errors`` rather than raised, keeping this
            # path total over a chain that carries sealed tear evidence.
            segment_events = _events_from_text(
                _decode_segment_text(tail),
                event_type=event_type,
                actor=None,
                since=None,
                until=None,
            )
            events.extend(segment_events)
            active.consumed[stem] = already + len(tail)
            if archived:
                active.complete.add(stem)
            with contextlib.suppress(OSError):
                info = path.stat()
                active.fingerprint[stem] = (info.st_size, info.st_mtime_ns)
            if stem not in active.order:
                active.order.append(stem)

            # Refresh the index only on a cold segment, where the whole segment
            # is in hand. A warm (seek) pass keeps the existing entry: its
            # shorter prefix stays valid and adoptable.
            if use_index and not errors and raw is not None:
                fresh_index[stem] = {
                    "byte_len": already + len(tail),
                    "prefix_sha256": hashlib.sha256(raw[: already + len(tail)]).hexdigest(),
                    "start_hmac": start_hmac,
                    "end_hmac": active.prev_hmac,
                    "rows": adopted_rows + [_event_to_row(e) for e in segment_events],
                }
                index_changed = True
            elif use_index and stem in index:
                fresh_index[stem] = index[stem]

        if use_index and index_changed and not errors:
            _store_segment_index(self._audit_dir, self._key, event_type or "", fresh_index)

        return ChainScanResult(
            ok=not errors,
            events=events,
            cursor=active,
            errors=errors,
            rescanned=not resume,
        )

    def verify(self) -> tuple[bool, list[str]]:
        """Walk archived then live JSONL segments and verify the HMAC chain.

        Archived ``*.jsonl.gz`` segments are replayed first, in chronological
        (filename-date) order, then the live ``*.jsonl`` files, so the
        ``prev_hmac`` linkage is continuous from genesis to tail across the
        retention/archive boundary (issue #1835).  A flipped byte inside a
        ``.gz`` segment, or a deleted segment, surfaces as an HMAC/linkage
        error naming the segment rather than passing silently.

        Tear evidence (crash-shaped damage at the chain tail, sealed or not -
        see :meth:`verify_detailed`) is reported through the same error list
        until an operator acknowledges it with ``bernstein audit ack-tear``;
        acknowledged tears no longer fail verification, but the evidence
        records stay in the chain permanently.

        Returns:
            ``(valid, errors)`` where *valid* is True when the entire chain
            is intact and *errors* lists any violations found.
        """
        report = self.verify_detailed()
        errors = list(report.hard_errors)
        for tear in report.unacknowledged_tears:
            if tear.raw_errors:
                errors.extend(tear.raw_errors)
            else:
                errors.append(f"{tear.describe()} - run 'bernstein audit ack-tear' after investigating")
        return len(errors) == 0, errors

    def verify_detailed(self) -> ChainVerifyReport:
        """Verify the chain, separating tear evidence from hard corruption.

        The tear model is garbage-tolerant: a crashed append can leave a
        partial line, JSON-shaped bytes that fail their MAC, or bytes that are
        not UTF-8 at all - file size can be persisted before data blocks, so
        no clean-prefix assumption holds. Any non-verifying suffix of the
        chain's final segment is classified as a tear with the byte offset of
        the last verifiable record (``sealed=False`` until an append seals
        it). A non-verifying run elsewhere is only tear evidence when a
        verifying :data:`EVENT_CHAIN_TORN_RECORD` record anchors it (the run
        sits exactly on the recorded ``verified_prefix_offset .. byte_offset``
        span in the same segment); everything else stays a hard error, because
        damage in the middle of verified history is tampering, not a crash.

        Acknowledgements are chain records themselves
        (:data:`EVENT_CHAIN_TEAR_ACKNOWLEDGED`), matched by
        ``(segment, byte_offset)``; an unacknowledged tear is reported on
        every run - it never clears itself, in particular not by re-sealing.

        Returns:
            A :class:`ChainVerifyReport`.
        """
        errors: list[str] = []
        ctx = _ChainWalkContext()
        archived = _archived_segment_paths(self._audit_dir)
        live_files = sorted(self._audit_dir.glob(_JSONL_GLOB))
        if not archived and not live_files:
            return ChainVerifyReport()

        prev_hmac = _GENESIS_HMAC
        for gz_path in archived:
            raw = _read_archived_segment(gz_path, errors)
            if raw is None:
                # Cannot establish linkage past an unreadable segment; the
                # error is already recorded, so stop rather than mis-seed the
                # live files from a wrong (genesis) prev_hmac.
                return ChainVerifyReport(hard_errors=errors)
            prev_hmac = _verify_log_bytes(raw, gz_path.name, prev_hmac, self._key, errors, ctx)
        for log_path in live_files:
            prev_hmac = _verify_log_bytes(log_path.read_bytes(), log_path.name, prev_hmac, self._key, errors, ctx)

        return _build_verify_report(errors, ctx)

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
        resource_id: str | None = None,
        include_archived: bool = False,
    ) -> list[AuditEvent]:
        """Filter audit events by type, actor, resource, and/or time range.

        Args:
            event_type: If set, only return events matching this type.
            actor: If set, only return events from this actor.
            since: ISO 8601 lower bound (inclusive).
            until: ISO 8601 upper bound (inclusive).
            resource_id: If set, only return events whose ``resource_id``
                matches exactly. A non-empty value also enables a raw-line
                prefilter: a record whose serialized form does not contain the
                id as a substring cannot have it as a field value, so it is
                skipped before ``json.loads`` runs. This keeps a per-resource
                lookup from paying a full parse of the whole log. The prefilter
                is a superset test - a coincidental substring match still parses
                and is then rejected by the exact comparison below - so it can
                never drop a genuine match, only cheaply reject definite misses.
            include_archived: Also read archived ``*.jsonl.gz`` segments,
                replayed in chronological order *before* the live files, the
                same way :meth:`verify` walks them (#1835). Default False
                keeps the hot path reading only live segments.

                Callers that reason about *linkage* between events - rather
                than about individual events - need this. After retention an
                event's predecessor commonly lives in an archived segment, so
                a live-only read makes an intact chain look broken. Reading
                archives costs a decompress per segment, so it belongs on the
                paths that need completeness, not on every query.

        Returns:
            List of matching AuditEvent instances (chronological order).
            Lines that are not valid UTF-8 cannot be canonical records and
            are skipped whole (see :func:`_decode_segment_text`); they are
            never returned in laundered form, and ``verify`` names them.
        """
        results: list[AuditEvent] = []
        sources: list[bytes | None] = []
        if include_archived:
            discarded: list[str] = []
            sources.extend(_read_archived_segment(gz, discarded) for gz in _archived_segment_paths(self._audit_dir))
        sources.extend(path.read_bytes() for path in sorted(self._audit_dir.glob(_JSONL_GLOB)))

        for blob in sources:
            if blob is None:
                # Unreadable archive segment (corrupt gzip). ``verify`` reports
                # it with a named error; a query simply has nothing to yield.
                continue
            # Per-line strict decode: an undecodable line is dropped whole
            # rather than becoming replacement characters, so a query never
            # reports a record that differs from the bytes ``verify`` hashed -
            # and never raises forever over a chain carrying sealed tear
            # evidence, whose torn bytes are permanent by design.
            text = _decode_segment_text(blob)
            # Segment-level reject: a record can only carry ``resource_id`` as a
            # field value if that value appears verbatim in the segment's bytes.
            # A segment that never mentions the id holds no match, so skip it
            # whole - no line split, no per-line work. This is what keeps a
            # first-time approval resolve, and a stream of unknown card hashes,
            # off an O(chain) parse of the entire log.
            if resource_id and resource_id not in text:
                continue
            results.extend(
                _events_from_text(
                    text,
                    event_type=event_type,
                    actor=actor,
                    since=since,
                    until=until,
                    resource_id=resource_id,
                )
            )

        return results
