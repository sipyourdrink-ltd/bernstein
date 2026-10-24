"""List sandbox sessions that outlived the task they were provisioned for (#5112).

The spawner provisions one sandbox session per agent spawn and destroys it
when the exec resolves. Both ends are chained into the audit log
(``sandbox.session_create`` / ``sandbox.session_destroy``). A create with no
later destroy is a session Bernstein never tore down: the orchestrator died
mid-exec, or the backend's ``destroy`` raised and the spawner only logged it.
Such a session keeps holding a container, a VM, or a hosted sandbox until
someone notices.

Whether it is *leaked* is a question about the owning task, not about the
session: a session whose task is still running is supposed to exist. This
module answers it from the task store's own replay of ``tasks.jsonl`` - the
same last-record-wins projection the task server rebuilds on startup -
rather than from a process-liveness or file-age heuristic. A session is
listed as leaked only when every task it was provisioned for is recorded
finished: one of :data:`~bernstein.core.tasks.lifecycle.LANE_FINISHED_STATUSES`
(``done``, ``failed``, ``closed``, ``cancelled``, ``abandoned``, ``refused``).
Anything less certain is reported as excluded, with the reason:

* the create record names no owning task (written before the spawner
  recorded one, or malformed);
* an owning task has no record in the task store;
* an owning task is in any other status, including ``orphaned`` (pending
  crash recovery) and ``suspended`` (resumable).

It fails closed rather than list from a partial source: a task log line the
store would skip (a torn append or missing task id), or an audit segment it cannot read, stops the
sweep with the file named.

The sweep is read-only. It never destroys a session, never appends to the
audit chain, never mints an audit key, and never creates a directory.

It reads the sandbox records without re-verifying the HMAC chain. The listing
is advisory - it gates and removes nothing - so an unverified record can only
mislead it (a destroy appended without the audit key would hide a leak), and
``bernstein audit verify``, which rejects such a record, is the step to run
before a listing drives a removal.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from bernstein.core.tasks.lifecycle import LANE_FINISHED_STATUSES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from bernstein.core.security.audit import AuditEvent

logger = logging.getLogger(__name__)

#: Status values that make a task finished for this sweep:
#: :data:`~bernstein.core.tasks.lifecycle.LANE_FINISHED_STATUSES`, the set the
#: lane sweeps share, so they never disagree about the same task.
_FINISHED: frozenset[str] = frozenset(status.value for status in LANE_FINISHED_STATUSES)

__all__ = [
    "OpenSandboxSession",
    "SandboxLeakCandidate",
    "SandboxLeakVerdict",
    "SandboxSweepUnavailableError",
    "classify_open_session",
    "open_sandbox_sessions",
    "sweep_leaked_sandboxes",
]


class SandboxSweepUnavailableError(RuntimeError):
    """The audit log or the task store could not be read, so nothing is listed.

    The sweep fails closed: a source it cannot read is never taken to mean
    "no live tasks".
    """


class SandboxLeakVerdict(StrEnum):
    """Outcome for one sandbox session that was created and never destroyed."""

    LEAKED = "leaked"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class OpenSandboxSession:
    """A ``sandbox.session_create`` with no later ``sandbox.session_destroy``.

    Attributes:
        sandbox_session_id: Backend-allocated session id (the audit
            ``resource_id``).
        backend: Backend name recorded at create time (``docker``, ``e2b``, ...).
        agent_session_id: Bernstein agent session the sandbox was provisioned for.
        task_ids: Tasks the spawn ran. Empty when the create record names none.
        created_at: ISO-8601 timestamp of the create record.
        superseded: True when a later create reused this id before any
            destroy. The id now names the newer session, so this lifetime can
            no longer be addressed by it.
    """

    sandbox_session_id: str
    backend: str
    agent_session_id: str
    task_ids: tuple[str, ...]
    created_at: str
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class SandboxLeakCandidate:
    """One open sandbox session and the sweep's verdict on it.

    Attributes:
        session: The open session.
        verdict: :attr:`SandboxLeakVerdict.LEAKED` only when every owning task
            is recorded terminal.
        reason: Why the session was listed or left out, for the operator.
        task_statuses: Recorded status per owning task; ``None`` when the
            task store has no record of it.
        age_seconds: Seconds since the create record, or ``None`` when its
            timestamp cannot be parsed.
    """

    session: OpenSandboxSession
    verdict: SandboxLeakVerdict
    reason: str
    task_statuses: Mapping[str, str | None]
    age_seconds: float | None

    @property
    def is_leaked(self) -> bool:
        return self.verdict is SandboxLeakVerdict.LEAKED


def _task_ids_from_details(raw: object) -> tuple[str, ...]:
    """The owning task ids, or ``()`` when the field is absent or malformed.

    Only a list of non-empty strings is accepted. A bare string is rejected
    rather than split into characters, and a list with any non-string or
    empty entry is rejected whole: a partly readable owner list could drop
    the one task that is still running.
    """
    if not isinstance(raw, list) or not raw:
        return ()
    if not all(isinstance(item, str) and item for item in raw):
        return ()
    return tuple(dict.fromkeys(raw))


def open_sandbox_sessions(events: Iterable[AuditEvent]) -> list[OpenSandboxSession]:
    """Pair create and destroy records; return the sessions never destroyed.

    *events* must be in chain order. A destroy closes the most recent create
    for the same session id, so an id a backend reuses after a destroy is
    tracked per lifetime. A destroy with no create in view (its create aged
    into a segment the caller did not read) is ignored.

    A create for an id that is still open does not replace the earlier
    lifetime silently: that lifetime is returned as well, marked
    ``superseded``, and a later destroy closes only the newest one.
    """
    from bernstein.core.security.audit import SANDBOX_SESSION_CREATE, SANDBOX_SESSION_DESTROY

    open_by_id: dict[str, OpenSandboxSession] = {}
    superseded: list[OpenSandboxSession] = []
    for event in events:
        if event.event_type == SANDBOX_SESSION_DESTROY:
            open_by_id.pop(event.resource_id, None)
            continue
        if event.event_type != SANDBOX_SESSION_CREATE or not event.resource_id:
            continue
        details = event.details if isinstance(event.details, dict) else {}
        earlier = open_by_id.get(event.resource_id)
        if earlier is not None:
            superseded.append(replace(earlier, superseded=True))
        open_by_id[event.resource_id] = OpenSandboxSession(
            sandbox_session_id=event.resource_id,
            backend=str(details.get("backend") or "unknown"),
            agent_session_id=str(details.get("agent_session_id") or ""),
            task_ids=_task_ids_from_details(details.get("task_ids")),
            created_at=event.timestamp,
        )
    return [*open_by_id.values(), *superseded]


def _age_seconds(created_at: str, now: float) -> float | None:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max(0.0, now - created.timestamp())


def classify_open_session(
    session: OpenSandboxSession,
    task_status: Callable[[str], str | None],
    *,
    now: float,
) -> SandboxLeakCandidate:
    """Decide whether *session* is leaked from its owning tasks' recorded status.

    Args:
        session: An open session from :func:`open_sandbox_sessions`.
        task_status: Returns a task's recorded status value, or ``None`` when
            the task store has no record of it.
        now: Unix time the age is measured against.
    """
    age = _age_seconds(session.created_at, now)
    if session.superseded:
        # Never leaked, whatever its tasks say: the id now names the newer
        # session, and acting on this listing by id would reach that one.
        return SandboxLeakCandidate(
            session=session,
            verdict=SandboxLeakVerdict.EXCLUDED,
            reason="session id was created again before this lifetime was destroyed; it now names the newer session",
            task_statuses={task_id: task_status(task_id) for task_id in session.task_ids},
            age_seconds=age,
        )
    if not session.task_ids:
        return SandboxLeakCandidate(
            session=session,
            verdict=SandboxLeakVerdict.EXCLUDED,
            reason="create record names no owning task",
            task_statuses={},
            age_seconds=age,
        )
    statuses = {task_id: task_status(task_id) for task_id in session.task_ids}
    unknown = [task_id for task_id, status in statuses.items() if status is None]
    if unknown:
        reason = f"no task-store record for {', '.join(unknown)}"
        verdict = SandboxLeakVerdict.EXCLUDED
    elif running := [f"{tid} is {st}" for tid, st in statuses.items() if st not in _FINISHED]:
        reason = "; ".join(running)
        verdict = SandboxLeakVerdict.EXCLUDED
    else:
        reason = ", ".join(f"{tid} {st}" for tid, st in statuses.items())
        verdict = SandboxLeakVerdict.LEAKED
    return SandboxLeakCandidate(
        session=session,
        verdict=verdict,
        reason=reason,
        task_statuses=statuses,
        age_seconds=age,
    )


def _read_sandbox_events(audit_dir: Path) -> list[AuditEvent]:
    from bernstein.core.security.audit import (
        SANDBOX_SESSION_CREATE,
        SANDBOX_SESSION_DESTROY,
        AuditKeyMissingError,
        AuditKeyPermissionError,
        AuditLog,
        load_audit_key,
    )

    try:
        key = load_audit_key()
    except (AuditKeyMissingError, AuditKeyPermissionError) as exc:
        raise SandboxSweepUnavailableError(f"cannot open the audit log: {exc}") from exc
    unreadable: list[str] = []
    try:
        events = AuditLog(audit_dir, key=key).query(include_archived=True, unreadable=unreadable)
    except OSError as exc:
        raise SandboxSweepUnavailableError(f"cannot read the audit log at {audit_dir}: {exc}") from exc
    if unreadable:
        # A segment lost here may hold the create (hiding a leak) or the destroy
        # (inventing one); either way the pairing below cannot be trusted.
        lost = "; ".join(unreadable)
        raise SandboxSweepUnavailableError(f"cannot read every audit segment under {audit_dir}: {lost}")
    return [e for e in events if e.event_type in (SANDBOX_SESSION_CREATE, SANDBOX_SESSION_DESTROY)]


def _check_task_log_is_whole(jsonl_path: Path) -> None:
    """Refuse a task log the store would replay with a record silently dropped.

    ``TaskStore.replay_jsonl`` logs and skips a line that does not parse - a
    torn append is a documented artifact of interleaved writes - and replays the
    rest. The task then reads back at an *older* status, and older usually means
    more finished: a torn ``done -> open`` reopen leaves the task ``done`` and its
    live session listed as leaked. The sweep reads what replay reads, the live
    log, with the same line rules (whitespace-only lines are skipped), and fails
    closed on the first line it cannot use, naming it.

    The archive (``archive/tasks.jsonl``) is not scanned because replay does not
    read it. If replay's file set ever widens, this scan must widen with it;
    ``test_the_task_archive_feeds_neither_replay_nor_the_torn_line_scan`` fails
    when that happens.
    """
    import json

    if not jsonl_path.exists():
        return
    try:
        lines = jsonl_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SandboxSweepUnavailableError(f"cannot read the task log {jsonl_path}: {exc}") from exc
    for line_num, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise SandboxSweepUnavailableError(
                f"{jsonl_path}:{line_num}: task record does not parse (a torn append?); "
                "the task store would skip it and replay an older status, so nothing is listed"
            ) from None
        if not isinstance(record, dict):
            raise SandboxSweepUnavailableError(f"{jsonl_path}:{line_num}: task record is not a JSON object")
        if not record.get("id"):
            raise SandboxSweepUnavailableError(
                f"{jsonl_path}:{line_num}: task record has no id; "
                "the task store would skip it and replay an older status, so nothing is listed"
            )


def _load_task_statuses(sdd_dir: Path) -> Callable[[str], str | None]:
    """Replay the task log the way the task server does on startup.

    Returns a lookup from task id to its latest recorded status value.
    """
    from bernstein.core.tasks.models import TaskStoreUnavailable
    from bernstein.core.tasks.task_store import TaskStore

    jsonl_path = sdd_dir / "runtime" / "tasks.jsonl"
    _check_task_log_is_whole(jsonl_path)
    store = TaskStore(
        jsonl_path=jsonl_path,
        archive_path=sdd_dir / "archive" / "tasks.jsonl",
    )
    try:
        store.replay_jsonl()
    except (TaskStoreUnavailable, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SandboxSweepUnavailableError(f"cannot replay the task log under {sdd_dir}: {exc}") from exc

    def _status(task_id: str) -> str | None:
        task = store.get_task(task_id)
        return None if task is None else task.status.value

    return _status


def sweep_leaked_sandboxes(workdir: Path, *, now: float | None = None) -> list[SandboxLeakCandidate]:
    """List every sandbox session created under *workdir* and never destroyed.

    Leaked sessions come first, then excluded ones; each group is ordered
    oldest first.

    Args:
        workdir: Project root holding ``.sdd/``.
        now: Unix time ages are measured against. Defaults to the clock.

    Raises:
        SandboxSweepUnavailableError: The audit key, the audit log, or the
            task log cannot be read.
    """
    sdd_dir = workdir / ".sdd"
    audit_dir = sdd_dir / "audit"
    if not audit_dir.is_dir():
        return []
    sessions = open_sandbox_sessions(_read_sandbox_events(audit_dir))
    if not sessions:
        return []
    task_status = _load_task_statuses(sdd_dir)
    clock = time.time() if now is None else now
    candidates = [classify_open_session(s, task_status, now=clock) for s in sessions]
    candidates.sort(key=lambda c: (not c.is_leaked, -(c.age_seconds or 0.0), c.session.sandbox_session_id))
    return candidates
