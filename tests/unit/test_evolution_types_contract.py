"""QA tests for the ChangeContract value type on UpgradeProposal.

Covers construction, type validation, and to_dict/from_dict round-trips for
``bernstein.evolution.types.ChangeContract`` (issue #5405 slice 2).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from bernstein.evolution.types import (
    ChangeContract,
    ChangeFalsifier,
    ChangeRollback,
    EffectDirection,
    PredictedEffect,
    ProposalStatus,
    RiskLevel,
    UpgradeProposal,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def predicted_effect() -> PredictedEffect:
    return PredictedEffect(metric="test_pass_rate", direction=EffectDirection.INCREASING)


@pytest.fixture()
def falsifier() -> ChangeFalsifier:
    return ChangeFalsifier(history_ref="metrics/2026-09-04.jsonl", expected_verdicts=["pass"])


@pytest.fixture()
def rollback() -> ChangeRollback:
    return ChangeRollback(files_to_restore=["src/bernstein/evolution/types.py"], change_description="revert")


@pytest.fixture()
def contract(
    predicted_effect: PredictedEffect,
    falsifier: ChangeFalsifier,
    rollback: ChangeRollback,
) -> ChangeContract:
    return ChangeContract(
        component="evolution.loop",
        target_fingerprint="abc123",
        predicted_effect=predicted_effect,
        invariants=["no_new_dependencies", "test_pass_rate_not_lower"],
        falsifier=falsifier,
        rollback=rollback,
    )


def _proposal(contract: ChangeContract | None = None) -> UpgradeProposal:
    return UpgradeProposal(
        id="UPG-0001",
        title="Test proposal",
        description="desc",
        risk_level=RiskLevel.L1_TEMPLATE,
        target_files=["src/bernstein/evolution/types.py"],
        diff="--- a/f\n+++ b/f\n@@\n",
        rationale="why",
        expected_impact="better",
        confidence=0.9,
        contract=contract,
    )


# ---------------------------------------------------------------------------
# 1. Construction: all required fields present - no raise
# ---------------------------------------------------------------------------


def test_contract_construction_with_all_required_fields(contract: ChangeContract) -> None:
    assert contract.component == "evolution.loop"
    assert contract.target_fingerprint == "abc123"
    assert contract.predicted_effect.metric == "test_pass_rate"
    assert contract.predicted_effect.direction is EffectDirection.INCREASING
    assert contract.invariants == ["no_new_dependencies", "test_pass_rate_not_lower"]
    assert contract.falsifier.history_ref == "metrics/2026-09-04.jsonl"
    assert contract.rollback.change_description == "revert"


# ---------------------------------------------------------------------------
# 2. Missing required field raises and the message names the field
# ---------------------------------------------------------------------------


def test_missing_required_fields_raise_and_message_names_the_first() -> None:
    """Dataclass __init__ raises TypeError listing every missing required
    positional argument by name."""
    with pytest.raises(TypeError, match="component"):
        ChangeContract()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "missing", ["component", "target_fingerprint", "predicted_effect", "invariants", "falsifier", "rollback"]
)
def test_each_required_field_is_enforced(
    missing: str,
    predicted_effect: PredictedEffect,
    falsifier: ChangeFalsifier,
    rollback: ChangeRollback,
) -> None:
    kwargs: dict[str, Any] = {
        "component": "c",
        "target_fingerprint": "fp",
        "predicted_effect": predicted_effect,
        "invariants": [],
        "falsifier": falsifier,
        "rollback": rollback,
    }
    del kwargs[missing]

    with pytest.raises(TypeError, match=missing):
        ChangeContract(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. Malformed predicted_effect (bare str instead of structured value)
# ---------------------------------------------------------------------------


def test_malformed_predicted_effect_bare_str_raises(
    predicted_effect: PredictedEffect, falsifier: ChangeFalsifier, rollback: ChangeRollback
) -> None:
    with pytest.raises(TypeError, match="predicted_effect must be PredictedEffect"):
        ChangeContract(
            component="c",
            target_fingerprint="fp",
            predicted_effect="test_pass_rate:increasing",  # type: ignore[arg-type]
            invariants=[],
            falsifier=falsifier,
            rollback=rollback,
        )


def test_malformed_predicted_effect_bad_direction_raises(falsifier: ChangeFalsifier, rollback: ChangeRollback) -> None:
    with pytest.raises(TypeError, match="direction must be EffectDirection"):
        PredictedEffect(metric="m", direction="increasing")  # type: ignore[arg-type]


def test_malformed_predicted_effect_bad_metric_raises() -> None:
    with pytest.raises(TypeError, match="metric must be str"):
        PredictedEffect(metric=42, direction=EffectDirection.INCREASING)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. Malformed invariants (bare str instead of sequence)
# ---------------------------------------------------------------------------


def test_malformed_invariants_bare_str_raises(
    predicted_effect: PredictedEffect, falsifier: ChangeFalsifier, rollback: ChangeRollback
) -> None:
    with pytest.raises(TypeError, match="invariants must be list"):
        ChangeContract(
            component="c",
            target_fingerprint="fp",
            predicted_effect=predicted_effect,
            invariants="no_new_dependencies",  # type: ignore[arg-type]
            falsifier=falsifier,
            rollback=rollback,
        )


def test_malformed_component_not_a_str(
    predicted_effect: PredictedEffect, falsifier: ChangeFalsifier, rollback: ChangeRollback
) -> None:
    with pytest.raises(TypeError, match="component must be str"):
        ChangeContract(
            component=123,  # type: ignore[arg-type]
            target_fingerprint="fp",
            predicted_effect=predicted_effect,
            invariants=[],
            falsifier=falsifier,
            rollback=rollback,
        )


def test_malformed_falsifier_bare_str_raises(predicted_effect: PredictedEffect, rollback: ChangeRollback) -> None:
    with pytest.raises(TypeError, match="falsifier must be ChangeFalsifier"):
        ChangeContract(
            component="c",
            target_fingerprint="fp",
            predicted_effect=predicted_effect,
            invariants=[],
            falsifier="history/metrics.jsonl",  # type: ignore[arg-type]
            rollback=rollback,
        )


def test_malformed_rollback_bare_str_raises(predicted_effect: PredictedEffect, falsifier: ChangeFalsifier) -> None:
    with pytest.raises(TypeError, match="rollback must be ChangeRollback"):
        ChangeContract(
            component="c",
            target_fingerprint="fp",
            predicted_effect=predicted_effect,
            invariants=[],
            falsifier=falsifier,
            rollback="revert it",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 5. Full contract serializes via to_dict and deserializes via from_dict
# ---------------------------------------------------------------------------


def test_full_contract_round_trip(contract: ChangeContract) -> None:
    d = contract.to_dict()

    assert set(d) == {
        "component",
        "target_fingerprint",
        "predicted_effect",
        "invariants",
        "falsifier",
        "rollback",
    }
    assert d["predicted_effect"] == {"metric": "test_pass_rate", "direction": "increasing"}

    restored = ChangeContract.from_dict(copy.deepcopy(d))

    assert restored == contract
    # And it round-trips again identically.
    assert restored.to_dict() == d


def test_contract_from_dict_accepts_its_own_to_dict_output(contract: ChangeContract) -> None:
    restored = ChangeContract.from_dict(contract.to_dict())

    assert restored == contract


def test_contract_equality_is_value_based(contract: ChangeContract) -> None:
    twin = ChangeContract.from_dict(contract.to_dict())

    assert twin == contract
    assert twin is not contract


# ---------------------------------------------------------------------------
# 6. UpgradeProposal with contract: to_dict/from_dict round-trip preserves it
# ---------------------------------------------------------------------------


def test_proposal_with_contract_round_trip_preserves_contract(contract: ChangeContract) -> None:
    proposal = _proposal(contract=contract)

    d = proposal.to_dict()
    assert d["contract"] is not None

    restored = UpgradeProposal.from_dict(copy.deepcopy(d))

    assert restored.contract == contract
    assert restored == proposal


def test_proposal_with_contract_serializes_contract_as_nested_dict(contract: ChangeContract) -> None:
    d = _proposal(contract=contract).to_dict()

    nested = d["contract"]
    assert isinstance(nested, dict)
    assert nested["component"] == contract.component


# ---------------------------------------------------------------------------
# 7. UpgradeProposal without contract (contract=None): round-trip still works
# ---------------------------------------------------------------------------


def test_proposal_without_contract_round_trip() -> None:
    proposal = _proposal(contract=None)

    d = proposal.to_dict()
    assert d["contract"] is None

    restored = UpgradeProposal.from_dict(copy.deepcopy(d))

    assert restored.contract is None
    assert restored == proposal


def test_proposal_contract_field_defaults_to_none() -> None:
    proposal = _proposal()

    assert proposal.contract is None


# ---------------------------------------------------------------------------
# 8. UpgradeProposal contract field is optional (can be omitted entirely)
# ---------------------------------------------------------------------------


def test_proposal_contract_field_omitted_entirely() -> None:
    proposal = UpgradeProposal(
        id="UPG-0002",
        title="No contract",
        description="desc",
        risk_level=RiskLevel.L0_CONFIG,
        target_files=[],
        diff="",
        rationale="why",
        expected_impact="better",
        confidence=0.5,
    )

    assert proposal.contract is None

    restored = UpgradeProposal.from_dict(proposal.to_dict())

    assert restored.contract is None
    assert restored == proposal


def test_proposal_from_dict_tolerates_missing_contract_key() -> None:
    """Old serialized proposals predating the contract field load cleanly."""
    d = {
        "id": "UPG-0003",
        "title": "Legacy",
        "description": "desc",
        "risk_level": "config",
        "target_files": [],
        "diff": "",
        "rationale": "why",
        "expected_impact": "better",
        "confidence": 0.5,
        "status": "pending",
    }

    restored = UpgradeProposal.from_dict(d)

    assert restored.contract is None
    assert restored.status is ProposalStatus.PENDING
