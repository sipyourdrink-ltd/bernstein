"""Synthetic transaction: one throwaway entity, one ordered validator list.

``bernstein trackers test`` proves an adapter is *reachable* -- it
constructs the adapter and lists open tickets. That is the ceiling of
today's coverage, and it is not enough: an adapter can be up, authorised
and still wrong (a status id that maps to the wrong workflow state, a
comment that lands on the wrong field, a ``pull_open_tickets`` that
returns zero rows because a filter silently excludes everything).

This module drives the cheapest honest proof instead. It creates one
throwaway entity through the adapter itself, runs an ordered list of
validator callables against it, and lets the last validator delete it and
assert absence. Every entity carries a deterministic, date-derived marker
so a run that aborts halfway leaves a leftover the *next* run on the same
day recognises and sweeps, rather than a growing pile of orphans.

Shape
-----

* :func:`synthetic_marker` -- the deterministic id scheme.
* :class:`SyntheticProbeAdapter` -- the three optional operations an
  adapter must expose to host a throwaway entity. Adapters that do not
  expose them are refused with :class:`SyntheticProbeUnsupported`; the
  runner never grows a per-adapter branch.
* :data:`DEFAULT_VALIDATORS` -- the shared ordered list. Adding a check
  is appending one callable.
* :func:`run_synthetic_transaction` -- the runner. Returns a
  :class:`SyntheticTransactionReport` whose ``exit_code`` is non-zero if
  any validator failed, and which prints one verdict line per validator.

A validator is a plain callable taking a :class:`SyntheticContext`. It
signals failure by raising (any exception; the runner records the
exception class so a :class:`~bernstein.core.trackers.contract.RateLimited`
is distinguishable from a
:class:`~bernstein.core.trackers.contract.TrackerUnavailable` or a plain
assertion) and signals "not applicable to this adapter" by raising
:class:`ValidatorSkipped`. A skip is not a failure.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from bernstein.core.trackers.contract import TrackerError

if TYPE_CHECKING:
    from typing import TextIO

    from bernstein.core.trackers.contract import AbstractTrackerAdapter

__all__ = [
    "DEFAULT_VALIDATORS",
    "PROBE_OPERATIONS",
    "SYNTHETIC_MARKER_PREFIX",
    "SyntheticContext",
    "SyntheticProbeAdapter",
    "SyntheticProbeUnsupported",
    "SyntheticTransactionReport",
    "Validator",
    "ValidatorSkipped",
    "ValidatorVerdict",
    "add_comment_replay_with_the_same_key_is_accepted",
    "add_comment_returns_a_comment_id",
    "attach_blob_or_declares_unsupported",
    "claim_ticket_or_declares_unsupported",
    "delete_removes_the_synthetic_entity",
    "probe_body",
    "probe_title",
    "pull_open_tickets_finds_the_synthetic_entity",
    "run_synthetic_transaction",
    "synthetic_marker",
    "transition_reports_the_requested_status",
]


#: Prefix every synthetic entity's title carries. Operators grepping their
#: tracker for orphaned probe entities search for this.
SYNTHETIC_MARKER_PREFIX = "bernstein-synthetic"

#: The three optional adapter operations a synthetic transaction needs.
PROBE_OPERATIONS: tuple[str, ...] = (
    "create_probe_ticket",
    "find_probe_tickets",
    "delete_probe_ticket",
)

_OUTCOME_PREFIX = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}


# ---------------------------------------------------------------------------
# Deterministic identity
# ---------------------------------------------------------------------------


def synthetic_marker(tracker_name: str, *, day: dt.date | None = None) -> str:
    """Return the deterministic marker for ``tracker_name`` on ``day``.

    The marker is date-derived at day granularity, which is what makes a
    re-run collide with its own leftovers: two runs on the same UTC day
    compute the same marker, so the second finds and sweeps whatever the
    first left behind. Trackers assign their own entity ids, so the
    marker -- not the id -- is the thing the runner can predict.

    Args:
        tracker_name: The adapter's ``name`` attribute.
        day: UTC day to derive from; defaults to today in UTC.

    Returns:
        ``bernstein-synthetic-<tracker>-<YYYYMMDD>``.
    """
    stamp = (day or dt.datetime.now(dt.UTC).date()).strftime("%Y%m%d")
    return f"{SYNTHETIC_MARKER_PREFIX}-{tracker_name}-{stamp}"


def probe_title(marker: str) -> str:
    """Return the title a synthetic entity is created with."""
    return f"{marker} (bernstein adapter contract probe)"


def probe_body(marker: str) -> str:
    """Return the description a synthetic entity is created with."""
    return (
        "Throwaway entity created by the bernstein adapter contract test.\n\n"
        f"Marker: {marker}\n\n"
        "It is deleted by the last validator of the run that created it. "
        "If you are reading this, a run aborted midway; the next run on "
        "the same UTC day sweeps it, or you can delete it by hand."
    )


# ---------------------------------------------------------------------------
# Probe surface
# ---------------------------------------------------------------------------


class SyntheticProbeUnsupported(TrackerError):
    """Raised when an adapter cannot host a throwaway entity."""


class ValidatorSkipped(Exception):
    """Raised by a validator that does not apply to this adapter."""


@runtime_checkable
class SyntheticProbeAdapter(Protocol):
    """Optional adapter surface the synthetic transaction drives.

    Deliberately separate from
    :class:`~bernstein.core.trackers.contract.AbstractTrackerAdapter`:
    the hot path never creates or deletes tickets, and a general-purpose
    ``delete_ticket`` on the shared contract would be a destructive
    operation every caller could reach by accident. These three carry
    "probe" in their names and every implementation refuses an entity
    whose title does not carry the marker.
    """

    def create_probe_ticket(self, marker: str) -> str:
        """Create a throwaway entity titled with ``marker``; return its id."""
        ...

    def find_probe_tickets(self, marker: str) -> tuple[str, ...]:
        """Return the ids of every entity whose title carries ``marker``."""
        ...

    def delete_probe_ticket(self, ticket_id: str, marker: str) -> None:
        """Delete ``ticket_id``, refusing it if its title lacks ``marker``."""
        ...


@dataclass(frozen=True)
class SyntheticContext:
    """What a validator is handed.

    Attributes:
        adapter: The adapter under test, on its normal contract surface.
        probe: The same object, viewed through the probe surface.
        ticket_id: Tracker-assigned id of the entity created for this run.
        marker: The deterministic marker this run's entity carries.
    """

    adapter: AbstractTrackerAdapter
    probe: SyntheticProbeAdapter
    ticket_id: str
    marker: str


#: A validator is a plain callable. It raises to fail and raises
#: :class:`ValidatorSkipped` to opt out on adapters it does not apply to.
type Validator = Callable[[SyntheticContext], None]


# ---------------------------------------------------------------------------
# Verdicts and report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatorVerdict:
    """The outcome of one validator.

    Attributes:
        name: The validator callable's ``__name__``.
        outcome: ``"passed"``, ``"failed"`` or ``"skipped"``.
        detail: Human-readable reason; empty for a plain pass.
        error_kind: Exception class name for a failure -- ``RateLimited``,
            ``TrackerUnavailable``, ``OptimisticConcurrencyError`` and
            ``IdempotencyConflict`` are distinguishable here rather than
            flattened into one "it broke".
    """

    name: str
    outcome: str
    detail: str = ""
    error_kind: str | None = None


@dataclass(frozen=True)
class SyntheticTransactionReport:
    """The result of one synthetic transaction.

    Attributes:
        tracker: Adapter name.
        marker: Deterministic marker used for this run.
        ticket_id: Id of the entity created, or ``None`` if creation failed.
        leftovers_cleaned: Number of same-marker entities swept before the
            run created its own.
        verdicts: One verdict per validator, in the order they ran.
        lines: Exactly the lines the runner printed.
    """

    tracker: str
    marker: str
    ticket_id: str | None
    leftovers_cleaned: int
    verdicts: tuple[ValidatorVerdict, ...]
    lines: tuple[str, ...]

    @property
    def failures(self) -> tuple[ValidatorVerdict, ...]:
        """Verdicts that failed. A skip is not a failure."""
        return tuple(v for v in self.verdicts if v.outcome == "failed")

    @property
    def ok(self) -> bool:
        """True when no validator failed."""
        return not self.failures

    @property
    def exit_code(self) -> int:
        """``0`` when every validator passed or skipped, ``1`` otherwise."""
        return 0 if self.ok else 1

    def render_lines(self) -> list[str]:
        """Return the printed lines, for callers that captured no stream."""
        return list(self.lines)


# ---------------------------------------------------------------------------
# The shared validator list
# ---------------------------------------------------------------------------


def pull_open_tickets_finds_the_synthetic_entity(ctx: SyntheticContext) -> None:
    """The hot path returns the entity that was just created."""
    for ticket in ctx.adapter.pull_open_tickets({}):
        if ticket.id == ctx.ticket_id:
            return
    msg = f"pull_open_tickets did not return the entity {ctx.ticket_id} it should see as open"
    raise AssertionError(msg)


def add_comment_returns_a_comment_id(ctx: SyntheticContext) -> None:
    """A comment lands and the tracker hands back an id for it."""
    result = ctx.adapter.add_comment(
        ctx.ticket_id,
        f"{ctx.marker}: adapter contract probe comment.",
        idempotency_key=f"{ctx.marker}-comment",
    )
    if result.ticket_id != ctx.ticket_id:
        msg = f"add_comment reported ticket {result.ticket_id!r}, expected {ctx.ticket_id!r}"
        raise AssertionError(msg)
    if not result.comment_id:
        msg = "add_comment returned an empty comment_id"
        raise AssertionError(msg)


def add_comment_replay_with_the_same_key_is_accepted(ctx: SyntheticContext) -> None:
    """Replaying an identical comment does not blow up on the idempotency key.

    Trackers differ on whether they honour the key; what must never
    happen is the replay erroring out, because the orchestrator retries
    comments after a transport failure it cannot distinguish from a
    write that already landed.
    """
    result = ctx.adapter.add_comment(
        ctx.ticket_id,
        f"{ctx.marker}: adapter contract probe comment.",
        idempotency_key=f"{ctx.marker}-comment",
    )
    if not result.comment_id:
        msg = "replayed add_comment returned an empty comment_id"
        raise AssertionError(msg)


def transition_reports_the_requested_status(ctx: SyntheticContext) -> None:
    """A transition reports back the status it was asked for."""
    status_id = f"{ctx.marker}-done"
    result = ctx.adapter.transition(
        ctx.ticket_id,
        status_id,
        idempotency_key=f"{ctx.marker}-transition",
    )
    if result.ticket_id != ctx.ticket_id:
        msg = f"transition reported ticket {result.ticket_id!r}, expected {ctx.ticket_id!r}"
        raise AssertionError(msg)
    if result.new_status != status_id:
        msg = f"transition reported status {result.new_status!r}, expected {status_id!r}"
        raise AssertionError(msg)


def claim_ticket_or_declares_unsupported(ctx: SyntheticContext) -> None:
    """Claiming succeeds, or the adapter says it does not support claiming."""
    try:
        result = ctx.adapter.claim_ticket(ctx.ticket_id, f"{ctx.marker}-agent")
    except NotImplementedError as exc:
        raise ValidatorSkipped(str(exc) or "claim_ticket not supported") from exc
    if not result.claimed:
        msg = f"claim_ticket refused the freshly created entity {ctx.ticket_id}"
        raise AssertionError(msg)


def attach_blob_or_declares_unsupported(ctx: SyntheticContext) -> None:
    """Attaching succeeds, or the adapter says it does not support attaching."""
    try:
        result = ctx.adapter.attach_blob(
            ctx.ticket_id,
            ctx.marker.encode("utf-8"),
            "text/plain",
            idempotency_key=f"{ctx.marker}-attach",
        )
    except NotImplementedError as exc:
        raise ValidatorSkipped(str(exc) or "attach_blob not supported") from exc
    if not result.attachment_id:
        msg = "attach_blob returned an empty attachment_id"
        raise AssertionError(msg)


def delete_removes_the_synthetic_entity(ctx: SyntheticContext) -> None:
    """The entity is deleted and no longer findable. Always runs last."""
    ctx.probe.delete_probe_ticket(ctx.ticket_id, ctx.marker)
    remaining = ctx.probe.find_probe_tickets(ctx.marker)
    if remaining:
        msg = f"entities still carry marker {ctx.marker!r} after delete: {remaining}"
        raise AssertionError(msg)


#: The shared ordered list. Adding a check is appending one callable; the
#: deleting validator stays last.
DEFAULT_VALIDATORS: tuple[Validator, ...] = (
    pull_open_tickets_finds_the_synthetic_entity,
    add_comment_returns_a_comment_id,
    add_comment_replay_with_the_same_key_is_accepted,
    transition_reports_the_requested_status,
    claim_ticket_or_declares_unsupported,
    attach_blob_or_declares_unsupported,
    delete_removes_the_synthetic_entity,
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _resolve_probe(adapter: AbstractTrackerAdapter) -> SyntheticProbeAdapter:
    """Return ``adapter`` viewed as a probe host, or refuse it by name."""
    missing = [op for op in PROBE_OPERATIONS if not callable(getattr(adapter, op, None))]
    if missing:
        name = getattr(adapter, "name", type(adapter).__name__)
        msg = (
            f"Tracker {name!r} cannot host a synthetic transaction: "
            f"missing {', '.join(missing)}. Implement the probe surface on "
            "the adapter to bring it under the contract test."
        )
        raise SyntheticProbeUnsupported(msg)
    return cast("SyntheticProbeAdapter", adapter)


def _entities(count: int) -> str:
    """Pluralise an entity count for the printed sweep lines."""
    return f"{count} entity" if count == 1 else f"{count} entities"


def _verdict_line(verdict: ValidatorVerdict) -> str:
    prefix = _OUTCOME_PREFIX.get(verdict.outcome, "????")
    suffix = f"  {verdict.detail}" if verdict.detail else ""
    return f"{prefix} {verdict.name}{suffix}"


def run_synthetic_transaction(
    adapter: AbstractTrackerAdapter,
    *,
    validators: Sequence[Validator] = DEFAULT_VALIDATORS,
    day: dt.date | None = None,
    stream: TextIO | None = None,
) -> SyntheticTransactionReport:
    """Drive one throwaway entity through ``adapter`` and report per validator.

    Sweeps same-day leftovers, creates the entity, runs every validator in
    order (a failure does not abort the run, so the deleting validator
    still gets to clean up), then sweeps anything that still carries the
    marker so nothing leaks either way.

    Args:
        adapter: The adapter under test. Must expose
            :data:`PROBE_OPERATIONS`.
        validators: Ordered validator callables; defaults to
            :data:`DEFAULT_VALIDATORS`.
        day: UTC day the marker derives from; defaults to today.
        stream: Where verdict lines are written; defaults to stdout.

    Returns:
        A :class:`SyntheticTransactionReport`. Callers that need a process
        exit status use ``report.exit_code``.

    Raises:
        SyntheticProbeUnsupported: When ``adapter`` cannot host a
            throwaway entity.
    """
    probe = _resolve_probe(adapter)
    out = sys.stdout if stream is None else stream
    tracker = getattr(adapter, "name", type(adapter).__name__)
    marker = synthetic_marker(tracker, day=day)

    lines: list[str] = []

    def emit(line: str) -> None:
        lines.append(line)
        print(line, file=out)

    emit(f"probe {tracker} marker={marker}")

    leftovers = probe.find_probe_tickets(marker)
    for stale in leftovers:
        probe.delete_probe_ticket(stale, marker)
    if leftovers:
        emit(f"swept {_entities(len(leftovers))} left by an earlier run")

    ticket_id = probe.create_probe_ticket(marker)
    ctx = SyntheticContext(adapter=adapter, probe=probe, ticket_id=ticket_id, marker=marker)

    verdicts: list[ValidatorVerdict] = []
    for validator in validators:
        name = getattr(validator, "__name__", repr(validator))
        try:
            validator(ctx)
        except ValidatorSkipped as exc:
            verdict = ValidatorVerdict(name=name, outcome="skipped", detail=str(exc))
        except Exception as exc:  # the runner reports failures, it does not classify them
            verdict = ValidatorVerdict(
                name=name,
                outcome="failed",
                detail=str(exc),
                error_kind=type(exc).__name__,
            )
        else:
            verdict = ValidatorVerdict(name=name, outcome="passed")
        verdicts.append(verdict)
        emit(_verdict_line(verdict))

    stragglers = probe.find_probe_tickets(marker)
    for stale in stragglers:
        probe.delete_probe_ticket(stale, marker)
    if stragglers:
        emit(f"warn: swept {_entities(len(stragglers))} no validator deleted")

    failed = sum(1 for v in verdicts if v.outcome == "failed")
    skipped = sum(1 for v in verdicts if v.outcome == "skipped")
    passed = len(verdicts) - failed - skipped
    outcome = "ok" if failed == 0 else "failed"
    emit(f"result {outcome}: {passed} passed, {skipped} skipped, {failed} failed")

    return SyntheticTransactionReport(
        tracker=tracker,
        marker=marker,
        ticket_id=ticket_id,
        leftovers_cleaned=len(leftovers),
        verdicts=tuple(verdicts),
        lines=tuple(lines),
    )
