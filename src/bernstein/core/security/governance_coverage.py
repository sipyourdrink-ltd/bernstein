"""Governance coverage: the fraction of a run's actions the chain can account for.

Issue #5067. The operator screens report entries -- an audit log, an approval
queue -- and nothing reports *coverage*. An operator cannot see that a run
produced actions no record ties to a principal, because a screen built out of
entries only ever shows the entries that exist.

This module is the projection behind that number. It answers two questions
about one run, from recorded evidence only:

``attributable_action_ratio``
    Of the actions the run recorded, how many were performed by an actor the
    run also made a governance verdict about. A lineage-spine entry's ``actor``
    is a free-form string the writing adapter supplied; nothing binds it to an
    identity. A :class:`~bernstein.core.security.governance.GovernanceDecision`
    naming that string as its ``subject`` is the only place in the run where
    the installation resolved it to a principal and ruled on it. An action
    whose actor never appears there is recorded but not attributed.

``decision_coverage``
    Of the same actions, how many were performed by a principal holding a
    recorded ``allow`` verdict in this run. A ``deny`` or ``refuse`` attributes
    the actor without authorising it: an action taken in the face of a recorded
    denial is precisely an uncovered one, so it counts in the denominator and
    not in the numerator.

Three rules keep the number honest rather than flattering:

* **The governance record is not an agent action.** Decision records are
  anchored into the same spine, so counting spine rows naively would let a run
  raise its own coverage by recording more decisions. Rows written by
  :data:`~bernstein.core.security.governance.GOVERNANCE_ACTOR` are excluded.
* **Chain bookkeeping is not an agent action.** The journal-head seal and
  artifact-attempt rows record the run's own plumbing and a non-delivery
  respectively; both are excluded, the same way every other consumer of the
  spine excludes them.
* **No actions means no ratio.** A run that recorded nothing reports ``None``,
  not ``0`` and not ``1``. Zero over zero is absent evidence, and rendering it
  as a number is how a console comes to claim what it cannot show.

``chain_status`` travels with the numbers because a ratio computed over a
mutated chain is not a measurement. It carries the
:class:`~bernstein.core.lineage.spine.SpineStatus` value verbatim, so a
``tampered`` run cannot be read as a clean one that merely scored badly.

Determinism: the document is canonical JSON (sorted keys, minimal separators)
and every field is a pure function of the files under ``.sdd``, so the
dashboard route and an offline recomputation produce byte-identical bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import (
    ARTIFACT_ATTEMPT_STEP_PREFIX,
    JOURNAL_SEAL_STEP_PREFIX,
    LineageSpine,
)
from bernstein.core.security.governance import GOVERNANCE_ACTOR, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.lineage.spine import SpineEntry

#: Version stamped into the coverage document. Bump only on a shape change.
COVERAGE_DOCUMENT_VERSION = 1

#: Verdict that authorises. Any other recorded verdict attributes the actor
#: without covering the action.
_ALLOW = "allow"

#: Decimal places the ratios are rounded to. Fixed so two surfaces computing
#: the same fraction serialise the same digits.
_RATIO_PRECISION = 6


@dataclass(frozen=True, slots=True)
class CoverageMetric:
    """One coverage bar: a count, its denominator, and their ratio."""

    covered: int
    total: int

    @property
    def ratio(self) -> float | None:
        """The fraction covered, or ``None`` when nothing was recorded."""
        if self.total == 0:
            return None
        return round(self.covered / self.total, _RATIO_PRECISION)

    def to_dict(self) -> dict[str, Any]:
        return {"covered": self.covered, "total": self.total, "ratio": self.ratio}


@dataclass(frozen=True, slots=True)
class GovernanceCoverage:
    """What one run's recorded evidence can account for."""

    run_id: str
    chain_status: str
    recorded_actions: int
    recorded_decisions: int
    attributable_actions: CoverageMetric
    decided_actions: CoverageMetric

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": COVERAGE_DOCUMENT_VERSION,
            "run_id": self.run_id,
            "chain_status": self.chain_status,
            "recorded_actions": self.recorded_actions,
            "recorded_decisions": self.recorded_decisions,
            "metrics": {
                "attributable_action_ratio": self.attributable_actions.to_dict(),
                "decision_coverage": self.decided_actions.to_dict(),
            },
        }


def _is_agent_action(entry: SpineEntry) -> bool:
    """Whether a spine entry records an action an agent took.

    False for the run's own bookkeeping: the journal-head seal, artifact
    attempts (which record a non-delivery), and the governance decision records
    anchored into the same chain.
    """
    if entry.actor == GOVERNANCE_ACTOR:
        return False
    return not entry.step_id.startswith((JOURNAL_SEAL_STEP_PREFIX, ARTIFACT_ATTEMPT_STEP_PREFIX))


def collect_governance_coverage(workdir: Path, run_id: str, *, hmac_key: bytes) -> GovernanceCoverage:
    """Project the coverage of *run_id* from ``<workdir>/.sdd``.

    Args:
        workdir: Project root containing ``.sdd``.
        run_id: The run to report on.
        hmac_key: Audit-chain HMAC key the spine entries are tagged with;
            used to verify the chain the numbers are read from.

    Returns:
        The :class:`GovernanceCoverage` for the run. A run with no spine is not
        an error: it reports ``no_entries`` and absent ratios.

    Raises:
        SpineRunIdError: When *run_id* would escape its per-run directory.
    """
    lineage_root = workdir / ".sdd" / "lineage"
    spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)

    actions = [entry for entry in spine.iter_entries() if _is_agent_action(entry)]
    decisions = read_decisions(lineage_root, run_id)

    subjects = {decision.subject for decision in decisions}
    allowed = {decision.subject for decision in decisions if decision.verdict == _ALLOW}

    total = len(actions)
    return GovernanceCoverage(
        run_id=run_id,
        chain_status=spine.verify().status.value,
        recorded_actions=total,
        recorded_decisions=len(decisions),
        attributable_actions=CoverageMetric(
            covered=sum(1 for entry in actions if entry.actor in subjects),
            total=total,
        ),
        decided_actions=CoverageMetric(
            covered=sum(1 for entry in actions if entry.actor in allowed),
            total=total,
        ),
    )


def governance_coverage_json(workdir: Path, run_id: str, *, hmac_key: bytes) -> str:
    """Return the canonical coverage document for *run_id*.

    **The** entry point: the dashboard route returns exactly these bytes, so
    the screen and an offline recomputation from ``.sdd`` cannot differ.
    """
    payload = collect_governance_coverage(workdir, run_id, hmac_key=hmac_key).to_dict()
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = [
    "COVERAGE_DOCUMENT_VERSION",
    "CoverageMetric",
    "GovernanceCoverage",
    "collect_governance_coverage",
    "governance_coverage_json",
]
