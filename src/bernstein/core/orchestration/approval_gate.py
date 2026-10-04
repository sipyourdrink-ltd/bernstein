"""Pre-spawn human-in-the-loop approval gate (#1110).

Provides :func:`wait_for_approval`, the runtime half of the explicit
approval gate that ships with :class:`bernstein.core.models.ApprovalSpec`.
On entry the gate writes a ``<task_id>.pending`` JSON sentinel under
``.sdd/runtime/approvals/`` and emits an ``approval_pending`` event into
the HMAC-chained audit log. Operator decisions arrive as
``<task_id>.approved`` / ``<task_id>.rejected`` decision records written by
``bernstein approve`` / ``bernstein reject``, the task server's approval
routes, or the chat bridge (the same files the post-completion review gate
uses).

A decision file is honoured only when it is an authentic decision record
(:mod:`bernstein.core.security.approval_decision`): its MAC verifies under
the install's decision key, it names this task, it sits in the slot matching
its outcome, and it carries the nonce this gate published in the ``.pending``
sentinel when it opened the request. Anything else in a decision slot
resolves the gate to rejected with ``decision_source="unverified-file"``.

The gate is intentionally synchronous: the orchestrator tick path is
file-driven and runs outside an asyncio loop, so polling with
``time.monotonic`` keeps the implementation simple and free of event-loop
ownership questions. Concurrency between racing ``bernstein approve``
calls is resolved by atomic ``os.replace`` writes; the first writer wins
and any subsequent writers see the resolved state and become no-ops with
a clear "already resolved" log line. Atomic file replacement is also
what makes the pending sentinel safe to read mid-update.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bernstein.core.security.approval_decision import (
    DecisionCheck,
    DecisionSource,
    build_decision_record,
    check_decision_file,
    inspect_decisions,
    new_request_nonce,
    quarantine_decision_file,
    write_decision_record,
)
from bernstein.core.security.path_containment import PathContainmentError, contained_path

if TYPE_CHECKING:
    from bernstein.core.security.audit import AuditLog
    from bernstein.core.tasks.models import ApprovalSpec

logger = logging.getLogger(__name__)

#: Outcome of a :func:`wait_for_approval` call.
ApprovalOutcome = Literal["approved", "rejected", "timeout"]

#: Name of this gate on its audit events (the review gate uses its own).
_GATE_NAME = "pre-spawn"

#: Default poll interval. Short enough to feel snappy in foreground, long
#: enough to keep filesystem load negligible in background runs.
_DEFAULT_POLL_INTERVAL_S: float = 0.5

#: Relative directory where every approval sentinel lives. Keeping all
#: gates under one folder lets ``bernstein pending`` enumerate every
#: outstanding decision in a single ``glob``.
_RUNTIME_REL = Path(".sdd") / "runtime" / "approvals"

#: The one rule for any identifier that becomes an approvals filename.
#:
#: Every sink under :data:`_RUNTIME_REL` derives its name from a caller-supplied
#: id (``<id>.pending`` / ``.approved`` / ``.rejected`` / ``.resumed``), so the
#: id is an identifier and never a path fragment. If two call sites can disagree
#: about this rule they eventually will, so all of them go through
#: :func:`approval_path_in`.
#:
#: **Length is 64**, matching the prevailing identifier rule in this codebase
#: (``replay.journal``, ``run_service.paths``, ``orchestration.missions``,
#: ``persistence.work_ledger``). A tighter bound here would refuse ids those
#: surfaces accept and strand them with no operator remedy. A caller with a
#: narrower downstream budget enforces that budget at its own boundary rather
#: than tightening this shared rule (see
#: :func:`bernstein.core.tasks.suspension.validate_task_id`, which additionally
#: caps at 59 because a parked task's journal run id is ``"task-" + task_id``).
#:
#: **The first character must be alphanumeric.** This is deliberately stricter
#: than the prevailing rule, which admits a leading dot and therefore matches
#: ``.`` and ``..``. Those surfaces are not always joined onto a directory; this
#: one always is, so traversal segments must be impossible here.
#:
#: **A colon is refused**, unlike ``evidence.run_artifacts`` which admits it for
#: MCP-supplied ids. A colon cannot be made safe for a path that is joined and
#: then written: on Windows ``C:evil`` parses as a drive-relative path, so
#: ``base / "C:evil.approved"`` discards the base entirely, and ``file:stream``
#: addresses an NTFS alternate data stream, which a containment check cannot
#: see because the path itself still looks contained. Verified for both shapes;
#: see the mismatch test in ``tests/unit/test_task_suspension.py``.
_APPROVAL_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class UnsafeApprovalIdError(ValueError):
    """Raised when an id cannot be used to derive an approvals filename.

    Fail closed: the identifier is rejected outright rather than sanitised, so
    a traversal attempt surfaces as a refusal instead of silently reading or
    writing a record outside the approvals directory.
    """


def validate_approval_id(approval_id: str) -> str:
    """Return *approval_id* if it is a safe single path segment, else refuse.

    Raises:
        UnsafeApprovalIdError: The id is empty, longer than 64 characters, or
            contains any character outside ``[A-Za-z0-9._-]`` (and it must
            start with an alphanumeric, which rules out ``.`` and ``..``).
    """
    if not _APPROVAL_ID_RE.match(approval_id):
        msg = f"refusing to derive an approvals path from unsafe id {approval_id!r}"
        raise UnsafeApprovalIdError(msg)
    return approval_id


def _approvals_dir(workdir: Path) -> Path:
    """Return the canonical approvals directory rooted at *workdir*."""
    return workdir / _RUNTIME_REL


def approval_path_in(approvals_dir: Path, approval_id: str, suffix: str) -> Path:
    """Return the contained ``<approvals_dir>/<approval_id><suffix>`` path.

    The single implementation every approvals sink resolves to. Two
    independent gates, both fail closed: the identifier allowlist above, then a
    resolved-path containment check. Resolving both the candidate and the base
    means a symlinked approvals directory is followed consistently, while a
    candidate landing anywhere outside the resolved base is refused -- which is
    what catches a symlinked decision file, something the allowlist alone
    cannot see.

    The second gate is :func:`contained_path`, which resolves the base the same
    way this function used to and refuses anything that is not a strict
    descendant of it. The refusal is re-raised as
    :class:`UnsafeApprovalIdError` so the three call sites keep catching one
    exception type.

    Args:
        approvals_dir: The approvals directory itself.
        approval_id: Task or approval identifier.
        suffix: File suffix including the dot, e.g. ``".approved"``.

    Raises:
        UnsafeApprovalIdError: The id is unsafe, or the resolved path escapes
            the approvals directory.
    """
    validate_approval_id(approval_id)
    try:
        candidate = contained_path(approvals_dir, f"{approval_id}{suffix}", label="approval id")
    except PathContainmentError as exc:
        msg = f"refusing approvals path outside {approvals_dir.resolve()} for id {approval_id!r}"
        raise UnsafeApprovalIdError(msg) from exc
    # contained_path proves a strict descendant, not a direct child: a single
    # segment that is itself a symlink deeper into the tree passes the prefix
    # test. Decision files are always direct children of the approvals dir.
    if candidate.parent != Path(os.path.realpath(approvals_dir)):
        msg = f"refusing approvals path outside {approvals_dir.resolve()} for id {approval_id!r}"
        raise UnsafeApprovalIdError(msg)
    return candidate


def approval_path(workdir: Path, approval_id: str, suffix: str) -> Path:
    """Return the contained ``<workdir>/.sdd/runtime/approvals/<id><suffix>``.

    Convenience wrapper over :func:`approval_path_in` for the common case where
    the caller holds a project root rather than the approvals directory.

    Raises:
        UnsafeApprovalIdError: The id is unsafe, or the resolved path escapes
            the approvals directory.
    """
    return approval_path_in(_approvals_dir(workdir), approval_id, suffix)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Write *payload* to *path* atomically via ``os.replace``.

    A temporary file is created in the same directory so the rename is on
    one filesystem (cross-filesystem rename is not atomic on POSIX). On
    failure the temp file is unlinked and the original exception
    propagates.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise


def _emit_audit(
    audit_log: AuditLog | None,
    *,
    event_type: str,
    task_id: str,
    details: dict[str, object],
) -> None:
    """Append an ``approval_*`` event to the HMAC-chained audit log.

    The lifecycle module owns the global :class:`AuditLog` singleton; we
    look it up dynamically when *audit_log* is ``None`` so callers do not
    have to thread the reference through. Failures are logged at debug
    level: an audit-log write should never block a gate decision.
    """
    log = audit_log
    if log is None:
        try:
            from bernstein.core.tasks.lifecycle import get_audit_log

            log = get_audit_log()
        except Exception:
            logger.debug("approval gate: audit log lookup failed", exc_info=True)
            return
    if log is None:
        return
    try:
        log.log(
            event_type=event_type,
            actor="approval_gate",
            resource_type="task",
            resource_id=task_id,
            details=details,
        )
    except Exception:
        # Audit log write is best-effort: a broken audit chain should
        # surface elsewhere (``bernstein verify``), not silently abort the
        # task lifecycle.
        logger.warning(
            "approval gate: failed to emit %s audit event for task %s",
            event_type,
            task_id,
            exc_info=True,
        )


#: Public name for the audit helper, shared with the post-completion review
#: gate in :mod:`bernstein.core.security.approval`.
emit_approval_audit = _emit_audit


def _publish_actor_event(
    *,
    session_id: str | None,
    kind: Literal["approval_requested", "approval_granted", "approval_denied"],
    task_id: str,
    extras: dict[str, object] | None = None,
) -> None:
    """Mirror an approval-gate decision into a registered :class:`RunActor`.

    The bridge is best-effort: if no actor is registered for the given
    session id (the common case today), this is a silent no-op. The
    file-driven gate remains the source of truth either way; the actor
    feed is an opt-in observability channel that runs alongside it.
    """
    if not session_id:
        return
    try:
        from bernstein.core.orchestration.run_actor import Event
        from bernstein.core.orchestration.run_actor_registry import (
            publish_event_sync,
        )
    except Exception:
        logger.debug("approval gate: actor bridge import failed", exc_info=True)
        return
    payload: dict[str, object] = {"approval_id": task_id, "task_id": task_id}
    if extras:
        payload.update(extras)
    try:
        publish_event_sync(
            session_id,
            Event(kind=kind, payload=payload, source="approval_gate"),
        )
    except Exception:
        logger.debug(
            "approval gate: actor bridge publish failed for %s",
            task_id,
            exc_info=True,
        )


def _pending_path(workdir: Path, task_id: str) -> Path:
    """Return the ``<task_id>.pending`` sentinel path (validated, contained)."""
    return approval_path(workdir, task_id, ".pending")


def _approved_path(workdir: Path, task_id: str) -> Path:
    """Return the ``<task_id>.approved`` decision path (validated, contained)."""
    return approval_path(workdir, task_id, ".approved")


def _rejected_path(workdir: Path, task_id: str) -> Path:
    """Return the ``<task_id>.rejected`` decision path (validated, contained)."""
    return approval_path(workdir, task_id, ".rejected")


def write_pending_sentinel(
    workdir: Path,
    task_id: str,
    spec: ApprovalSpec,
    *,
    now: float | None = None,
    nonce: str | None = None,
) -> Path:
    """Write the ``.pending`` sentinel for *task_id* and return its path.

    The sentinel content is:

    .. code-block:: json

        {
          "prompt": "...",
          "timeout_at_iso": "...",
          "created_iso": "...",
          "default_action": "reject",
          "nonce": "<hex>"
        }

    ``nonce`` identifies this approval request. Decision writers copy it
    into the signed decision record; the gate compares against the copy it
    holds in memory, so a record made for an earlier request does not
    resolve this one.

    This is the canonical hand-off between the orchestrator and the CLI:
    ``bernstein pending`` reads the sentinel to render the prompt while
    ``bernstein approve``/``reject`` write the corresponding decision
    file. The write is atomic so any concurrent reader observes either
    the previous content or the full new payload, never a partial one.

    Args:
        workdir: Project root (parent of ``.sdd/``).
        task_id: Identifier whose gate is being entered.
        spec: Approval specification governing this gate.
        now: Optional injected timestamp for deterministic tests.
        nonce: The request nonce; a fresh one is generated when omitted.

    Returns:
        Absolute path to the sentinel that was written.
    """
    moment = time.time() if now is None else now
    created = datetime.fromtimestamp(moment, tz=UTC)
    timeout_at = created + timedelta(seconds=spec.timeout_seconds)
    payload: dict[str, object] = {
        "task_id": task_id,
        "prompt": spec.prompt,
        "timeout_at_iso": timeout_at.isoformat(),
        "created_iso": created.isoformat(),
        "default_action": spec.default_action,
        "timeout_seconds": spec.timeout_seconds,
        "nonce": nonce if nonce is not None else new_request_nonce(),
    }
    path = _pending_path(workdir, task_id)
    _atomic_write_json(path, payload)
    return path


def request_pending_paths(workdir: Path, task_id: str) -> list[Path]:
    """Return the pending files that can carry *task_id*'s open request nonce.

    The pre-spawn gate publishes ``approvals/<id>.pending``; the review gate
    publishes ``pending_approvals/<id>.json``. Both names are derived from a
    validated id and contained in their directory.

    Raises:
        UnsafeApprovalIdError: The id is unsafe.
    """
    paths = [_pending_path(workdir, task_id)]
    review_dir = workdir / ".sdd" / "runtime" / "pending_approvals"
    if review_dir.is_dir():
        paths.append(approval_path_in(review_dir, task_id, ".json"))
    return paths


def record_decision(
    workdir: Path,
    task_id: str,
    outcome: Literal["approved", "rejected"],
    *,
    source: DecisionSource,
    principal: dict[str, str],
    reason: str = "",
) -> tuple[Path, bool, str]:
    """Write a signed decision record for *task_id* answering its open request.

    The single writer used by ``bernstein approve`` / ``reject`` and the chat
    bridge. The record is bound to the nonce of the currently open request
    (``""`` when none is open, in which case no gate will honour it).

    Returns:
        ``(path, created, nonce)`` -- the decision file, whether it was newly
        created, and the request nonce the record was bound to.

    Raises:
        UnsafeApprovalIdError: The id is unsafe.
        Exception: The decision key could not be loaded (the record is not
            written).
    """
    from bernstein.core.security.approval_decision import read_request_nonce

    path = approval_path(workdir, task_id, f".{outcome}")
    nonce = read_request_nonce(request_pending_paths(workdir, task_id))
    record = build_decision_record(
        task_id=task_id,
        outcome=outcome,
        source=source,
        principal=principal,
        nonce=nonce,
        reason=reason,
    )
    created = write_decision_record(path, record)
    return path, created, nonce


def _resolve_default_action(action: Literal["reject", "approve", "fail"]) -> ApprovalOutcome:
    """Translate a spec's ``default_action`` into the wait-for-approval outcome.

    ``"approve"`` lets the body run; ``"reject"`` and ``"fail"`` both halt
    it. The two non-approve values are kept distinct in the audit chain
    via the ``decision_source`` and ``outcome`` fields, but for the gate's
    runtime contract they collapse to ``"rejected"``.
    """
    if action == "approve":
        return "approved"
    return "rejected"


def settle_prior_decisions(
    approved: Path,
    rejected: Path,
    *,
    task_id: str,
    gate: str,
    audit_log: AuditLog | None = None,
) -> DecisionCheck | None:
    """Clear decision slots left over from earlier requests before a new one opens.

    Called by both task gates *before* they publish a new request nonce, so
    nothing in a slot at this point can answer the request about to open.

    * A genuine record (MAC and task binding hold) is an earlier decision for
      this task -- for example the pre-spawn approval of a task now at its
      review gate, or the decision on an earlier attempt. It is removed and an
      ``approval_decision_superseded`` audit event records what it was; it is
      not applied to the new request.
    * Anything else is not a decision anyone made through a decision path.
      It is returned so the caller resolves the new request as rejected with
      ``decision_source="unverified-file"`` (fail closed).

    Returns:
        The first unverified slot, or ``None`` when every present slot held a
        genuine earlier record (or no slot was occupied).
    """
    unverified: DecisionCheck | None = None
    for slot_path, slot in ((approved, "approved"), (rejected, "rejected")):
        if not slot_path.exists() and not slot_path.is_symlink():
            continue
        check = check_decision_file(
            slot_path,
            slot=slot,  # type: ignore[arg-type]
            task_id=task_id,
            expected_nonce=None,
        )
        if not check.verified:
            unverified = unverified or check
            continue
        with contextlib.suppress(FileNotFoundError):
            slot_path.unlink()
        details: dict[str, object] = {"gate": gate, **check.audit_details()}
        if check.record is not None:
            details["superseded_outcome"] = check.record.get("outcome", "")
            details["superseded_request_nonce"] = check.record.get("nonce", "")
        _emit_audit(audit_log, event_type="approval_decision_superseded", task_id=task_id, details=details)
    return unverified


def resolution_details(check: DecisionCheck, *, gate: str, request_nonce: str) -> dict[str, object]:
    """Return the ``approval_resolved`` audit details for a decision-file check.

    An unverified slot is quarantined (moved aside as ``<name>.unverified``)
    so it is kept as evidence without occupying the slot for the next request.
    """
    if not check.verified:
        quarantine_decision_file(check.path)
    return {
        "outcome": check.outcome,
        "gate": gate,
        "request_nonce": request_nonce,
        **check.audit_details(),
    }


def _write_timeout_record(
    path: Path, *, task_id: str, outcome: ApprovalOutcome, nonce: str, default_action: str
) -> None:
    """Persist the timeout-default resolution as a signed decision record.

    Best-effort: if the record cannot be signed or written, nothing is left
    in the slot rather than an unsigned file the next request would treat as
    unverified.
    """
    from bernstein.core.approval.models import internal_principal

    try:
        record = build_decision_record(
            task_id=task_id,
            outcome="approved" if outcome == "approved" else "rejected",
            source="timeout-default",
            principal=internal_principal("approval-gate/timeout").to_dict(),
            nonce=nonce,
            reason=f"timeout-default:{default_action}",
        )
        write_decision_record(path, record)
    except Exception as exc:
        logger.warning(
            "approval gate: could not persist timeout decision for task %s: %s",
            task_id,
            exc,
        )


def _cleanup_pending(workdir: Path, task_id: str) -> None:
    """Remove the ``.pending`` sentinel; ignore missing-file errors.

    Called once a decision is recorded so :func:`list_pending_approvals`
    does not surface stale entries on the next tick.
    """
    pending = _pending_path(workdir, task_id)
    with contextlib.suppress(FileNotFoundError, OSError):
        pending.unlink()


def list_pending_approvals(workdir: Path) -> list[dict[str, object]]:
    """Return every active ``<task_id>.pending`` sentinel under *workdir*.

    Used by ``bernstein pending`` to surface approval-pending tasks
    distinct from the post-completion review queue. Each entry is the raw
    JSON dict written by :func:`write_pending_sentinel`, augmented with a
    ``task_id`` key derived from the filename for callers that prefer it
    over digging into the body.
    """
    approvals_dir = _approvals_dir(workdir)
    if not approvals_dir.exists():
        return []
    entries: list[dict[str, object]] = []
    for sentinel in sorted(approvals_dir.glob("*.pending")):
        try:
            data = json.loads(sentinel.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("approval gate: skipping unreadable sentinel %s: %s", sentinel.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        data.setdefault("task_id", sentinel.stem)
        entries.append(data)
    return entries


def wait_for_approval(
    task_id: str,
    spec: ApprovalSpec,
    *,
    workdir: Path | None = None,
    audit_log: AuditLog | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    now: float | None = None,
    monotonic: object | None = None,
    sleep: object | None = None,
    session_id: str | None = None,
) -> ApprovalOutcome:
    """Block until the operator decides or the TTL fires.

    The gate is idempotent under concurrent ``bernstein approve`` /
    ``bernstein reject`` calls: the underlying decision files are written
    via atomic rename, so the first writer's contents persist and any
    subsequent writer's content is silently dropped at the filesystem
    level. The CLI commands also detect already-resolved tasks and emit
    a "already resolved" message instead of re-resolving.

    Each call opens a new request with a fresh nonce. A decision file is
    honoured only when it is an authentic decision record for this task
    and this request (see :mod:`bernstein.core.security.approval_decision`);
    a genuine record from an earlier request is superseded when the request
    opens, and anything else in a decision slot resolves the gate to
    ``"rejected"`` with ``decision_source="unverified-file"``.

    Args:
        task_id: Identifier of the task being gated; appears in audit
            events and on-disk sentinels.
        spec: Approval specification driving prompt, timeout, and
            default action.
        workdir: Project root. Defaults to the current working directory.
        audit_log: Optional explicit audit log; defaults to the
            lifecycle-registered singleton.
        session_id: Optional run-actor session id. When set, the gate
            mirrors ``approval_requested`` / ``approval_granted`` /
            ``approval_denied`` events into a :class:`RunActor`
            registered under that id (see
            :mod:`bernstein.core.orchestration.run_actor_registry`). The
            mirror is best-effort and never affects the file-driven
            decision contract.
        poll_interval_s: Seconds between filesystem checks.
        now: Optional injected wall-clock timestamp (used by
            :func:`write_pending_sentinel` for deterministic ISO strings).
        monotonic: Optional injected monotonic clock - useful in tests
            that drive the timeout deterministically. Must accept zero
            arguments and return a float; defaults to
            :func:`time.monotonic`.
        sleep: Optional injected sleep callable. Must accept a float;
            defaults to :func:`time.sleep`.

    Returns:
        ``"approved"``, ``"rejected"``, or ``"timeout"``.

    Notes:
        On ``"timeout"`` the gate also resolves the task by writing the
        appropriate decision file derived from ``spec.default_action``,
        so subsequent CLI calls see a terminal state and the on-disk
        history mirrors the in-memory outcome.
    """
    root = workdir if workdir is not None else Path.cwd()
    monotonic_clock = monotonic if callable(monotonic) else time.monotonic
    sleep_fn = sleep if callable(sleep) else time.sleep

    approvals_dir = _approvals_dir(root)
    approvals_dir.mkdir(parents=True, exist_ok=True)
    approved = _approved_path(root, task_id)
    rejected = _rejected_path(root, task_id)

    # Anything already in a decision slot predates this request. A genuine
    # earlier record is superseded; anything else fails the gate closed.
    prior_unverified = settle_prior_decisions(approved, rejected, task_id=task_id, gate=_GATE_NAME, audit_log=audit_log)

    # Open the request: the nonce stays in memory and is published in the
    # sentinel for decision writers to bind their record to.
    nonce = new_request_nonce()
    write_pending_sentinel(root, task_id, spec, now=now, nonce=nonce)
    _emit_audit(
        audit_log,
        event_type="approval_pending",
        task_id=task_id,
        details={
            "prompt": spec.prompt,
            "timeout_seconds": spec.timeout_seconds,
            "default_action": spec.default_action,
            "gate": _GATE_NAME,
            "request_nonce": nonce,
        },
    )
    _publish_actor_event(
        session_id=session_id,
        kind="approval_requested",
        task_id=task_id,
        extras={"prompt": spec.prompt},
    )

    def _resolve(check: DecisionCheck) -> ApprovalOutcome:
        details = resolution_details(check, gate=_GATE_NAME, request_nonce=nonce)
        if not check.verified:
            logger.warning(
                "approval gate: task %s decision file %s is not an authentic decision record (%s) -- rejecting",
                task_id,
                check.path.name,
                check.failure,
            )
        _emit_audit(audit_log, event_type="approval_resolved", task_id=task_id, details=details)
        _publish_actor_event(
            session_id=session_id,
            kind="approval_granted" if check.outcome == "approved" else "approval_denied",
            task_id=task_id,
            extras={"decision_source": check.source},
        )
        _cleanup_pending(root, task_id)
        return check.outcome

    if prior_unverified is not None:
        return _resolve(prior_unverified)

    deadline = monotonic_clock() + float(spec.timeout_seconds)  # type: ignore[operator]
    while True:
        decision = inspect_decisions(approved, rejected, task_id=task_id, expected_nonce=nonce)
        if decision is not None:
            return _resolve(decision)

        remaining = deadline - monotonic_clock()  # type: ignore[operator]
        if remaining <= 0:
            outcome = _resolve_default_action(spec.default_action)
            # Persist a signed decision record so future readers see a
            # terminal state attributed to the timeout policy.
            _write_timeout_record(
                approved if outcome == "approved" else rejected,
                task_id=task_id,
                outcome=outcome,
                nonce=nonce,
                default_action=spec.default_action,
            )
            _emit_audit(
                audit_log,
                event_type="approval_resolved",
                task_id=task_id,
                details={
                    "outcome": "timeout",
                    "decision_source": "timeout-default",
                    "default_action": spec.default_action,
                    "applied_outcome": outcome,
                    "gate": _GATE_NAME,
                    "request_nonce": nonce,
                },
            )
            _publish_actor_event(
                session_id=session_id,
                kind="approval_granted" if outcome == "approved" else "approval_denied",
                task_id=task_id,
                extras={
                    "decision_source": "timeout-default",
                    "default_action": spec.default_action,
                },
            )
            _cleanup_pending(root, task_id)
            return "timeout"

        sleep_fn(min(poll_interval_s, remaining))  # type: ignore[operator]


__all__ = [
    "ApprovalOutcome",
    "DecisionSource",
    "emit_approval_audit",
    "list_pending_approvals",
    "record_decision",
    "request_pending_paths",
    "resolution_details",
    "settle_prior_decisions",
    "wait_for_approval",
    "write_pending_sentinel",
]
