"""Typed activity boundary for any agent modality (issue #2311).

Bernstein's deterministic scheduler is validated for coding agents, but the same
control plane generalizes to research, browser, data, and ops agents. This
module is the uniform contract -- one typed activity boundary -- where every
agent kind, whatever its modality, returns an artifact plus the hashes needed to
replay it. The scheduler stays deterministic and the agent stays an opaque
stochastic activity behind a hash-in / hash-out contract.

The boundary is a small, closed surface:

* :class:`Observation` -- one content-addressed evidence unit. The bytes the
  agent saw at a decision point (a fetched page, a screenshot, a DOM snapshot, a
  signed input artifact) are hashed *at capture time*, so the content hash is a
  forensic record of "the exact bytes behind this decision".
* :class:`ActivityResult` -- the typed result every modality returns:
  ``{artifact, artifact_hash, evidence_set_hash, terminal_state, reason_code}``.
  ``evidence_set_hash`` is a deterministic hash over the *set* of observation
  content hashes, so two runs that gathered the same exogenous signal in any
  order carry the same evidence identity.
* :func:`validate_activity_result` -- the boundary check (AC2). A result whose
  ``artifact_hash`` or ``evidence_set_hash`` does not recompute, whose
  ``terminal_state`` is not a known state, or whose ``reason_code`` is empty is
  rejected with a typed :class:`ActivityRejected` refusal *before* it reaches
  the journal.
* :func:`dispatch_activity` -- the scheduler-facing entry point. It validates
  the result, pins ``evidence_set_hash`` into the run
  :class:`~bernstein.core.replay.journal.EventJournal` as one
  ``activity.result`` event (AC3), optionally refuses a stage that introduces no
  new evidence hash via a :class:`RedundancyLedger` (AC4), and -- when an audit
  chain is supplied -- mirrors the boundary into the HMAC-chained audit log.

The module owns the *boundary*, not the modality-specific execution: the
concrete modality runners live in
:mod:`bernstein.core.orchestration.activity_modalities` and hand a built
:class:`ActivityResult` in. That keeps the deterministic dispatch path free of
any live model call, which is what makes the cross-modality replay guarantee
hold -- the outer sequence of anchored evidence-set hashes is a pure function of
the observations, independent of the stochastic inner execution.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIVITY_RESULT_EVENT",
    "ActivityKind",
    "ActivityRejected",
    "ActivityResult",
    "DispatchOutcome",
    "Observation",
    "RedundancyLedger",
    "RedundantEvidenceRefused",
    "TerminalState",
    "dispatch_activity",
    "evidence_set_hash",
    "validate_activity_result",
]

#: Journal event type stamped for each crossed activity boundary. Kept in
#: lockstep with :data:`bernstein.core.security.audit_chain.EVENT_ACTIVITY_RESULT`.
ACTIVITY_RESULT_EVENT = "activity.result"


class ActivityKind(enum.Enum):
    """The agent modality behind a typed activity.

    ``CODING`` is the modality the scheduler is already validated for; the other
    members are the non-coding modalities the boundary generalizes to. The value
    is the stable string stamped into the journal and the audit chain.
    """

    CODING = "coding"
    RESEARCH = "research"
    BROWSER = "browser"
    DATA = "data"
    OPS = "ops"


class TerminalState(enum.Enum):
    """The typed terminal state of an activity boundary crossing.

    A closed set so a downstream verifier can exhaustively reason about how an
    opaque activity ended without parsing free text.
    """

    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class ActivityRejected(ValueError):
    """A malformed activity result was rejected at the boundary (AC2).

    Raised by :func:`validate_activity_result` (and therefore by
    :func:`dispatch_activity`) before the result is anchored, so a result whose
    hashes do not recompute, whose terminal state is unknown, or whose reason
    code is empty never reaches the journal or the audit chain.
    """


class RedundantEvidenceRefused(ValueError):
    """A stage was refused because it introduces no new evidence hash (AC4).

    The deterministic scheduler refuses to add a stage whose ``evidence_set_hash``
    equals a prior stage's: a stage that re-derives an already-seen exogenous
    signal contributes nothing new and would only inflate the task graph. The
    message names the prior stage that already contributed the signal.
    """


def _canonical_bytes(value: Any) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    """Return the ``sha256:`` content hash of raw bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_json(value: Any) -> str:
    """Return the ``sha256:`` content hash of a canonical JSON projection."""
    return _sha256_bytes(_canonical_bytes(value))


@dataclass(frozen=True, slots=True)
class Observation:
    """One content-addressed evidence unit captured at a decision point.

    The evidence behind any modality's decision -- a fetched page, a screenshot,
    a DOM snapshot, a signed input artifact -- is hashed the moment it is
    captured, so the content hash is a forensic record of the exact bytes the
    agent saw. The ``ref`` (a URL, a decision-step label, an artifact path) is
    provenance metadata and is deliberately *not* part of the content hash: two
    identical byte blobs are the same evidence regardless of where they came
    from.

    Attributes:
        kind: Observation category (``page`` / ``snapshot`` / ``artifact`` ...).
        ref: Human-facing provenance reference (URL, decision-step label, path).
        content_hash: The ``sha256:``-prefixed hash of the captured bytes.
    """

    kind: str
    ref: str
    content_hash: str

    @classmethod
    def of(cls, *, kind: str, ref: str, content: bytes) -> Observation:
        """Content-address *content* at capture time and return an observation.

        Args:
            kind: Observation category.
            ref: Provenance reference (not part of the content hash).
            content: The exact bytes captured behind the decision.

        Returns:
            An :class:`Observation` whose ``content_hash`` fixes the bytes.
        """
        return cls(kind=kind, ref=ref, content_hash=_sha256_bytes(content))


def evidence_set_hash(observations: Iterable[Observation]) -> str:
    """Return the deterministic hash over the *set* of observation hashes.

    The evidence set is a set: the hash is computed over the sorted, de-duplicated
    observation content hashes, so two stages that gathered the same exogenous
    signal in a different order -- or that fetched the same page twice -- carry
    the same evidence identity. This is what the redundancy-refusal check keys on
    (AC4) and what a replay reattaches against (AC1).

    Args:
        observations: The observations contributing exogenous signal.

    Returns:
        A ``sha256:``-prefixed hash that is order- and duplicate-independent.
    """
    unique_sorted = sorted({o.content_hash for o in observations})
    return _sha256_json(unique_sorted)


@dataclass(frozen=True, slots=True)
class ActivityResult:
    """The typed result every modality returns across the activity boundary.

    This is the hash-in / hash-out contract: the scheduler never inspects the
    stochastic ``artifact`` body, only the hashes and the typed terminal state.

    Attributes:
        kind: The agent modality behind the activity.
        artifact: The modality's opaque result payload (JSON-serialisable).
        artifact_hash: The ``sha256:`` hash of the canonical artifact projection.
        evidence_set_hash: The ``sha256:`` hash over the observation set.
        terminal_state: The typed terminal state.
        reason_code: A short, non-empty machine reason code for the terminal
            state (e.g. ``ok`` / ``policy_denied`` / ``budget_exhausted``).
        observations: The content-addressed evidence the activity gathered.
    """

    kind: ActivityKind
    artifact: Any
    artifact_hash: str
    evidence_set_hash: str
    terminal_state: TerminalState
    reason_code: str
    observations: tuple[Observation, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        kind: ActivityKind,
        artifact: Any,
        observations: tuple[Observation, ...] = (),
        terminal_state: TerminalState = TerminalState.COMPLETED,
        reason_code: str = "ok",
    ) -> ActivityResult:
        """Build a result, computing the artifact and evidence-set hashes.

        The computed result satisfies :func:`validate_activity_result` by
        construction; the validator still runs at the boundary to catch
        hand-forged or wire-deserialised results.

        Args:
            kind: The agent modality.
            artifact: The opaque result payload.
            observations: The gathered evidence.
            terminal_state: The typed terminal state.
            reason_code: A non-empty reason code.

        Returns:
            A fully-populated :class:`ActivityResult`.
        """
        return cls(
            kind=kind,
            artifact=artifact,
            artifact_hash=_sha256_json(artifact),
            evidence_set_hash=evidence_set_hash(observations),
            terminal_state=terminal_state,
            reason_code=reason_code,
            observations=observations,
        )


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """The result of crossing one activity boundary through the scheduler.

    Attributes:
        result: The validated :class:`ActivityResult`.
        journal_index: 0-based index of the anchoring journal entry, or ``None``
            when no journal was supplied.
        journal_event_hash: The anchoring journal entry's Merkle ``event_hash``,
            or empty when no journal was supplied.
    """

    result: ActivityResult
    journal_index: int | None = None
    journal_event_hash: str = ""


def validate_activity_result(result: ActivityResult) -> ActivityResult:
    """Validate an activity result at the boundary (AC2).

    Enforces every invariant a downstream verifier depends on:

    * ``kind`` and ``terminal_state`` are members of their enums.
    * ``reason_code`` is a non-empty string.
    * ``artifact_hash`` recomputes from the artifact.
    * ``evidence_set_hash`` recomputes from the observations.
    * every observation carries a ``sha256:``-shaped content hash.

    Raises :class:`ActivityRejected` on the first violation, so a malformed or
    tampered result never reaches the journal or the audit chain.

    Args:
        result: The result to validate.

    Returns:
        The same result, unchanged, when valid.

    Raises:
        ActivityRejected: When any invariant is violated.
    """
    if not isinstance(result.kind, ActivityKind):
        raise ActivityRejected(f"unknown activity kind: {result.kind!r}")
    if not isinstance(result.terminal_state, TerminalState):
        raise ActivityRejected(f"unknown terminal_state: {result.terminal_state!r}")
    if not isinstance(result.reason_code, str) or not result.reason_code.strip():
        raise ActivityRejected("reason_code must be a non-empty string")

    expected_artifact_hash = _sha256_json(result.artifact)
    if result.artifact_hash != expected_artifact_hash:
        raise ActivityRejected(
            f"artifact_hash mismatch: declared {result.artifact_hash!r}, recomputed {expected_artifact_hash!r}"
        )

    for obs in result.observations:
        if not isinstance(obs.content_hash, str) or not obs.content_hash.startswith("sha256:"):
            raise ActivityRejected(f"observation {obs.ref!r} has a malformed content hash: {obs.content_hash!r}")

    expected_evidence_hash = evidence_set_hash(result.observations)
    if result.evidence_set_hash != expected_evidence_hash:
        raise ActivityRejected(
            f"evidence_set_hash mismatch: declared {result.evidence_set_hash!r}, recomputed {expected_evidence_hash!r}"
        )

    return result


@dataclass
class RedundancyLedger:
    """Tracks which evidence sets a run's stages have already contributed (AC4).

    The deterministic scheduler consults the ledger before admitting a stage: a
    stage whose ``evidence_set_hash`` equals one already admitted introduces no
    new exogenous signal and is refused. The ledger is per-run mutable state,
    keyed only on the evidence-set hash (never on the artifact), so the refusal
    decision is a pure function of the observations.
    """

    _seen: dict[str, str] = field(default_factory=dict)

    def seen_hashes(self) -> frozenset[str]:
        """Return the set of admitted evidence-set hashes."""
        return frozenset(self._seen)

    def stage_for(self, evidence_hash: str) -> str | None:
        """Return the stage id that first admitted *evidence_hash*, if any."""
        return self._seen.get(evidence_hash)

    def admit(self, evidence_hash: str, *, stage_id: str) -> None:
        """Admit a new evidence set, or refuse a duplicate (AC4).

        Args:
            evidence_hash: The stage's ``evidence_set_hash``.
            stage_id: The stage being admitted.

        Raises:
            RedundantEvidenceRefused: When *evidence_hash* was already admitted
                by a prior stage.
        """
        prior = self._seen.get(evidence_hash)
        if prior is not None:
            raise RedundantEvidenceRefused(
                f"stage {stage_id!r} introduces no new evidence hash "
                f"(evidence_set_hash already contributed by stage {prior!r})"
            )
        self._seen[evidence_hash] = stage_id


def dispatch_activity(
    result: ActivityResult,
    *,
    stage_id: str,
    journal: EventJournal | None = None,
    chain: AuditChainStore | None = None,
    redundancy_ledger: RedundancyLedger | None = None,
) -> DispatchOutcome:
    """Cross one activity boundary through the deterministic scheduler.

    The modality runner has already produced *result*; this is the single
    scheduler-facing dispatch path every modality routes through. It:

    1. Validates the result at the boundary (AC2) -- a malformed result raises
       :class:`ActivityRejected` before anything is written.
    2. Refuses a stage that introduces no new evidence hash (AC4) when a
       :class:`RedundancyLedger` is supplied, logging the refusal.
    3. Pins ``evidence_set_hash`` into the run journal as one ``activity.result``
       event so a replay reattaches the same bytes (AC3).
    4. Mirrors the boundary into the HMAC-chained audit log when a chain is
       supplied.

    The evidence-set hash is admitted to the ledger only *after* validation and
    only when the journal write is about to happen, so a refused or malformed
    stage never mutates the ledger.

    Args:
        result: The modality's activity result.
        stage_id: The scheduler stage id crossing the boundary.
        journal: Run event journal to anchor the activity into.
        chain: Optional HMAC audit chain to mirror the boundary into.
        redundancy_ledger: Optional per-run ledger enforcing the
            redundancy-refusal check.

    Returns:
        A :class:`DispatchOutcome` with the validated result and anchoring
        metadata.

    Raises:
        ActivityRejected: When the result is malformed.
        RedundantEvidenceRefused: When the stage introduces no new evidence.
    """
    validated = validate_activity_result(result)

    if redundancy_ledger is not None:
        try:
            redundancy_ledger.admit(validated.evidence_set_hash, stage_id=stage_id)
        except RedundantEvidenceRefused as refusal:
            logger.warning(
                "activity dispatch refused: stage=%s introduces no new evidence hash "
                "(evidence_set_hash=%s already seen) - stage not anchored",
                sanitize_log(stage_id),
                validated.evidence_set_hash,
            )
            raise refusal

    journal_index: int | None = None
    journal_event_hash = ""
    if journal is not None:
        journal_index = journal.event_count()
        journal.record(
            ACTIVITY_RESULT_EVENT,
            stage_id=stage_id,
            kind=validated.kind.value,
            artifact_hash=validated.artifact_hash,
            evidence_set_hash=validated.evidence_set_hash,
            terminal_state=validated.terminal_state.value,
            reason_code=validated.reason_code,
            observations=[
                {"kind": o.kind, "ref": o.ref, "content_hash": o.content_hash} for o in validated.observations
            ],
        )
        journal_event_hash = journal.head()

    if chain is not None:
        from bernstein.core.security.audit_chain import record_activity_result

        record_activity_result(
            chain=chain,
            run_id=journal.run_id if journal is not None else "",
            stage_id=stage_id,
            kind=validated.kind.value,
            artifact_hash=validated.artifact_hash,
            evidence_set_hash=validated.evidence_set_hash,
            terminal_state=validated.terminal_state.value,
            reason_code=validated.reason_code,
            journal_index=journal_index if journal_index is not None else -1,
            journal_event_hash=journal_event_hash,
        )

    logger.debug(
        "activity dispatched: stage=%s kind=%s terminal=%s evidence_set_hash=%s",
        sanitize_log(stage_id),
        validated.kind.value,
        validated.terminal_state.value,
        validated.evidence_set_hash,
    )
    return DispatchOutcome(
        result=validated,
        journal_index=journal_index,
        journal_event_hash=journal_event_hash,
    )
