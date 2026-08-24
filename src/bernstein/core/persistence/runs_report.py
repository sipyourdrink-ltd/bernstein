"""Finished-run classification, projected from the work ledger (#4465).

After a batch of unattended runs the first operator question is always the
same: which runs opened a PR, which failed the gate, which produced no
changes, which died on infrastructure. Every fact already lives in the work
ledger (:mod:`bernstein.core.persistence.work_ledger`) -- this module is the
read-only classifier over it. ``bernstein runs report`` is a thin renderer
on top; the classification rule lives here, next to the ledger it reads, so
there is exactly one place that can answer "why did this run end that way".

The wrap-up contract
---------------------
A run's terminal facts -- the branch it published, the gate that blocked it,
the commit count against base, the infra error that killed it -- travel as
the payload of its last ``run.closed`` entry (see
:data:`bernstein.core.persistence.work_ledger.KIND_RUN_CLOSED`). That
payload is the "wrap-up" this module's docstrings and tests refer to:
:class:`RunWrapUp` is its typed, round-trippable shape. A run that never
appended a ``run.closed`` entry -- killed mid-flight, before it could record
anything -- has no wrap-up at all, and :func:`classify_run` maps that
absence to :attr:`RunOutcome.INFRA_ERROR` rather than raising.

Outcome classes (stable values -- the ``--json`` contract)
------------------------------------------------------------
``pr-opened``    branch published (a PR was opened)
``gate-failed``  a quality gate blocked the run
``no-changes``   zero commits over base
``infra-error``  adapter/transport death, or no wrap-up was ever recorded
``wedged``       the run ended with unspawnable open tasks

Scope note: this module reports what a run's ledger holds; it does not poll
process liveness. It is meant to run after a batch, so a run still actually
in flight when invoked simply shows whatever its ledger holds so far -- the
same "no run.closed entry yet" shape as a kill, which is the honest answer
absent a heartbeat check.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    LedgerError,
    LedgerReader,
    LedgerState,
    default_ledger_root,
    replay_state,
    run_ledger_dir,
)
from bernstein.core.security.path_containment import PathContainmentError

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.persistence.work_ledger import LedgerEntry

#: Error-kind values on a wrap-up that indicate an infrastructure death
#: rather than a task or gate outcome.
_INFRA_ERROR_KINDS = frozenset({"adapter", "transport"})

_NO_WRAPUP_EVIDENCE = "no wrap-up recorded before the run ended (likely killed mid-flight)"


class RunOutcome(StrEnum):
    """The five outcome classes ``bernstein runs report`` distinguishes."""

    PR_OPENED = "pr-opened"
    GATE_FAILED = "gate-failed"
    NO_CHANGES = "no-changes"
    INFRA_ERROR = "infra-error"
    WEDGED = "wedged"


@dataclass(frozen=True)
class RunWrapUp:
    """Terminal facts a run records on its closing (``run.closed``) entry.

    Every field is optional: different endings populate different subsets,
    and an all-defaults instance is a valid (if uninformative) wrap-up. The
    payload is redacted and hashed like any other ledger entry payload, so
    these fields must stay JSON-primitive (str, int, or None).

    Attributes:
        branch: Branch name published for this run, if any.
        pr_number: Pull request number opened for this run, if any.
        gate_name: Name of the quality gate that blocked the run, if any.
        failing_check: First failing check under ``gate_name``, if any.
        commits_over_base: Commit count against the base branch. ``0`` means
            the run produced no changes; ``None`` means not recorded.
        error_kind: ``"adapter"`` or ``"transport"`` when the run ended on
            an infrastructure death the supervisor detected; ``""`` else.
        error_message: Human-readable detail for ``error_kind``.
    """

    branch: str = ""
    pr_number: int | None = None
    gate_name: str = ""
    failing_check: str = ""
    commits_over_base: int | None = None
    error_kind: str = ""
    error_message: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Return the dict this wrap-up would be stored as, unset fields omitted."""
        payload: dict[str, Any] = {}
        if self.branch:
            payload["branch"] = self.branch
        if self.pr_number is not None:
            payload["pr_number"] = self.pr_number
        if self.gate_name:
            payload["gate_name"] = self.gate_name
        if self.failing_check:
            payload["failing_check"] = self.failing_check
        if self.commits_over_base is not None:
            payload["commits_over_base"] = self.commits_over_base
        if self.error_kind:
            payload["error_kind"] = self.error_kind
        if self.error_message:
            payload["error_message"] = self.error_message
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RunWrapUp:
        """Build a wrap-up from a ``run.closed`` entry's payload dict.

        Malformed or missing fields fall back to their defaults rather than
        raising -- a wrap-up is best-effort evidence, not a second ledger
        that can fail closed.
        """
        pr_number = payload.get("pr_number")
        commits = payload.get("commits_over_base")
        return cls(
            branch=str(payload.get("branch", "")),
            pr_number=pr_number if isinstance(pr_number, int) and not isinstance(pr_number, bool) else None,
            gate_name=str(payload.get("gate_name", "")),
            failing_check=str(payload.get("failing_check", "")),
            commits_over_base=commits if isinstance(commits, int) and not isinstance(commits, bool) else None,
            error_kind=str(payload.get("error_kind", "")),
            error_message=str(payload.get("error_message", "")),
        )


@dataclass(frozen=True)
class FinishedRun:
    """One row of ``bernstein runs report``: a classified finished run.

    Field names are the stable ``--json`` contract; a scheduler reacting to
    these rows programmatically should key off them, not off ``evidence``
    text.
    """

    run_id: str
    branch: str
    outcome: RunOutcome
    evidence: str
    started_at: float

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON row shape (``outcome`` as its string value)."""
        return {
            "run_id": self.run_id,
            "branch": self.branch,
            "outcome": self.outcome.value,
            "evidence": self.evidence,
            "started_at": self.started_at,
        }


def classify_run(state: LedgerState, wrapup: RunWrapUp | None) -> tuple[RunOutcome, str]:
    """Classify one run from its ledger projection and its wrap-up.

    Precedence, first match wins -- this order is the classifier's public
    contract and each branch is covered by a dedicated fixture test:

    1. ``wrapup.error_kind`` is ``"adapter"`` or ``"transport"`` -- the
       supervisor itself detected an infrastructure death.
    2. *wrapup* is ``None`` -- no ``run.closed`` entry exists at all, the
       run was killed mid-flight and never got to record anything.
    3. ``wrapup.gate_name`` is set -- a quality gate blocked the run.
    4. the run closed with tasks still scheduled or in flight -- wedged on
       unspawnable open tasks.
    5. ``wrapup.branch`` or ``wrapup.pr_number`` is set -- the success path.
    6. otherwise -- no changes over base.

    Returns:
        ``(outcome, evidence)``: the class, and a one-line human string.
    """
    if wrapup is not None and wrapup.error_kind in _INFRA_ERROR_KINDS:
        message = wrapup.error_message or "no further detail recorded"
        return RunOutcome.INFRA_ERROR, f"{wrapup.error_kind}: {message}"

    if wrapup is None:
        return RunOutcome.INFRA_ERROR, _NO_WRAPUP_EVIDENCE

    if wrapup.gate_name:
        check = wrapup.failing_check or "(failing check not recorded)"
        return RunOutcome.GATE_FAILED, f"{wrapup.gate_name}: {check}"

    open_tasks = state.resume_frontier()
    if open_tasks:
        return RunOutcome.WEDGED, f"{len(open_tasks)} unspawnable task(s): {', '.join(open_tasks)}"

    if wrapup.branch or wrapup.pr_number is not None:
        evidence = wrapup.branch or "(branch not recorded)"
        if wrapup.pr_number is not None:
            evidence = f"{evidence} (PR #{wrapup.pr_number})"
        return RunOutcome.PR_OPENED, evidence

    commits = wrapup.commits_over_base or 0
    return RunOutcome.NO_CHANGES, f"{commits} commits over base"


def _last_wrapup(entries: list[LedgerEntry]) -> RunWrapUp | None:
    """Return the wrap-up on the last ``run.closed`` entry, or ``None``."""
    for entry in reversed(entries):
        if entry.kind == KIND_RUN_CLOSED:
            return RunWrapUp.from_payload(entry.payload)
    return None


def classify_ledger_dir(ledger_dir: Path, *, run_id: str) -> FinishedRun | None:
    """Classify one on-disk ledger directory.

    Returns:
        A :class:`FinishedRun`, or ``None`` when the directory holds no
        ledger entries at all (nothing to report).
    """
    reader = LedgerReader(ledger_dir)
    if not reader.exists():
        return None
    entries = list(reader.entries())
    if not entries:
        return None
    state = replay_state(entries, run_id=run_id)
    wrapup = _last_wrapup(entries)
    outcome, evidence = classify_run(state, wrapup)
    return FinishedRun(
        run_id=state.run_id or run_id,
        branch=wrapup.branch if wrapup is not None else "",
        outcome=outcome,
        evidence=evidence,
        started_at=entries[0].ts,
    )


def list_finished_runs(sdd_dir: Path, *, since: float | None = None) -> list[FinishedRun]:
    """Classify every run under the ledger root, newest-started first.

    Args:
        sdd_dir: The install's ``.sdd`` directory.
        since: When given, a unix timestamp; runs that started earlier are
            omitted.

    Returns:
        One :class:`FinishedRun` per run directory with ledger entries.
        A run directory that fails to classify (a malformed id, an
        unreadable file) is skipped rather than raised -- one bad run
        cannot take the whole report down, the same contract a run with no
        wrap-up gets from :func:`classify_run`.
    """
    root = default_ledger_root(sdd_dir)
    runs: list[FinishedRun] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            ledger_dir = run_ledger_dir(sdd_dir, child.name)
            run = classify_ledger_dir(ledger_dir, run_id=child.name)
        except (LedgerError, PathContainmentError, OSError):
            continue
        if run is None:
            continue
        if since is not None and run.started_at < since:
            continue
        runs.append(run)
    runs.sort(key=lambda run: (-run.started_at, run.run_id))
    return runs


__all__ = [
    "FinishedRun",
    "RunOutcome",
    "RunWrapUp",
    "classify_ledger_dir",
    "classify_run",
    "list_finished_runs",
]
