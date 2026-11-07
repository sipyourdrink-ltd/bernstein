"""Authenticated decision records for the file-based approval gates.

Both task approval gates -- the pre-spawn gate in
:mod:`bernstein.core.orchestration.approval_gate` and the post-completion
review gate in :mod:`bernstein.core.security.approval` -- learn an operator's
decision from ``<task_id>.approved`` / ``<task_id>.rejected`` files under
``.sdd/runtime/approvals/``. Those files sit inside the project working
directory, which agents can write to. A file's existence therefore says
nothing about who decided.

This module turns the decision file into a record that a gate can check:

* **Record.** A JSON object naming the task, the outcome, the decision path
  (``cli`` / ``web`` / ``chat`` / ``tui`` / ``timeout-default``), the principal
  that decided, the decision time, and the nonce of the approval request it
  answers.
* **Authentication.** An HMAC-SHA256 over the canonical record bytes, keyed by
  a per-purpose key derived (HKDF-SHA256, :mod:`key_derivation`) from the
  install's audit master key. That key lives outside the project directory
  (``$XDG_STATE_HOME/bernstein/audit.key`` or ``$BERNSTEIN_AUDIT_KEY_PATH``,
  mode ``0600``), so the CLI, the task server, and the orchestrator can all
  load it while nothing under ``.sdd/`` carries it.
* **Binding.** Each gate opens a request with a fresh random nonce, keeps it
  in memory, and publishes it in its pending file. A record is honoured only
  when its MAC verifies, its task id equals the gated task, its outcome
  matches the file it sits in, and its nonce equals the open request's nonce.

Anything else found in a decision slot -- an empty file, a legacy plain-text
file, a record with a bad MAC, a record for another task or for an earlier
request -- is *unverified*, and a gate resolves it as a rejection with the
decision source ``unverified-file``.

What this does not protect against: a process running as the same OS user
can read the audit key and so can mint a valid record deliberately. The
record stops writes that merely create or copy a file in the decision slot,
and it keeps the audit trail honest about which path a decision came from.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from bernstein.core.security.key_derivation import (
    DOMAIN_APPROVAL_DECISION,
    derive_store_key,
    domain_tag,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "RECORD_SCHEMA",
    "SIGNABLE_SOURCES",
    "UNVERIFIED_SOURCE",
    "DecisionCheck",
    "DecisionOutcome",
    "DecisionSource",
    "build_decision_record",
    "check_decision_file",
    "decision_key",
    "inspect_decisions",
    "new_request_nonce",
    "quarantine_decision_file",
    "read_request_nonce",
    "write_decision_record",
]

#: Outcome a decision record can carry; also the decision-file suffix.
DecisionOutcome = Literal["approved", "rejected"]

#: The path a gate resolution came through, as recorded in the audit chain.
#:
#: ``cli`` -- ``bernstein approve`` / ``bernstein reject``; ``web`` -- the task
#: server's ``/approvals/{id}/approve|reject`` routes; ``chat`` -- the chat
#: bridge (``bernstein chat``); ``tui`` -- reserved for a TUI writer;
#: ``timeout-default`` -- the gate applied its configured default when nobody
#: decided; ``unverified-file`` -- a decision slot held something that is not
#: an authentic record, and the gate failed closed.
DecisionSource = Literal["cli", "tui", "web", "chat", "timeout-default", "unverified-file"]

#: Sources a signed record may carry. ``unverified-file`` is a gate verdict,
#: never something a writer can claim.
SIGNABLE_SOURCES: Final[frozenset[str]] = frozenset({"cli", "tui", "web", "chat", "timeout-default"})

#: Source label for a decision slot that failed verification.
UNVERIFIED_SOURCE: Final[DecisionSource] = "unverified-file"

#: Schema marker embedded in every record.
RECORD_SCHEMA: Final[str] = "bernstein.approval-decision/v1"

#: Upper bound on a decision file the gate is willing to parse. A real record
#: is a few hundred bytes; anything far larger is not one.
_MAX_RECORD_BYTES: Final[int] = 64 * 1024

_OUTCOMES: Final[frozenset[str]] = frozenset({"approved", "rejected"})


def new_request_nonce() -> str:
    """Return a fresh 128-bit hex nonce for one approval request."""
    return secrets.token_hex(16)


def decision_key(master_key: bytes | None = None) -> bytes:
    """Return the HMAC key that authenticates decision records.

    Derived from the install's audit master key under a dedicated domain, so a
    decision MAC can never be confused with an audit-chain or lineage MAC.

    Args:
        master_key: Explicit master key (tests). Defaults to the key returned
            by :func:`bernstein.core.security.audit.load_or_create_audit_key`.
    """
    if master_key is None:
        from bernstein.core.security.audit import load_or_create_audit_key

        master_key = load_or_create_audit_key()
    return derive_store_key(master_key, DOMAIN_APPROVAL_DECISION)


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the canonical bytes the MAC covers (domain tag + sorted JSON)."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return domain_tag(DOMAIN_APPROVAL_DECISION).encode("utf-8") + b"\n" + body.encode("utf-8")


def _mac(key: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(key, _canonical_bytes(payload), hashlib.sha256).hexdigest()


def build_decision_record(
    *,
    task_id: str,
    outcome: DecisionOutcome,
    source: DecisionSource,
    principal: Mapping[str, str],
    nonce: str,
    reason: str = "",
    decided_at: float | None = None,
    key: bytes | None = None,
) -> dict[str, Any]:
    """Return a signed decision record ready to be written to disk.

    Args:
        task_id: The gated task the decision applies to.
        outcome: ``"approved"`` or ``"rejected"``.
        source: The decision path; must be one of :data:`SIGNABLE_SOURCES`.
        principal: Who decided, as :meth:`ApprovalPrincipal.to_dict` returns.
        nonce: The open request's nonce, or ``""`` when no request is open.
        reason: Optional free-text reason supplied by the decider.
        decided_at: Decision time (epoch seconds); defaults to now.
        key: Explicit decision key (tests); defaults to :func:`decision_key`.

    Raises:
        ValueError: The outcome or source is not one a writer may record, or
            the principal has no identifier.
    """
    if outcome not in _OUTCOMES:
        raise ValueError(f"unknown decision outcome {outcome!r}")
    if source not in SIGNABLE_SOURCES:
        raise ValueError(f"decision source {source!r} cannot be recorded by a writer")
    if not str(principal.get("identifier", "")).strip():
        raise ValueError("a decision record must name the principal that decided")
    moment = time.time() if decided_at is None else decided_at
    payload: dict[str, Any] = {
        "schema": RECORD_SCHEMA,
        "task_id": task_id,
        "outcome": outcome,
        "source": source,
        "principal": {str(k): str(v) for k, v in principal.items()},
        "nonce": nonce,
        "reason": reason,
        "decided_at": datetime.fromtimestamp(moment, tz=UTC).isoformat(),
    }
    payload["mac"] = _mac(key if key is not None else decision_key(), payload)
    return payload


def write_decision_record(path: Path, record: Mapping[str, Any]) -> bool:
    """Atomically write *record* to *path*; return ``True`` if newly created.

    The write goes through a same-directory temp file and ``os.replace`` so a
    polling gate never observes a partial record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pre_exists = path.exists()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(dict(record), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise
    return not pre_exists


def read_request_nonce(pending_paths: Iterable[Path]) -> str:
    """Return the nonce of the open approval request, or ``""`` if none.

    A task can have a pending file for the pre-spawn gate
    (``approvals/<id>.pending``) or the review gate
    (``pending_approvals/<id>.json``). When several exist the most recently
    written one is the open request. The nonce is not a secret: it binds a
    decision to one request, and the gate compares against the copy it holds
    in memory, so tampering with the pending file can only make a decision
    fail to verify.
    """
    candidates: list[tuple[float, str]] = []
    for path in pending_paths:
        try:
            mtime = path.stat().st_mtime
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            nonce = data.get("nonce")
            if isinstance(nonce, str) and nonce:
                candidates.append((mtime, nonce))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


@dataclass(frozen=True)
class DecisionCheck:
    """Result of checking one decision file.

    Attributes:
        slot: Which decision file was checked (``approved`` / ``rejected``).
        path: The file that was checked.
        verified: ``True`` only when the record authenticated and bound to the
            expected task, slot, and (when given) request nonce.
        failure: Short machine-readable reason when not verified.
        record: The parsed record when its MAC verified (even if a binding
            check then failed), else ``None``.
    """

    slot: DecisionOutcome
    path: Path
    verified: bool
    failure: str = ""
    record: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def outcome(self) -> DecisionOutcome:
        """The outcome a gate applies: the slot if verified, else rejected."""
        return self.slot if self.verified else "rejected"

    @property
    def source(self) -> DecisionSource:
        """The recorded decision path, or ``unverified-file``."""
        if self.verified and self.record is not None:
            return cast("DecisionSource", self.record["source"])
        return UNVERIFIED_SOURCE

    @property
    def principal(self) -> dict[str, str]:
        """The recorded principal for a verified record, else ``{}``."""
        if self.verified and self.record is not None:
            return dict(self.record["principal"])
        return {}

    def audit_details(self) -> dict[str, object]:
        """Return the fields every gate puts on its resolution audit event."""
        details: dict[str, object] = {
            "decision_source": self.source,
            "decision_file": self.path.name,
            "verified": self.verified,
        }
        if self.verified and self.record is not None:
            principal = self.principal
            details["principal"] = principal.get("identifier", "")
            details["principal_auth_method"] = principal.get("auth_method", "")
            details["principal_kind"] = principal.get("kind", "")
            details["principal_grant"] = principal.get("grant", "")
            details["decided_at"] = self.record.get("decided_at", "")
        else:
            details["verification_failure"] = self.failure
            if self.record is not None:
                # MAC held but the binding did not: say what it claimed.
                details["claimed_task_id"] = self.record.get("task_id", "")
                details["claimed_source"] = self.record.get("source", "")
        return details


class _NotRegularFileError(OSError):
    """The decision slot is a link, FIFO, device, or directory."""


def _read_regular_file(path: Path) -> bytes:
    """Read at most ``_MAX_RECORD_BYTES + 1`` bytes from a regular file.

    Opened without following a final symlink and without blocking, then
    checked with ``fstat``: a FIFO or device planted in the decision slot must
    not stall the gate's poll loop.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if path.is_symlink():
            raise _NotRegularFileError(str(exc)) from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _NotRegularFileError(path.name)
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_RECORD_BYTES:
            chunk = os.read(fd, _MAX_RECORD_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def check_decision_file(
    path: Path,
    *,
    slot: DecisionOutcome,
    task_id: str,
    expected_nonce: str | None,
    key: bytes | None = None,
) -> DecisionCheck:
    """Check that *path* holds an authentic decision record for *task_id*.

    Args:
        path: The decision file (must exist; callers check first).
        slot: The outcome implied by the file name.
        task_id: The gated task.
        expected_nonce: The open request's nonce. ``None`` skips the nonce
            check (used to recognise a genuine record left by an earlier
            request, and by paths that have no request nonce).
        key: Explicit decision key (tests); defaults to :func:`decision_key`.
    """

    def _fail(reason: str, record: dict[str, Any] | None = None) -> DecisionCheck:
        return DecisionCheck(slot=slot, path=path, verified=False, failure=reason, record=record)

    try:
        raw = _read_regular_file(path)
    except _NotRegularFileError:
        return _fail("not-a-regular-file")
    except OSError:
        return _fail("unreadable")
    if len(raw) > _MAX_RECORD_BYTES:
        return _fail("oversized")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _fail("not-a-record")
    if not isinstance(data, dict) or data.get("schema") != RECORD_SCHEMA:
        return _fail("not-a-record")
    mac = data.get("mac")
    if not isinstance(mac, str) or not mac:
        return _fail("unsigned")
    payload = {k: v for k, v in data.items() if k != "mac"}
    try:
        expected_mac = _mac(key if key is not None else decision_key(), payload)
    except (TypeError, ValueError):
        return _fail("not-a-record")
    except Exception:
        # Key could not be loaded (permissions, missing state dir): nothing
        # can be verified, so nothing is honoured.
        logger.warning("approval decision: decision key unavailable; treating %s as unverified", path.name)
        return _fail("key-unavailable")
    if not hmac.compare_digest(mac, expected_mac):
        return _fail("bad-signature")
    record = cast("dict[str, Any]", data)
    principal = record.get("principal")
    if (
        not isinstance(principal, dict)
        or not str(principal.get("identifier", "")).strip()
        or record.get("source") not in SIGNABLE_SOURCES
        or record.get("outcome") not in _OUTCOMES
    ):
        return _fail("malformed-record", record)
    if record.get("task_id") != task_id:
        return _fail("task-mismatch", record)
    if record.get("outcome") != slot:
        return _fail("outcome-mismatch", record)
    if expected_nonce is not None and not hmac.compare_digest(str(record.get("nonce", "")), expected_nonce):
        return _fail("nonce-mismatch", record)
    return DecisionCheck(slot=slot, path=path, verified=True, record=record)


def inspect_decisions(
    approved: Path,
    rejected: Path,
    *,
    task_id: str,
    expected_nonce: str | None,
    key: bytes | None = None,
) -> DecisionCheck | None:
    """Return the decision a gate should apply, or ``None`` if none is present.

    Fails closed: if either slot holds something unverified, that unverified
    check is returned (a rejection). If both slots hold verified records, the
    rejection wins.
    """
    checks: list[DecisionCheck] = []
    if approved.exists() or approved.is_symlink():
        checks.append(
            check_decision_file(approved, slot="approved", task_id=task_id, expected_nonce=expected_nonce, key=key)
        )
    if rejected.exists() or rejected.is_symlink():
        checks.append(
            check_decision_file(rejected, slot="rejected", task_id=task_id, expected_nonce=expected_nonce, key=key)
        )
    if not checks:
        return None
    for check in checks:
        if not check.verified:
            return check
    for check in checks:
        if check.slot == "rejected":
            return check
    return checks[0]


def quarantine_decision_file(path: Path) -> Path | None:
    """Move an unverified decision file aside as ``<name>.unverified``.

    The file is kept as evidence and the decision slot is cleared, so the
    next request for the same task starts from an empty slot. Best-effort.
    """
    target = path.with_name(path.name + ".unverified")
    try:
        os.replace(path, target)
    except OSError:
        logger.warning("approval decision: could not quarantine %s", path.name, exc_info=True)
        return None
    return target
