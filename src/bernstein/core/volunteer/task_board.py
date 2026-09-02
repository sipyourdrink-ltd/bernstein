"""Tasks that originate at the hub itself, with no git forge behind them.

Everything a donor can lease today came from a git forge: a task id is whatever
string the caller of :meth:`LeaseStore.claim` supplies, and the runner's
:class:`~bernstein.core.volunteer.runner.ClaimedTask` is issue-shaped down to
the ``issue_number`` field.  A project that has no public issue tracker, or one
that does not want its volunteer queue to be its issue tracker, therefore has no
way to offer work at all.  This module is that way: an operator publishes a task
to the hub, the hub lists it, and a donor leases it -- the git forge never
appears.

Why a task id is a content digest under a reserved prefix
---------------------------------------------------------

A hub-native task id is ``hub:`` followed by the sha256 of the task's canonical
content.  Two consequences, both of which the alternative (a random id, or an
integer counter) does not have.

First, the two origins stay distinguishable forever.  Forge-sourced ids are
opaque strings chosen by whoever mirrors the forge; ``hub:`` is reserved, so a
mirrored id cannot collide with a board id and a donor holding a lease can tell
from the id alone which board, if any, is authoritative for it.  Handing out
leases under an ambiguous scheme is expensive to unwind afterwards, because the
donors already hold the ambiguous ids.

Second, the id is checkable.  A donor that ran a task can recompute the digest
from the content it was handed and confirm it matches the id it holds a lease
on; a hub that quietly rewrote a task's body after publishing it would have to
change the id, which it cannot do without invalidating the lease.  The identity
and the content are the same fact rather than two facts that have to agree.

Idempotence falls out of the same property: republishing byte-identical content
lands on the id that already exists, so a re-run of an operator's publish script
does not double the board.

Durability, and what it is modelled on
--------------------------------------

The board is an append-only JSONL log replayed at construction, the same shape
and for the same reason as
:class:`~bernstein.core.volunteer.lease_store.LeaseStore`: the hub must be able
to be torn down and brought back without losing what it offered.  Records are
serialised with sorted keys and no insignificant whitespace, so two boards
holding the same tasks write the same bytes.  A torn line at the tail of a
crashed process costs that one record, not the whole board.

Like the lease store, this is **single-process only** -- mutations take an
in-process :class:`asyncio.Lock` and the append path takes no OS-level file
lock.  The two are always constructed together and the hub's serve command
already refuses to start more than one worker process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.volunteer.runner import repo_url_problem

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Schema version stamped into every JSONL record, so a later reader can tell a
#: format change from a corrupt line.
TASK_BOARD_SCHEMA_VERSION: int = 1

#: Reserved prefix for hub-native task ids.  A task id that does not start with
#: this came from somewhere else and the board is not authoritative for it.
HUB_TASK_ID_PREFIX: str = "hub:"

#: Where the board lives when the operator does not say otherwise.  A sibling of
#: the lease log, because the two are one hub's state and are torn down together.
DEFAULT_TASK_BOARD_PATH: str = ".sdd/runtime/volunteer/tasks.jsonl"


class TaskBoardError(RuntimeError):
    """Raised when the board's log cannot be read."""


class TaskPublishError(ValueError):
    """Raised when a task cannot be published as offered.

    The message names the offending field and the reason, following
    :class:`~bernstein.core.volunteer.manifest.VolunteerManifestError`.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


def is_hub_native(task_id: str) -> bool:
    """Whether ``task_id`` belongs to the hub-native namespace."""
    return task_id.startswith(HUB_TASK_ID_PREFIX)


def hub_task_id(
    *,
    repo_url: str,
    title: str,
    body: str,
    task_size: str,
    ref: str | None,
) -> str:
    """The id a task with this content has.

    A pure function of the content, so it can be recomputed by anyone holding
    the task and compared against the id they were leased.  ``published_at`` is
    deliberately not an input: when it were, republishing the same offer would
    mint a second task.
    """
    payload = {
        "body": body,
        "ref": ref,
        "repo_url": repo_url,
        "task_size": task_size,
        "title": title,
    }
    digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return f"{HUB_TASK_ID_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class HubTask:
    """One offer on the hub's own board.

    Attributes:
        task_id: The content digest under :data:`HUB_TASK_ID_PREFIX`.
        repo_url: Where the work happens.  Checked against the same rules the
            runner applies before git ever sees a URL.
        title: Short description.  Untrusted text; it reaches an agent only
            through the runner's sanitiser, exactly as issue text does.
        body: Longer description.  Untrusted on the same terms.
        task_size: Canonical size label.  The board's copy is authoritative for
            donor admission; what a claimant asks with is not.
        ref: Branch or tag to work from, or ``None`` for the default.
        published_at: When the board first accepted this content.
    """

    task_id: str
    repo_url: str
    title: str
    body: str
    task_size: str
    ref: str | None
    published_at: float

    def to_dict(self) -> dict[str, Any]:
        """The stable wire shape, as the HTTP surface projects it."""
        return {
            "task_id": self.task_id,
            "repo_url": self.repo_url,
            "title": self.title,
            "body": self.body,
            "task_size": self.task_size,
            "ref": self.ref,
            "published_at": self.published_at,
        }


class TaskBoard:
    """The hub's own offers, held in memory and durable in a JSONL log."""

    def __init__(
        self,
        jsonl_path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Build a board over ``jsonl_path``, replaying it if it exists.

        Args:
            jsonl_path: The append-only log.  Created on first write.
            clock: Source of the current Unix timestamp.  Injected so tests are
                exact rather than timing-dependent.
        """
        self._jsonl_path = jsonl_path
        self._clock = clock
        self._lock = asyncio.Lock()
        self._tasks: dict[str, HubTask] = {}
        self._replay()

    # -- reads ------------------------------------------------------------

    def get(self, task_id: str) -> HubTask | None:
        """The task with ``task_id``, or ``None`` if the board has no such offer."""
        return self._tasks.get(task_id)

    def list_open(self) -> tuple[HubTask, ...]:
        """Every published task, in publish order."""
        return tuple(self._tasks.values())

    # -- mutations --------------------------------------------------------

    async def publish(
        self,
        *,
        repo_url: str,
        title: str,
        body: str,
        task_size: str = "s",
        ref: str | None = None,
    ) -> HubTask:
        """Put an offer on the board and return it.

        Idempotent by construction: the id is a digest of the content, so
        republishing the same offer returns the task already on the board,
        keeping its original ``published_at``.

        ``task_size`` is stored as given rather than validated here; the budget
        path in :class:`~bernstein.core.volunteer.lease_store.LeaseStore` is the
        one authority on which size labels exist, and duplicating its table here
        would give a publisher and a claimant two different answers.

        Args:
            repo_url: Where the work happens.
            title: Short description of the task.
            body: Longer description.
            task_size: Canonical size label used by donor admission.
            ref: Branch or tag to work from.

        Returns:
            The published :class:`HubTask`.

        Raises:
            TaskPublishError: When a field cannot be offered as given.
        """
        url = repo_url.strip()
        url_problem = repo_url_problem(url)
        if url_problem is not None:
            raise TaskPublishError("repo_url", url_problem)
        if not title.strip():
            raise TaskPublishError("title", "must not be empty")
        if not task_size.strip():
            raise TaskPublishError("task_size", "must not be empty")
        clean_ref = ref.strip() if ref is not None else None
        if clean_ref == "":
            clean_ref = None

        task_id = hub_task_id(
            repo_url=url,
            title=title,
            body=body,
            task_size=task_size,
            ref=clean_ref,
        )
        async with self._lock:
            existing = self._tasks.get(task_id)
            if existing is not None:
                return existing
            task = HubTask(
                task_id=task_id,
                repo_url=url,
                title=title,
                body=body,
                task_size=task_size,
                ref=clean_ref,
                published_at=self._clock(),
            )
            self._tasks[task_id] = task
            self._append({"kind": "task", **task.to_dict()})
            return task

    # -- durability -------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> None:
        """Append one record to the log, flushed immediately."""
        payload = {"schema_version": TASK_BOARD_SCHEMA_VERSION, **record}
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self._jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def _replay(self) -> None:
        """Rebuild the board from the log, in the order it was written.

        A malformed line is logged and skipped rather than fatal, matching
        :meth:`LeaseStore._replay`: one torn record at the tail of a crashed
        process should cost the last offer, not the whole board.
        """
        if not self._jsonl_path.exists():
            return
        try:
            lines = self._jsonl_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise TaskBoardError(f"cannot read task board log {self._jsonl_path}: {exc}") from exc
        for number, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                record: dict[str, Any] = json.loads(line)
            except ValueError:
                logger.error("corrupted task record at %s:%d - skipping", self._jsonl_path, number)
                continue
            if not isinstance(record, dict) or record.get("kind") != "task":
                continue
            task_id = record.get("task_id")
            if not isinstance(task_id, str):
                continue
            ref = record.get("ref")
            self._tasks[task_id] = HubTask(
                task_id=task_id,
                repo_url=str(record["repo_url"]),
                title=str(record["title"]),
                body=str(record["body"]),
                task_size=str(record["task_size"]),
                ref=str(ref) if isinstance(ref, str) else None,
                published_at=float(record["published_at"]),
            )


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serialise ``payload`` sorted and compact, as the lease log is written."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = [
    "DEFAULT_TASK_BOARD_PATH",
    "HUB_TASK_ID_PREFIX",
    "TASK_BOARD_SCHEMA_VERSION",
    "HubTask",
    "TaskBoard",
    "TaskBoardError",
    "TaskPublishError",
    "hub_task_id",
    "is_hub_native",
]
