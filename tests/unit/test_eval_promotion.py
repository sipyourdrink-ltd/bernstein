"""Tests for deterministic stage promotion and revocation (#2520).

The stage assignment at any moment is a pure fold over the ordered chain of
verdict receipts: no state file holds the assignment, the receipt chain IS the
assignment. A significant_regression at canary or default emits a revocation
receipt that reverts the projection to the prior default.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_EVAL_GATE_REVOCATION,
    AuditChainStore,
)
from bernstein.eval.promotion import (
    DEFAULT_CANARY_TO_DEFAULT_M,
    DEFAULT_SHADOW_TO_CANARY_K,
    Stage,
    VerdictStep,
    build_revocation_receipt,
    project,
    replay_history,
    verify_revocation_receipt,
)
from bernstein.eval.significance import Verdict

_KEY = b"k" * 32


def _step(idx: int, verdict: Verdict) -> VerdictStep:
    return VerdictStep(
        receipt_hash="sha256:" + f"{idx:064d}",
        verdict=verdict,
        candidate_config_id="cfg-candidate",
        baseline_config_id="cfg-baseline",
    )


def _chain(verdicts: list[Verdict]) -> list[VerdictStep]:
    return [_step(i, v) for i, v in enumerate(verdicts)]


# ---------------------------------------------------------------------------
# Promotion ladder
# ---------------------------------------------------------------------------


def test_shadow_promotes_to_canary_after_k_promoting_verdicts() -> None:
    steps = _chain([Verdict.NON_INFERIOR] * DEFAULT_SHADOW_TO_CANARY_K)
    proj = project(steps)
    assert proj.final_stage is Stage.CANARY


def test_full_ladder_shadow_canary_default() -> None:
    n = DEFAULT_SHADOW_TO_CANARY_K + DEFAULT_CANARY_TO_DEFAULT_M
    steps = _chain([Verdict.SIGNIFICANT_IMPROVEMENT] * n)
    proj = project(steps)
    assert proj.final_stage is Stage.DEFAULT
    assert proj.default_config_id == "cfg-candidate"


def test_insufficient_evidence_breaks_the_streak() -> None:
    # A non-promoting verdict interrupts the consecutive run, so the candidate
    # never reaches canary.
    steps = _chain([Verdict.NON_INFERIOR, Verdict.INSUFFICIENT_EVIDENCE, Verdict.NON_INFERIOR])
    proj = project(steps, k=3)
    assert proj.final_stage is Stage.SHADOW


def test_noise_never_promotes() -> None:
    steps = _chain([Verdict.INSUFFICIENT_EVIDENCE] * 10)
    proj = project(steps)
    assert proj.final_stage is Stage.SHADOW
    assert proj.revocations == ()


# ---------------------------------------------------------------------------
# Prefix recomputability (AC3)
# ---------------------------------------------------------------------------


def test_stage_at_every_prefix_is_recomputable() -> None:
    verdicts = [
        Verdict.SIGNIFICANT_IMPROVEMENT,
        Verdict.SIGNIFICANT_IMPROVEMENT,
        Verdict.NON_INFERIOR,
        Verdict.SIGNIFICANT_IMPROVEMENT,
        Verdict.SIGNIFICANT_REGRESSION,
        Verdict.SIGNIFICANT_IMPROVEMENT,
    ]
    steps = _chain(verdicts)
    full = project(steps)
    history = replay_history(steps)
    assert len(history) == len(steps)
    # Each prefix folded independently reproduces the same stage at that point.
    for j in range(1, len(steps) + 1):
        prefix = project(steps[:j])
        assert prefix.final_stage is history[j - 1]
        assert prefix.final_stage is full.stage_at_prefix[j - 1]


# ---------------------------------------------------------------------------
# Rollback / revocation (AC5)
# ---------------------------------------------------------------------------


def test_regression_at_canary_rolls_back_to_prior_default() -> None:
    k = DEFAULT_SHADOW_TO_CANARY_K
    steps = _chain([Verdict.SIGNIFICANT_IMPROVEMENT] * k + [Verdict.SIGNIFICANT_REGRESSION])
    proj = project(steps)
    assert proj.final_stage is Stage.ROLLED_BACK
    # The projection reverts to the prior default (the incumbent baseline).
    assert proj.default_config_id == "cfg-baseline"
    assert len(proj.revocations) == 1
    revocation = proj.revocations[0]
    # It names the promoting receipts that lifted the candidate off shadow.
    assert set(revocation.revoked_receipt_hashes) == {s.receipt_hash for s in steps[:k]}
    assert revocation.reverts_to_config_id == "cfg-baseline"
    assert revocation.trigger_receipt_hash == steps[-1].receipt_hash


def test_regression_at_shadow_is_a_noop() -> None:
    steps = _chain([Verdict.SIGNIFICANT_REGRESSION])
    proj = project(steps)
    assert proj.final_stage is Stage.SHADOW
    assert proj.revocations == ()


def test_projection_is_pure() -> None:
    steps = _chain([Verdict.SIGNIFICANT_IMPROVEMENT] * 4 + [Verdict.SIGNIFICANT_REGRESSION])
    assert project(steps) == project(steps)


# ---------------------------------------------------------------------------
# Signed revocation receipt (AC5 linkage)
# ---------------------------------------------------------------------------


def test_revocation_receipt_seals_and_verifies(tmp_path: Path) -> None:
    k = DEFAULT_SHADOW_TO_CANARY_K
    steps = _chain([Verdict.SIGNIFICANT_IMPROVEMENT] * k + [Verdict.SIGNIFICANT_REGRESSION])
    proj = project(steps)
    revocation = proj.revocations[0]
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    receipt = build_revocation_receipt(
        revocation=revocation,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=1_700_000_000,
        chain=chain,
    )
    result = verify_revocation_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt.receipt_hash,
    )
    assert result.ok, result.reason
    events = chain.query(event_type=EVENT_EVAL_GATE_REVOCATION)
    assert len(events) == 1
    assert set(events[0].details["revoked_receipt_hashes"]) == set(revocation.revoked_receipt_hashes)
    assert events[0].details["trigger_receipt_hash"] == revocation.trigger_receipt_hash
    ok, errors = chain.verify()
    assert ok, errors


def test_tampered_revocation_receipt_fails(tmp_path: Path) -> None:
    import json

    from bernstein.eval.promotion import revocation_receipt_path

    k = DEFAULT_SHADOW_TO_CANARY_K
    steps = _chain([Verdict.SIGNIFICANT_IMPROVEMENT] * k + [Verdict.SIGNIFICANT_REGRESSION])
    revocation = project(steps).revocations[0]
    receipt = build_revocation_receipt(
        revocation=revocation,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=1_700_000_000,
    )
    path = revocation_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["reverts_to_stage"] = "default"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_revocation_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt.receipt_hash,
    )
    assert not result.ok
