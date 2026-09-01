"""Escalation ladder schema, evidence advance, and chain records (issue #4855 PR1).

PR1 is data-only: config + pure decide_* + audit helpers. agent_lifecycle
retry patching is deliberately untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from bernstein.core.config.config_schema import EscalationLadderStep, RoleModelPolicyEntry
from bernstein.core.config.seed_config import SeedError
from bernstein.core.config.seed_parser import (  # pyright: ignore[reportPrivateUsage]
    _parse_role_model_policy,
    _parse_single_role_policy,
)
from bernstein.core.routing.escalation_ladder import (
    EVIDENCE_DEGRADED_TERMINAL_OUTPUT,
    EVIDENCE_LOOP_VERDICT,
    EVIDENCE_VERIFICATION_FAILURE,
    LADDER_POLICY_VERSION,
    REASON_BUDGET_STOP,
    REASON_EXHAUSTED,
    REASON_MISSING_EVIDENCE,
    REASON_UNKNOWN_EVIDENCE_CLASS,
    FailureEvidence,
    LadderAdvanceInput,
    LadderStep,
    apply_monotonic_step,
    decide_ladder_advance,
    format_escalation_context,
    hop_record_digest,
    resolve_ladder_from_role_policy,
    validate_ladder_adapters,
    verify_hop_record_digest,
)
from bernstein.core.security.audit_chain import (
    EVENT_ESCALATION_LADDER_BUDGET_STOP,
    EVENT_ESCALATION_LADDER_EXHAUSTION,
    EVENT_ESCALATION_LADDER_HOP,
    EVENT_ESCALATION_LADDER_REFUSAL,
    AuditChainStore,
    record_escalation_ladder_budget_stop,
    record_escalation_ladder_exhaustion,
    record_escalation_ladder_hop,
    record_escalation_ladder_refusal,
)

_DIGEST = "a" * 64
_EVIDENCE = FailureEvidence(evidence_class=EVIDENCE_VERIFICATION_FAILURE, digest=_DIGEST)

_LADDER = (
    LadderStep(model="gpt-4.1-mini", adapter="codex", max_attempts=1),
    LadderStep(model="gemini-2.5-pro", adapter="gemini", max_attempts=1),
    LadderStep(model="o4-mini", adapter="codex", max_attempts=2),
)


def test_unset_ladder_preserves_role_entry_shape() -> None:
    """Unset ladder/fallback_model keeps RoleModelPolicyEntry dump byte-identical to today."""
    entry = RoleModelPolicyEntry(model="sonnet", cli="claude")
    dumped = entry.model_dump(exclude_none=True)
    assert "ladder" not in dumped
    assert "fallback_model" not in dumped
    assert "escalation_budget_usd" not in dumped
    assert resolve_ladder_from_role_policy(model=entry.model, ladder=None, fallback_model=None) is None


def test_fallback_model_sugar_two_step_ladder() -> None:
    entry = RoleModelPolicyEntry(model="gpt-4.1-mini", fallback_model="gpt-4.1")
    resolved = resolve_ladder_from_role_policy(
        model=entry.model,
        ladder=None,
        fallback_model=entry.fallback_model,
    )
    assert resolved == (
        LadderStep(model="gpt-4.1-mini", max_attempts=1),
        LadderStep(model="gpt-4.1", max_attempts=1),
    )


def test_ladder_and_fallback_model_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        RoleModelPolicyEntry(
            model="a",
            fallback_model="b",
            ladder=[EscalationLadderStep(model="a"), EscalationLadderStep(model="b")],
        )


def test_ladder_and_tier_models_are_mutually_exclusive_in_a_seed() -> None:
    """Both select a model, so a policy declaring each leaves a hop undefined."""
    with pytest.raises(SeedError, match="ladder and tier_models are mutually exclusive"):
        _parse_single_role_policy(
            "backend",
            {
                "model": "gpt-4.1-mini",
                "cli": "codex",
                "ladder": [{"model": "gpt-4.1-mini"}, {"model": "gpt-4.1"}],
                "tier_models": {"light": "gpt-4.1-mini"},
            },
        )


def test_tier_models_alone_still_parses() -> None:
    """The exclusion must not cost a policy that only declares tiers."""
    parsed = _parse_single_role_policy(
        "backend",
        {"model": "gpt-4.1-mini", "cli": "codex", "tier_models": {"light": "gpt-4.1-mini"}},
    )
    assert parsed["tier_models"] == {"light": "gpt-4.1-mini"}


def test_parse_ladder_seed_round_trip() -> None:
    parsed = _parse_single_role_policy(
        "backend",
        {
            "model": "gpt-4.1-mini",
            "cli": "codex",
            "ladder": [
                {"model": "gpt-4.1-mini", "adapter": "codex", "max_attempts": 1},
                {"model": "gemini-2.5-pro", "adapter": "gemini"},
            ],
            "escalation_budget_usd": 12.5,
        },
    )
    assert parsed["ladder"] == [
        {"model": "gpt-4.1-mini", "adapter": "codex", "max_attempts": 1},
        {"model": "gemini-2.5-pro", "adapter": "gemini", "max_attempts": 1},
    ]
    assert parsed["escalation_budget_usd"] == 12.5


def test_parse_rejects_unknown_adapter_at_config_read() -> None:
    with pytest.raises(SeedError, match="hard configuration failure"):
        _parse_single_role_policy(
            "backend",
            {
                "ladder": [
                    {"model": "x", "adapter": "definitely_not_a_registered_adapter_xyz"},
                ],
            },
        )


def test_validate_ladder_adapters_hard_fail_not_skip() -> None:
    steps = (LadderStep(model="x", adapter="no_such_adapter"),)
    with pytest.raises(ValueError, match="not skipped"):
        validate_ladder_adapters(steps, role="qa", known_adapters=frozenset({"codex", "gemini"}))


def test_advance_refused_without_evidence_and_recorded(tmp_path: Path) -> None:
    decision = decide_ladder_advance(
        LadderAdvanceInput(ladder=_LADDER, current_step=0, evidence=None),
    )
    assert decision.kind == "refuse"
    assert decision.reason == REASON_MISSING_EVIDENCE

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_escalation_ladder_refusal(
        chain=chain,
        run_id="run-1",
        task_id="task-1",
        from_step=0,
        reason=decision.reason,
        ladder_policy_version=decision.policy_version,
    )
    assert event.event_type == EVENT_ESCALATION_LADDER_REFUSAL
    rows = chain.query(event_type=EVENT_ESCALATION_LADDER_REFUSAL)
    assert len(rows) == 1
    assert rows[0].details["reason"] == REASON_MISSING_EVIDENCE
    assert "prev_chain_digest" in rows[0].details


def test_unknown_evidence_class_refused_like_missing(tmp_path: Path) -> None:
    decision = decide_ladder_advance(
        LadderAdvanceInput(
            ladder=_LADDER,
            current_step=0,
            evidence=FailureEvidence(evidence_class="vibes", digest=_DIGEST),
        ),
    )
    assert decision.kind == "refuse"
    assert decision.reason == REASON_UNKNOWN_EVIDENCE_CLASS

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_escalation_ladder_refusal(
        chain=chain,
        run_id="run-1",
        task_id="task-1",
        from_step=0,
        reason=decision.reason,
        evidence_class="vibes",
    )
    assert chain.query(event_type=EVENT_ESCALATION_LADDER_REFUSAL)[0].details["evidence_class"] == "vibes"


@pytest.mark.parametrize(
    "evidence_class",
    [
        EVIDENCE_VERIFICATION_FAILURE,
        EVIDENCE_LOOP_VERDICT,
        EVIDENCE_DEGRADED_TERMINAL_OUTPUT,
    ],
)
def test_hop_names_evidence_class_and_digest_replays(evidence_class: str, tmp_path: Path) -> None:
    evidence = FailureEvidence(evidence_class=evidence_class, digest=f"sha256:{_DIGEST}")
    decision = decide_ladder_advance(
        LadderAdvanceInput(ladder=_LADDER, current_step=0, evidence=evidence, attempts_on_step=2),
    )
    assert decision.kind == "advance"
    assert decision.to_step == 1
    assert decision.evidence_class == evidence_class
    assert decision.evidence_digest == f"sha256:{_DIGEST}"

    record = decision.to_record_dict(task_id="task-9")
    digest = hop_record_digest(record)
    assert verify_hop_record_digest(record, digest)

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_escalation_ladder_hop(
        chain=chain,
        run_id="run-1",
        task_id="task-9",
        from_step=0,
        to_step=1,
        evidence_class=evidence_class,
        evidence_digest=decision.evidence_digest or "",
        ladder_policy_version=LADDER_POLICY_VERSION,
        hop_digest=digest,
        escalation_context=decision.escalation_context or "",
    )
    details = chain.query(event_type=EVENT_ESCALATION_LADDER_HOP)[0].details
    assert details["evidence_class"] == evidence_class
    assert details["hop_digest"] == digest
    assert verify_hop_record_digest(record, details["hop_digest"])


def test_exhaustion_recorded(tmp_path: Path) -> None:
    decision = decide_ladder_advance(
        LadderAdvanceInput(ladder=_LADDER, current_step=2, evidence=_EVIDENCE),
    )
    assert decision.kind == "exhaust"
    assert decision.reason == REASON_EXHAUSTED

    record = decision.to_record_dict(task_id="task-x")
    digest = hop_record_digest(record)
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_escalation_ladder_exhaustion(
        chain=chain,
        run_id="run-1",
        task_id="task-x",
        from_step=2,
        evidence_class=EVIDENCE_VERIFICATION_FAILURE,
        evidence_digest=decision.evidence_digest or "",
        ladder_policy_version=LADDER_POLICY_VERSION,
        hop_digest=digest,
    )
    assert chain.query(event_type=EVENT_ESCALATION_LADDER_EXHAUSTION)[0].details["from_step"] == 2


def test_budget_guard_stops_with_recorded_reason(tmp_path: Path) -> None:
    decision = decide_ladder_advance(
        LadderAdvanceInput(
            ladder=_LADDER,
            current_step=0,
            evidence=_EVIDENCE,
            escalation_budget_usd=1.0,
            spend_usd=0.8,
            estimated_next_step_usd=0.5,
        ),
    )
    assert decision.kind == "budget_stop"
    assert decision.reason == REASON_BUDGET_STOP

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_escalation_ladder_budget_stop(
        chain=chain,
        run_id="run-1",
        task_id="task-b",
        from_step=0,
        reason=decision.reason,
        evidence_class=EVIDENCE_VERIFICATION_FAILURE,
        evidence_digest=decision.evidence_digest or "",
        ladder_policy_version=LADDER_POLICY_VERSION,
    )
    assert chain.query(event_type=EVENT_ESCALATION_LADDER_BUDGET_STOP)[0].details["reason"] == REASON_BUDGET_STOP


def test_non_claude_model_names_pass_through_untouched() -> None:
    """Regression: ladder must not rewrite adapter-specific model ids."""
    resolved = resolve_ladder_from_role_policy(
        model="gpt-4.1-mini",
        ladder=[
            {"model": "gpt-4.1-mini", "adapter": "codex"},
            {"model": "gemini-2.5-pro", "adapter": "gemini"},
            {"model": "accounts/fireworks/models/kimi-k2-instruct", "adapter": "openai_agents"},
        ],
        fallback_model=None,
    )
    assert resolved is not None
    assert [s.model for s in resolved] == [
        "gpt-4.1-mini",
        "gemini-2.5-pro",
        "accounts/fireworks/models/kimi-k2-instruct",
    ]
    decision = decide_ladder_advance(
        LadderAdvanceInput(ladder=resolved, current_step=0, evidence=_EVIDENCE),
    )
    assert decision.kind == "advance"
    assert resolved[decision.to_step or 0].model == "gemini-2.5-pro"


def test_monotonicity_never_steps_down_across_retries() -> None:
    step = 0
    for _ in range(3):
        # Compaction / retry without evidence must not move the step.
        refused = decide_ladder_advance(
            LadderAdvanceInput(ladder=_LADDER, current_step=step, evidence=None),
        )
        step = apply_monotonic_step(current_step=step, decision=refused)
        assert step == 0

    advanced = decide_ladder_advance(
        LadderAdvanceInput(ladder=_LADDER, current_step=step, evidence=_EVIDENCE),
    )
    step = apply_monotonic_step(current_step=step, decision=advanced)
    assert step == 1

    # Further refuse keeps step; never goes back to 0.
    refused_again = decide_ladder_advance(
        LadderAdvanceInput(ladder=_LADDER, current_step=step, evidence=None),
    )
    step = apply_monotonic_step(current_step=step, decision=refused_again)
    assert step == 1

    downward = advanced.__class__(
        kind="advance",
        from_step=1,
        to_step=0,
        reason="bogus",
        evidence_class=EVIDENCE_VERIFICATION_FAILURE,
        evidence_digest=f"sha256:{_DIGEST}",
        policy_version=LADDER_POLICY_VERSION,
    )
    with pytest.raises(ValueError, match="monotonicity"):
        apply_monotonic_step(current_step=1, decision=downward)


def test_escalation_context_in_patch_body_round_trips_byte_identically() -> None:
    line = format_escalation_context(
        from_step=0,
        to_step=1,
        attempts=2,
        evidence_class=EVIDENCE_LOOP_VERDICT,
    )
    assert line == "ESCALATION: step 0->1; attempts=2; evidence=loop_verdict"

    decision = decide_ladder_advance(
        LadderAdvanceInput(
            ladder=_LADDER,
            current_step=0,
            evidence=FailureEvidence(evidence_class=EVIDENCE_LOOP_VERDICT, digest=_DIGEST),
            attempts_on_step=2,
        ),
    )
    # Simulated retry patch body (agent_lifecycle seam not wired in PR1).
    patch_body = {
        "description": "retry work",
        "meta_messages": [decision.escalation_context],
        "model": _LADDER[1].model,
    }
    assert patch_body["meta_messages"] == [line]
    # Replay: same decision inputs → identical context line.
    again = decide_ladder_advance(
        LadderAdvanceInput(
            ladder=_LADDER,
            current_step=0,
            evidence=FailureEvidence(evidence_class=EVIDENCE_LOOP_VERDICT, digest=_DIGEST),
            attempts_on_step=2,
        ),
    )
    assert again.escalation_context == decision.escalation_context == line


def test_parse_role_model_policy_unset_ladder_identical_to_model_only() -> None:
    """Regression: roles without ladder dump the same keys as before #4855."""
    with_ladder_absent = _parse_role_model_policy({"backend": {"provider": "codex", "model": "gpt-4.1-mini"}})
    assert with_ladder_absent == {"backend": {"provider": "codex", "model": "gpt-4.1-mini"}}
