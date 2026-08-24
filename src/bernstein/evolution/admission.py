"""Empirical-confidence admission gate for upgrade proposals.

``empirical_confidence`` records outcomes but nothing consults it before an
upgrade is applied, so a producer with a bad history is admitted exactly like
one with a good one. This is the single admission point both executor families
consult, rather than a gate copied into each.

Three decisions are load-bearing and were agreed on the issue before this was
written:

* **The signal is the proposer, not the executor.** ``UpgradeProposal.to_task``
  hard-codes ``role="manager"`` for routing, so gating on the task role would
  measure whoever applies the change rather than whoever proposed it. The gate
  reads producer identity from the proposal.
* **The decision key is ``category:<category>|trigger:<triggered_by>``.**
  Proposal-ID granularity never accumulates the five samples
  ``empirical_confidence`` requires; agent type alone is too coarse to separate
  a producer that is reliable for dependency bumps and unreliable for
  refactors.
* **Cold start is explicit and fail-closed by default.** A key below the sample
  threshold has no measured accuracy, and treating "unmeasured" as "fine" is
  the failure this gate exists to prevent. Opening cold start is possible but
  has to be configured deliberately; there is no default-open fallthrough.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bernstein.core.quality.empirical_confidence import (
    Confidence,
    ConfidenceQuery,
)

if TYPE_CHECKING:
    from bernstein.evolution.proposals import UpgradeProposal

logger = logging.getLogger(__name__)

_ENV_MODE = "BERNSTEIN_ADMISSION_MODE"
_ENV_COLD_START = "BERNSTEIN_ADMISSION_COLD_START"
_ENV_THRESHOLD = "BERNSTEIN_ADMISSION_MIN_CONFIDENCE"

#: Admitted only at or above this measured accuracy once a key has samples.
DEFAULT_MIN_CONFIDENCE: float = 0.5

#: Recorded when a proposal carries no producer identity. Kept distinct from
#: any real agent type so unattributed proposals accumulate their own history
#: instead of silently borrowing someone else's.
UNATTRIBUTED_PRODUCER = "unattributed"


@runtime_checkable
class Admissible(Protocol):
    """The three things the gate needs, wherever the upgrade came from.

    Deliberately structural rather than a base class: ``UpgradeProposal`` and
    ``UpgradeTransaction`` are separate lineages that reach the same executor
    families, and a shared parent would couple them for no reason. The gate
    needs a category, a trigger and a producer, and nothing else.
    """

    @property
    def category(self) -> object: ...

    @property
    def triggered_by(self) -> object: ...

    produced_by: str


class ColdStartMode(StrEnum):
    """What to do with a key that has fewer than ``min_samples`` outcomes."""

    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"


class AdmissionMode(StrEnum):
    """Whether a refusal actually blocks.

    ``OBSERVE`` is the default, and deliberately so. Enforcing on a cold
    database deadlocks: nothing is admitted, so nothing applies, so no outcome
    is ever recorded, so the sample count never reaches the threshold that
    would let anything through. Observe mode evaluates and records exactly as
    enforcement does, but does not block -- which is the only way a key can
    accumulate the history enforcement needs.
    """

    OBSERVE = "observe"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class AdmissionDecision:
    """Why a proposal was admitted or refused.

    Carries the exact ``agent_type``/``decision_key`` pair used, because the
    outcome must later be recorded against the identical key — recording
    against a different one silently poisons the history.
    """

    admitted: bool
    reason: str
    agent_type: str
    decision_key: str
    confidence: Confidence

    def to_dict(self) -> dict[str, object]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "agent_type": self.agent_type,
            "decision_key": self.decision_key,
            "confidence": self.confidence.to_dict(),
        }


def producer_identity(proposal: Admissible | UpgradeProposal) -> str:
    """The agent that produced this proposal, not the one that applies it."""
    produced_by = getattr(proposal, "produced_by", "") or ""
    produced_by = produced_by.strip()
    return produced_by or UNATTRIBUTED_PRODUCER


def decision_key(proposal: Admissible | UpgradeProposal) -> str:
    """``category:<category>|trigger:<triggered_by>``.

    Both halves are read through ``getattr(..., "value", ...)`` so the key is
    identical whether the field holds an enum member or a bare string.
    """
    category = getattr(proposal.category, "value", proposal.category)
    trigger = getattr(proposal.triggered_by, "value", proposal.triggered_by)
    return f"category:{category}|trigger:{trigger}"


def _resolve_mode(explicit: AdmissionMode | None) -> AdmissionMode:
    if explicit is not None:
        return explicit
    raw = os.environ.get(_ENV_MODE, "").strip().lower()
    if not raw:
        return AdmissionMode.OBSERVE
    try:
        return AdmissionMode(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r; falling back to %s",
            _ENV_MODE,
            raw,
            AdmissionMode.OBSERVE.value,
        )
        return AdmissionMode.OBSERVE


def _resolve_cold_start(explicit: ColdStartMode | None) -> ColdStartMode:
    if explicit is not None:
        return explicit
    raw = os.environ.get(_ENV_COLD_START, "").strip().lower()
    if not raw:
        return ColdStartMode.FAIL_CLOSED
    try:
        return ColdStartMode(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r; falling back to %s",
            _ENV_COLD_START,
            raw,
            ColdStartMode.FAIL_CLOSED.value,
        )
        return ColdStartMode.FAIL_CLOSED


def _resolve_threshold(explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    raw = os.environ.get(_ENV_THRESHOLD, "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            logger.warning("Ignoring invalid %s=%r", _ENV_THRESHOLD, raw)
        else:
            if 0.0 <= parsed <= 1.0:
                return parsed
            logger.warning("Ignoring out-of-range %s=%r", _ENV_THRESHOLD, raw)
    return DEFAULT_MIN_CONFIDENCE


class AdmissionPolicy:
    """Gate upgrade proposals on the proposer's measured history."""

    def __init__(
        self,
        *,
        query: ConfidenceQuery | None = None,
        cold_start: ColdStartMode | None = None,
        min_confidence: float | None = None,
        mode: AdmissionMode | None = None,
    ) -> None:
        self._query = query or ConfidenceQuery()
        self._cold_start = _resolve_cold_start(cold_start)
        self._min_confidence = _resolve_threshold(min_confidence)
        self._mode = _resolve_mode(mode)

    @property
    def mode(self) -> AdmissionMode:
        return self._mode

    @property
    def cold_start(self) -> ColdStartMode:
        return self._cold_start

    @property
    def min_confidence(self) -> float:
        return self._min_confidence

    def evaluate(self, proposal: Admissible | UpgradeProposal) -> AdmissionDecision:
        """Decide whether this proposal may be applied."""
        agent_type = producer_identity(proposal)
        key = decision_key(proposal)
        confidence = self._query.get(agent_type, key)

        if confidence.insufficient_data:
            admitted = self._cold_start is ColdStartMode.FAIL_OPEN
            reason = (
                f"cold start: {confidence.samples}/{confidence.min_samples} samples "
                f"for {agent_type} on {key}; mode={self._cold_start.value}"
            )
            return self._finalise(admitted, reason, agent_type, key, confidence)

        measured = confidence.value if confidence.value is not None else 0.0
        admitted = measured >= self._min_confidence
        reason = (
            f"measured accuracy {measured:.3f} "
            f"{'>=' if admitted else '<'} threshold {self._min_confidence:.3f} "
            f"over {confidence.samples} samples"
        )
        return self._finalise(admitted, reason, agent_type, key, confidence)

    def _finalise(
        self,
        admitted: bool,
        reason: str,
        agent_type: str,
        key: str,
        confidence: Confidence,
    ) -> AdmissionDecision:
        """Apply the mode. Observe never blocks, but keeps the real verdict in
        ``reason`` so a deployment can see what enforcement would do."""
        if not admitted and self._mode is AdmissionMode.OBSERVE:
            return AdmissionDecision(
                admitted=True,
                reason=f"observe mode (would refuse: {reason})",
                agent_type=agent_type,
                decision_key=key,
                confidence=confidence,
            )
        return AdmissionDecision(
            admitted=admitted,
            reason=reason,
            agent_type=agent_type,
            decision_key=key,
            confidence=confidence,
        )

    def record_outcome(
        self,
        decision: AdmissionDecision,
        success: bool,
        *,
        evidence_uri: str | None = None,
    ) -> None:
        """Record the result of an admitted proposal.

        Takes the decision rather than the proposal so the outcome lands on the
        identical key admission used. Recomputing the key here would let a
        proposal mutated between admission and apply record against a different
        history than the one that let it through.
        """
        self._query.record(
            decision.agent_type,
            decision.decision_key,
            success,
            evidence_uri=evidence_uri,
        )


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "UNATTRIBUTED_PRODUCER",
    "Admissible",
    "AdmissionDecision",
    "AdmissionMode",
    "AdmissionPolicy",
    "ColdStartMode",
    "decision_key",
    "producer_identity",
]
