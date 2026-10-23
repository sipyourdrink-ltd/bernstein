"""Escalation ladder policy and evidence-driven advance decisions (issue #4855).

PR1 lands the ladder as **data only**: config shapes, evidence classes, pure
advance/refuse/exhaust decisions, hop digests for replay, and escalation-
context formatting. ``agent_lifecycle`` retry patching is unchanged; wiring
the ladder into retries is a follow-up.

Invariant: an escalation advance is **caused** by verified failure evidence,
not merely accompanied by a retry counter. :func:`decide_ladder_advance`
reads the evidence first; missing or unknown evidence refuses the hop.

Unrunnable steps (empty model, adapter not installed) hard-fail when the
config is read — never silently skipped — so the operator's written policy
is not rewritten at 3am. See :func:`validate_ladder_adapters`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from bernstein.core.security.agent_card_signer import canonicalize_jcs

#: Bumped when hop-record fields that enter the digest change shape.
LADDER_POLICY_VERSION = 1

EVIDENCE_VERIFICATION_FAILURE = "verification_failure"
EVIDENCE_LOOP_VERDICT = "loop_verdict"
EVIDENCE_DEGRADED_TERMINAL_OUTPUT = "degraded_terminal_output"

QUALIFYING_EVIDENCE_CLASSES: frozenset[str] = frozenset(
    {
        EVIDENCE_VERIFICATION_FAILURE,
        EVIDENCE_LOOP_VERDICT,
        EVIDENCE_DEGRADED_TERMINAL_OUTPUT,
    }
)

REASON_MISSING_EVIDENCE = "missing_evidence"
REASON_UNKNOWN_EVIDENCE_CLASS = "unknown_evidence_class"
REASON_ADVANCED = "advanced"
REASON_EXHAUSTED = "ladder_exhausted"
REASON_BUDGET_STOP = "escalation_budget_exhausted"
REASON_MONOTONICITY = "monotonicity_violation"
REASON_EMPTY_LADDER = "empty_ladder"
REASON_STEP_OUT_OF_RANGE = "step_out_of_range"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

DecisionKind = Literal["advance", "refuse", "exhaust", "budget_stop"]


@dataclass(frozen=True)
class LadderStep:
    """One rung of an escalation ladder.

    ``model`` is passed through to the adapter unmodified — nothing here
    assumes Claude (or any vendor) tier names.
    """

    model: str
    adapter: str | None = None
    max_attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "max_attempts": self.max_attempts}
        if self.adapter is not None:
            payload["adapter"] = self.adapter
        return payload


@dataclass(frozen=True)
class FailureEvidence:
    """Verified failure evidence that may cause a ladder advance.

    ``digest`` is the content address of the failure artefact the run already
    produces (gate result, test-run summary, loop verdict, degraded-terminal
    projection). Advance reads this reference; without it the hop is refused.
    """

    evidence_class: str
    digest: str

    def normalized_digest(self) -> str:
        """Return the bare 64-char hex digest (strip optional ``sha256:``)."""
        raw = self.digest.strip()
        if raw.startswith("sha256:"):
            raw = raw[7:]
        raw = raw.lower()
        if not _HEX64_RE.fullmatch(raw):
            raise ValueError(f"evidence digest must be 64 hex chars (optional sha256: prefix), got {self.digest!r}")
        return raw

    def digest_for_record(self) -> str:
        return f"sha256:{self.normalized_digest()}"


@dataclass(frozen=True)
class LadderAdvanceInput:
    """Inputs to :func:`decide_ladder_advance`.

    ``current_step`` is the 0-based index of the step that just failed.
    ``spend_usd`` / ``estimated_next_step_usd`` consult the existing cost
    surface; when ``escalation_budget_usd`` is set, climbing past it stops
    with :data:`REASON_BUDGET_STOP`.
    """

    ladder: tuple[LadderStep, ...]
    current_step: int
    evidence: FailureEvidence | None
    escalation_budget_usd: float | None = None
    spend_usd: float = 0.0
    estimated_next_step_usd: float = 0.0
    policy_version: int = LADDER_POLICY_VERSION
    attempts_on_step: int = 1


@dataclass(frozen=True)
class LadderDecision:
    """Pure outcome of an evidence-gated ladder advance attempt."""

    kind: DecisionKind
    from_step: int
    to_step: int | None
    reason: str
    evidence_class: str | None
    evidence_digest: str | None
    policy_version: int
    escalation_context: str | None = None

    def to_record_dict(self, *, task_id: str) -> dict[str, Any]:
        """Canonical hop/refusal/exhaustion projection for chain + replay."""
        return {
            "task_id": task_id,
            "kind": self.kind,
            "from_step": self.from_step,
            "to_step": self.to_step,
            "reason": self.reason,
            "evidence_class": self.evidence_class,
            "evidence_digest": self.evidence_digest,
            "ladder_policy_version": self.policy_version,
            "escalation_context": self.escalation_context,
        }


def resolve_ladder_from_role_policy(
    *,
    model: str | None,
    ladder: list[dict[str, Any] | LadderStep] | tuple[LadderStep, ...] | None,
    fallback_model: str | None,
) -> tuple[LadderStep, ...] | None:
    """Materialize a ladder from role policy fields.

    Unset ``ladder`` and unset ``fallback_model`` → ``None`` (today's
    behaviour). ``fallback_model`` alone is sugar for a two-step ladder
    ``[model, fallback_model]``. Explicit ``ladder`` and ``fallback_model``
    together are a conflict (raised by config validation).
    """
    if ladder is not None:
        steps = tuple(_coerce_step(item) for item in ladder)
        if not steps:
            raise ValueError("ladder must be non-empty when set")
        return steps
    if fallback_model is not None:
        if not isinstance(fallback_model, str) or not fallback_model.strip():
            raise ValueError("fallback_model must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("fallback_model requires model to be set (two-step sugar)")
        return (
            LadderStep(model=model.strip(), max_attempts=1),
            LadderStep(model=fallback_model.strip(), max_attempts=1),
        )
    return None


def _coerce_step(item: dict[str, Any] | LadderStep) -> LadderStep:
    if isinstance(item, LadderStep):
        return item
    model = item.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("ladder step model must be a non-empty string")
    adapter = item.get("adapter")
    if adapter is not None and (not isinstance(adapter, str) or not adapter.strip()):
        raise ValueError("ladder step adapter must be a non-empty string when set")
    max_attempts = item.get("max_attempts", 1)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("ladder step max_attempts must be a positive integer")
    return LadderStep(
        model=model.strip(),
        adapter=adapter.strip() if isinstance(adapter, str) else None,
        max_attempts=max_attempts,
    )


def validate_ladder_adapters(
    steps: tuple[LadderStep, ...] | list[LadderStep],
    *,
    role: str,
    known_adapters: frozenset[str],
) -> None:
    """Hard-fail when a step names an adapter that is not installed.

    Skipping would keep runs alive while silently changing the policy the
    operator wrote. Missing adapters fail when the config is read, not when
    escalation fires unattended.
    """
    for index, step in enumerate(steps):
        if step.adapter is None:
            continue
        if step.adapter not in known_adapters:
            known = ", ".join(sorted(known_adapters)) or "(none)"
            raise ValueError(
                f"role_model_policy.{role}.ladder[{index}].adapter={step.adapter!r} is not an "
                f"installed selectable adapter. Known: {known}. Unrunnable ladder steps are a "
                "hard configuration failure (not skipped)."
            )


def format_escalation_context(
    *,
    from_step: int,
    to_step: int,
    attempts: int,
    evidence_class: str,
) -> str:
    """One-line brief patched onto the retry task so the next model does not
    repeat the failed approach verbatim. Byte-stable for replay."""
    return f"ESCALATION: step {from_step}->{to_step}; attempts={attempts}; evidence={evidence_class}"


def hop_record_digest(record: dict[str, Any]) -> str:
    """Content-address a hop/refusal/exhaustion projection for replay verify."""
    digest = hashlib.sha256(canonicalize_jcs(record)).hexdigest()
    return f"sha256:{digest}"


def verify_hop_record_digest(record: dict[str, Any], expected: str) -> bool:
    """True when recomputing :func:`hop_record_digest` matches ``expected``."""
    return hop_record_digest(record) == expected


def decide_ladder_advance(inp: LadderAdvanceInput) -> LadderDecision:
    """Decide whether evidence causes a hop, refusal, exhaustion, or budget stop.

    Evidence is read first. A retry counter alone never advances the ladder.
    """
    version = inp.policy_version
    from_step = inp.current_step

    if not inp.ladder:
        return LadderDecision(
            kind="refuse",
            from_step=from_step,
            to_step=None,
            reason=REASON_EMPTY_LADDER,
            evidence_class=None,
            evidence_digest=None,
            policy_version=version,
        )

    if from_step < 0 or from_step >= len(inp.ladder):
        return LadderDecision(
            kind="refuse",
            from_step=from_step,
            to_step=None,
            reason=REASON_STEP_OUT_OF_RANGE,
            evidence_class=None,
            evidence_digest=None,
            policy_version=version,
        )

    evidence = inp.evidence
    if evidence is None:
        return LadderDecision(
            kind="refuse",
            from_step=from_step,
            to_step=None,
            reason=REASON_MISSING_EVIDENCE,
            evidence_class=None,
            evidence_digest=None,
            policy_version=version,
        )

    if evidence.evidence_class not in QUALIFYING_EVIDENCE_CLASSES:
        return LadderDecision(
            kind="refuse",
            from_step=from_step,
            to_step=None,
            reason=REASON_UNKNOWN_EVIDENCE_CLASS,
            evidence_class=evidence.evidence_class,
            evidence_digest=None,
            policy_version=version,
        )

    try:
        digest = evidence.digest_for_record()
    except ValueError:
        return LadderDecision(
            kind="refuse",
            from_step=from_step,
            to_step=None,
            reason=REASON_MISSING_EVIDENCE,
            evidence_class=evidence.evidence_class,
            evidence_digest=None,
            policy_version=version,
        )

    if from_step >= len(inp.ladder) - 1:
        return LadderDecision(
            kind="exhaust",
            from_step=from_step,
            to_step=None,
            reason=REASON_EXHAUSTED,
            evidence_class=evidence.evidence_class,
            evidence_digest=digest,
            policy_version=version,
        )

    to_step = from_step + 1
    if to_step <= from_step:
        return LadderDecision(
            kind="refuse",
            from_step=from_step,
            to_step=None,
            reason=REASON_MONOTONICITY,
            evidence_class=evidence.evidence_class,
            evidence_digest=digest,
            policy_version=version,
        )

    if inp.escalation_budget_usd is not None:
        projected = inp.spend_usd + inp.estimated_next_step_usd
        if projected > inp.escalation_budget_usd:
            return LadderDecision(
                kind="budget_stop",
                from_step=from_step,
                to_step=None,
                reason=REASON_BUDGET_STOP,
                evidence_class=evidence.evidence_class,
                evidence_digest=digest,
                policy_version=version,
            )

    context = format_escalation_context(
        from_step=from_step,
        to_step=to_step,
        attempts=inp.attempts_on_step,
        evidence_class=evidence.evidence_class,
    )
    return LadderDecision(
        kind="advance",
        from_step=from_step,
        to_step=to_step,
        reason=REASON_ADVANCED,
        evidence_class=evidence.evidence_class,
        evidence_digest=digest,
        policy_version=version,
        escalation_context=context,
    )


def apply_monotonic_step(*, current_step: int, decision: LadderDecision) -> int:
    """Return the next step index, never stepping down the ladder."""
    if decision.kind != "advance" or decision.to_step is None:
        return current_step
    if decision.to_step < current_step:
        raise ValueError(f"ladder monotonicity violated: cannot move from step {current_step} to {decision.to_step}")
    if decision.to_step < decision.from_step:
        raise ValueError(f"ladder monotonicity violated: from_step={decision.from_step} to_step={decision.to_step}")
    return decision.to_step


__all__ = [
    "EVIDENCE_DEGRADED_TERMINAL_OUTPUT",
    "EVIDENCE_LOOP_VERDICT",
    "EVIDENCE_VERIFICATION_FAILURE",
    "LADDER_POLICY_VERSION",
    "QUALIFYING_EVIDENCE_CLASSES",
    "REASON_ADVANCED",
    "REASON_BUDGET_STOP",
    "REASON_EMPTY_LADDER",
    "REASON_EXHAUSTED",
    "REASON_MISSING_EVIDENCE",
    "REASON_MONOTONICITY",
    "REASON_STEP_OUT_OF_RANGE",
    "REASON_UNKNOWN_EVIDENCE_CLASS",
    "FailureEvidence",
    "LadderAdvanceInput",
    "LadderDecision",
    "LadderStep",
    "apply_monotonic_step",
    "decide_ladder_advance",
    "format_escalation_context",
    "hop_record_digest",
    "resolve_ladder_from_role_policy",
    "validate_ladder_adapters",
    "verify_hop_record_digest",
]
