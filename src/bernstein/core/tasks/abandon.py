"""Agent-initiated abandon primitive with reason ledger (#1350).

When an agent realises mid-task that the spec is wrong, the test
environment is broken, the cost-per-line has crossed the budget ceiling,
or the work is otherwise dishonest to finish, the only clean exit is to
abandon. This module defines:

* :class:`AbandonReason` - closed taxonomy of structured exit reasons.
* :class:`Abandonment` - append-only ledger row.
* :class:`AbandonmentLedger` - persistent JSONL ledger at
  ``.sdd/runtime/abandonments.jsonl`` with atomic appends and rate
  aggregations by role / adapter.

Design notes
------------

* The ledger is append-only on the happy path; no in-place rewrites. Each
  row carries an opaque ``id`` (UUID4 hex) so concurrent appends from
  different writers never collide.
* :meth:`AbandonmentLedger.append` takes a single ``open()`` in ``"a"``
  mode per call; on POSIX, line-sized writes to an ``O_APPEND`` file are
  atomic up to PIPE_BUF, which is comfortably above any plausible row
  size here. This is the same contract the dead-letter-queue and signal
  ledgers rely on.
* :func:`Abandonment.to_dict` returns a JSON-safe dict; the inverse
  :meth:`Abandonment.from_dict` accepts malformed rows and either
  populates safe defaults or - for unrecognised reasons - raises so the
  caller can decide whether to skip or surface the error.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "abandonments.jsonl"


class AbandonReason(Enum):
    """Closed taxonomy of structured abandonment reasons.

    The vocabulary is intentionally small so dashboards can aggregate on
    it without operator-supplied free-form noise. ``OTHER`` is the
    escape hatch for cases that genuinely do not fit; agents are
    nudged in the prompt preamble to prefer a precise reason.
    """

    # Spec/intent issues
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    CONFLICTING_INSTRUCTIONS = "conflicting_instructions"
    SPEC_UNDERDETERMINED = "spec_underdetermined"
    # Environment / capability issues
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"
    BUDGET_EXCEEDED = "budget_exceeded"
    CAPABILITY_MISMATCH = "capability_mismatch"
    ENV_BROKEN = "env_broken"
    # Coordination
    BLOCKED_BY_EXTERNAL = "blocked_by_external"
    UNSAFE_CHANGE = "unsafe_change"
    OPERATOR_OVERRIDE = "operator_override"
    # Catch-all
    OTHER = "other"

    @classmethod
    def coerce(cls, value: str | AbandonReason) -> AbandonReason:
        """Best-effort coercion of a string/enum to :class:`AbandonReason`.

        Accepts the enum value (``"out_of_scope"``) or the enum name
        (``"OUT_OF_SCOPE"``). Raises :class:`ValueError` on unknown
        inputs so callers cannot silently launder a typo into ``OTHER``.
        """
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError(f"AbandonReason must be str or AbandonReason, got {type(value).__name__}")
        text = value.strip()
        if not text:
            raise ValueError("AbandonReason cannot be empty")
        # Try value match first
        for member in cls:
            if member.value == text:
                return member
        # Then name match (case-insensitive)
        upper = text.upper()
        for member in cls:
            if member.name == upper:
                return member
        raise ValueError(f"Unknown AbandonReason: {value!r}")


@dataclass(frozen=True)
class Abandonment:
    """A single append-only abandonment ledger row.

    Fields are deliberately scalar/JSON-friendly so the ledger format
    survives forward-compatible additions (unknown keys are tolerated by
    :meth:`from_dict`).

    Attributes:
        id: Stable per-row identifier (UUID4 hex slice).
        task_id: Originating task ID.
        reason: Structured :class:`AbandonReason`.
        detail: Free-form human-readable rationale.
        role: Task role at time of abandonment.
        agent_id: Adapter session identifier, if known.
        adapter: Adapter/CLI label (e.g. ``"claude"``, ``"codex"``).
        cost_to_date_usd: Cost accumulated on the task before abandon.
        attempts: How many retry attempts had happened before this row.
        timestamp: Unix epoch seconds at write time.
    """

    id: str
    task_id: str
    reason: AbandonReason
    detail: str = ""
    role: str = ""
    agent_id: str = ""
    adapter: str = ""
    cost_to_date_usd: float = 0.0
    attempts: int = 0
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Validate invariants at construction time."""
        if not self.id:
            raise ValueError("Abandonment.id must be non-empty")
        if not self.task_id:
            raise ValueError("Abandonment.task_id must be non-empty")
        if not isinstance(self.reason, AbandonReason):  # type: ignore[reportUnnecessaryIsInstance]
            raise ValueError("Abandonment.reason must be an AbandonReason member")
        if self.attempts < 0:
            raise ValueError(f"Abandonment.attempts must be >= 0, got {self.attempts}")
        if self.cost_to_date_usd < 0:
            raise ValueError(f"Abandonment.cost_to_date_usd must be >= 0, got {self.cost_to_date_usd}")
        if self.timestamp < 0:
            raise ValueError(f"Abandonment.timestamp must be >= 0, got {self.timestamp}")

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict for ledger writes."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "reason": self.reason.value,
            "detail": self.detail,
            "role": self.role,
            "agent_id": self.agent_id,
            "adapter": self.adapter,
            "cost_to_date_usd": self.cost_to_date_usd,
            "attempts": self.attempts,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Abandonment:
        """Deserialise a ledger row.

        Raises:
            ValueError: When required fields are missing or ``reason`` is
                not a known :class:`AbandonReason` value.
        """
        if "task_id" not in data:
            raise ValueError("Abandonment.from_dict missing task_id")
        if "reason" not in data:
            raise ValueError("Abandonment.from_dict missing reason")
        reason = AbandonReason.coerce(str(data["reason"]))
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:16]),
            task_id=str(data["task_id"]),
            reason=reason,
            detail=str(data.get("detail", "")),
            role=str(data.get("role", "")),
            agent_id=str(data.get("agent_id", "")),
            adapter=str(data.get("adapter", "")),
            cost_to_date_usd=float(data.get("cost_to_date_usd", 0.0)),
            attempts=int(data.get("attempts", 0)),
            timestamp=float(data.get("timestamp", 0.0)),
        )


def new_abandonment(
    *,
    task_id: str,
    reason: AbandonReason | str,
    detail: str = "",
    role: str = "",
    agent_id: str = "",
    adapter: str = "",
    cost_to_date_usd: float = 0.0,
    attempts: int = 0,
    timestamp: float | None = None,
) -> Abandonment:
    """Build an :class:`Abandonment` with a fresh ID and timestamp.

    Centralises construction so callers do not generate IDs in two
    places and so ``timestamp=None`` reliably resolves to ``time.time()``.
    """
    coerced = AbandonReason.coerce(reason)
    return Abandonment(
        id=uuid.uuid4().hex[:16],
        task_id=task_id,
        reason=coerced,
        detail=detail,
        role=role,
        agent_id=agent_id,
        adapter=adapter,
        cost_to_date_usd=cost_to_date_usd,
        attempts=attempts,
        timestamp=timestamp if timestamp is not None else time.time(),
    )


class AbandonmentLedger:
    """Append-only JSONL ledger of agent abandonments.

    The ledger never rewrites existing rows. Aggregations (:meth:`stats`,
    :meth:`abandon_rate_by_role`, :meth:`abandon_rate_by_adapter`) read
    the live file on every call so concurrent writers in the same
    process see each other's rows.

    Args:
        sdd_dir: Path to the project ``.sdd`` state directory. The
            ledger lives at ``<sdd_dir>/runtime/abandonments.jsonl``.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._path: Path = sdd_dir / "runtime" / LEDGER_FILENAME

    @property
    def path(self) -> Path:
        """Filesystem path to the underlying JSONL file."""
        return self._path

    def append(self, row: Abandonment) -> None:
        """Append a single row to the ledger.

        Creates parent directories on demand. Uses a single
        ``open(..., "a")`` so the line write is atomic on POSIX up to
        PIPE_BUF (rows are far smaller than that ceiling).

        Raises:
            OSError: Propagated when the underlying write fails - the
                caller decides whether to retry or surface the error.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row.to_dict(), sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()

    def read_all(self) -> list[Abandonment]:
        """Replay every well-formed row from disk.

        Malformed JSON lines and rows with unknown reasons are skipped
        (and logged at debug) so a single bad row never wedges the
        operator CLI. Returns the rows in append order.
        """
        if not self._path.exists():
            return []
        rows: list[Abandonment] = []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read abandonment ledger: %s", exc)
            return []
        # Split on LF only. `splitlines()` also splits on U+2028 and U+2029, which `json.dumps(...,
        # ensure_ascii=False)` writes inside a string, so one row became two invalid halves (#7960).
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed: object = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.debug("Skipping malformed ledger line: %s", exc)
                continue
            if not isinstance(parsed, dict):
                logger.debug("Skipping non-object ledger row: %r", parsed)
                continue
            data_any: dict[str, Any] = dict(parsed)  # type: ignore[arg-type]
            try:
                rows.append(Abandonment.from_dict(data_any))
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping invalid ledger row: %s", exc)
        return rows

    def list_recent(self, limit: int = 20) -> list[Abandonment]:
        """Return the most recent ``limit`` rows, newest first."""
        if limit <= 0:
            return []
        rows = self.read_all()
        rows.sort(key=lambda row: row.timestamp, reverse=True)
        return rows[:limit]

    def count_by_task(self, task_id: str) -> int:
        """Count how many times *task_id* has been abandoned in the ledger."""
        return sum(1 for row in self.read_all() if row.task_id == task_id)

    def by_reason(self, task_id: str) -> Counter[AbandonReason]:
        """Return reason histogram for a single task."""
        counts: Counter[AbandonReason] = Counter()
        for row in self.read_all():
            if row.task_id == task_id:
                counts[row.reason] += 1
        return counts

    def abandon_rate_by_role(self, completed_by_role: dict[str, int]) -> dict[str, float]:
        """Compute per-role abandonment rate.

        Args:
            completed_by_role: Map of role → completed (DONE/CLOSED)
                task count, used as the denominator. Roles absent from
                this map but present in the ledger are reported with a
                rate of ``1.0`` (no completions to compare against).

        Returns:
            Map of role → ``abandons / (abandons + completed)`` in
            ``[0.0, 1.0]``.
        """
        abandons: Counter[str] = Counter()
        for row in self.read_all():
            if row.role:
                abandons[row.role] += 1
        roles = set(abandons) | set(completed_by_role)
        rate: dict[str, float] = {}
        for role in roles:
            a = abandons.get(role, 0)
            c = completed_by_role.get(role, 0)
            denom = a + c
            rate[role] = (a / denom) if denom > 0 else 0.0
        return rate

    def abandon_rate_by_adapter(self, completed_by_adapter: dict[str, int]) -> dict[str, float]:
        """Compute per-adapter abandonment rate.

        Mirrors :meth:`abandon_rate_by_role` but keys on the adapter
        label recorded in the ledger row.
        """
        abandons: Counter[str] = Counter()
        for row in self.read_all():
            if row.adapter:
                abandons[row.adapter] += 1
        adapters = set(abandons) | set(completed_by_adapter)
        rate: dict[str, float] = {}
        for adapter in adapters:
            a = abandons.get(adapter, 0)
            c = completed_by_adapter.get(adapter, 0)
            denom = a + c
            rate[adapter] = (a / denom) if denom > 0 else 0.0
        return rate

    def stats(self) -> dict[str, Any]:
        """Return aggregate stats over the entire ledger.

        Keys:
            * ``total`` - total row count.
            * ``by_reason`` - reason value → row count.
            * ``by_role`` - role → row count.
            * ``by_adapter`` - adapter → row count.
        """
        by_reason: Counter[str] = Counter()
        by_role: Counter[str] = Counter()
        by_adapter: Counter[str] = Counter()
        rows = self.read_all()
        for row in rows:
            by_reason[row.reason.value] += 1
            if row.role:
                by_role[row.role] += 1
            if row.adapter:
                by_adapter[row.adapter] += 1
        return {
            "total": len(rows),
            "by_reason": dict(by_reason),
            "by_role": dict(by_role),
            "by_adapter": dict(by_adapter),
        }
