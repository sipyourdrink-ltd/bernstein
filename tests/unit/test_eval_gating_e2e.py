"""End-to-end statistical eval gating through EvalGate.evaluate_paired (#2520).

Drives the whole path: a noisy result does not promote, a significant result
promotes deterministically, and a regression triggers a signed revocation whose
linkage the audit chain confirms. Also exercises the baseline ratchet gate and
the benchmark gate's optional significance mode.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.quality.benchmark_gate import BenchmarkGate
from bernstein.core.security.audit_chain import (
    EVENT_EVAL_GATE_REVOCATION,
    EVENT_EVAL_GATE_VERDICT,
    AuditChainStore,
)
from bernstein.eval.baseline import load_baseline, promote_baseline_from_receipt
from bernstein.eval.gate_receipt import build_verdict_receipt, verify_verdict_receipt
from bernstein.eval.promotion import Stage, project, steps_from_receipts, verify_revocation_receipt
from bernstein.eval.significance import Verdict
from bernstein.evolution.gate import EvalGate

_KEY = b"k" * 32


def _outcomes(base_passes: int, cand_passes: int, n: int) -> tuple[dict[str, bool], dict[str, bool]]:
    base = {f"t{i:03d}": (i < base_passes) for i in range(n)}
    cand = {f"t{i:03d}": (i < cand_passes) for i in range(n)}
    return base, cand


def _gate(tmp_path: Path) -> EvalGate:
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir(parents=True, exist_ok=True)
    return EvalGate(eval_harness=None, state_dir=state_dir)


def test_noisy_result_does_not_promote(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY)
    # One extra pass out of 16 tasks: noise.
    base, cand = _outcomes(base_passes=8, cand_passes=9, n=16)
    result = gate.evaluate_paired(
        baseline_outcomes=base,
        candidate_outcomes=cand,
        hmac_key=_KEY,
        timestamp=1_700_000_000,
        audit_chain=chain,
    )
    assert result.accepted is False
    assert result.promoted is False
    assert result.stage == Stage.SHADOW.value
    assert result.verdict == Verdict.INSUFFICIENT_EVIDENCE.value


def test_significant_result_promotes_deterministically(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipts = []
    # Two independent significant verdicts climb shadow -> canary.
    for ts in (1_700_000_000, 1_700_000_100):
        r = build_verdict_receipt(
            baseline_outcomes=base,
            candidate_outcomes=cand,
            candidate_config_id="cfg-candidate",
            baseline_config_id="cfg-baseline",
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            timestamp=ts,
        )
        assert r.verdict == Verdict.SIGNIFICANT_IMPROVEMENT
        receipts.append(r)
    projection = project(steps_from_receipts(receipts))
    assert projection.final_stage is Stage.CANARY
    # Determinism: rebuilding from the same inputs yields the same receipt hash.
    again = build_verdict_receipt(
        baseline_outcomes=base,
        candidate_outcomes=cand,
        candidate_config_id="cfg-candidate",
        baseline_config_id="cfg-baseline",
        workdir=tmp_path / "other",
        lineage_root=tmp_path / "other" / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=1_700_000_000,
    )
    assert again.receipt_hash == receipts[0].receipt_hash


def test_baseline_ratchet_only_on_significant_improvement(tmp_path: Path) -> None:
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir(parents=True)
    # Non-inferior verdict must NOT advance the baseline.
    base_ni, cand_ni = _outcomes(base_passes=10, cand_passes=11, n=16)
    ni_receipt = build_verdict_receipt(
        baseline_outcomes={**base_ni, "t015": False},
        candidate_outcomes={**cand_ni, "t015": True},
        candidate_config_id="c",
        baseline_config_id="b",
        workdir=tmp_path,
        lineage_root=state_dir / "lineage",
        hmac_key=_KEY,
        timestamp=1,
    )
    # Force a non-promoting or non-improvement receipt path.
    if ni_receipt.verdict is Verdict.SIGNIFICANT_IMPROVEMENT:
        # Fall back to an explicitly non-inferior fixture.
        base2, cand2 = _outcomes(base_passes=12, cand_passes=12, n=16)
        cand2["t000"] = False
        cand2["t013"] = True
        ni_receipt = build_verdict_receipt(
            baseline_outcomes=base2,
            candidate_outcomes=cand2,
            candidate_config_id="c",
            baseline_config_id="b",
            workdir=tmp_path,
            lineage_root=state_dir / "lineage",
            hmac_key=_KEY,
            timestamp=2,
        )
    assert ni_receipt.verdict is not Verdict.SIGNIFICANT_IMPROVEMENT
    assert promote_baseline_from_receipt(state_dir, ni_receipt) is False
    assert load_baseline(state_dir) is None

    # A significant improvement advances it.
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    imp = build_verdict_receipt(
        baseline_outcomes=base,
        candidate_outcomes=cand,
        candidate_config_id="c",
        baseline_config_id="b",
        workdir=tmp_path,
        lineage_root=state_dir / "lineage",
        hmac_key=_KEY,
        timestamp=3,
    )
    assert imp.verdict is Verdict.SIGNIFICANT_IMPROVEMENT
    assert promote_baseline_from_receipt(state_dir, imp) is True
    baseline = load_baseline(state_dir)
    assert baseline is not None
    assert baseline.score == imp.evidence.cand_rate


def test_regression_triggers_signed_revocation(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY)
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    reg_base, reg_cand = _outcomes(base_passes=14, cand_passes=4, n=16)

    prior = []
    # Two improvements climb to canary.
    for ts in (1, 2):
        r = build_verdict_receipt(
            baseline_outcomes=base,
            candidate_outcomes=cand,
            candidate_config_id="cfg-candidate",
            baseline_config_id="cfg-baseline",
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            timestamp=ts,
            chain=chain,
        )
        prior.append(r)
    assert project(steps_from_receipts(prior)).final_stage is Stage.CANARY

    # A regression at canary rolls back and emits a signed revocation receipt.
    result = gate.evaluate_paired(
        baseline_outcomes=reg_base,
        candidate_outcomes=reg_cand,
        hmac_key=_KEY,
        timestamp=3,
        candidate_config_id="cfg-candidate",
        baseline_config_id="cfg-baseline",
        prior_receipts=prior,
        audit_chain=chain,
    )
    assert result.verdict == Verdict.SIGNIFICANT_REGRESSION.value
    assert result.stage == Stage.ROLLED_BACK.value
    assert result.revocation_receipt_hash is not None

    # The revocation receipt verifies offline and the chain confirms the linkage.
    rev = verify_revocation_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=result.revocation_receipt_hash,
    )
    assert rev.ok, rev.reason
    assert rev.receipt is not None
    assert set(rev.receipt.revoked_receipt_hashes) == {r.receipt_hash for r in prior}

    verdict_events = chain.query(event_type=EVENT_EVAL_GATE_VERDICT)
    revocation_events = chain.query(event_type=EVENT_EVAL_GATE_REVOCATION)
    assert len(verdict_events) == 3
    assert len(revocation_events) == 1
    assert revocation_events[0].details["trigger_receipt_hash"] == result.receipt_hash
    ok, errors = chain.verify()
    assert ok, errors

    # Every sealed verdict receipt still verifies offline.
    for r in prior:
        v = verify_verdict_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            receipt_hash=r.receipt_hash,
        )
        assert v.ok, v.reason


def test_benchmark_gate_significance_mode(tmp_path: Path) -> None:
    gate = BenchmarkGate(workdir=tmp_path, run_dir=tmp_path)
    base = {f"b{i:02d}": True for i in range(16)}
    # Candidate regresses on many benchmarks.
    current = {f"b{i:02d}": (i >= 10) for i in range(16)}
    result = gate.evaluate_significance(baseline_outcomes=base, current_outcomes=current)
    assert result.verdict is Verdict.SIGNIFICANT_REGRESSION  # type: ignore[attr-defined]


def test_doctor_advisory_flags_below_min_n_receipts(tmp_path: Path) -> None:
    from bernstein.cli.commands.doctor_cmd import check_eval_gate_min_n_advisory

    # No receipts yet: advisory passes.
    clean = check_eval_gate_min_n_advisory(tmp_path)
    assert clean["status"] == "PASS"

    # Seal an underpowered (n=4) verdict receipt.
    base, cand = _outcomes(base_passes=0, cand_passes=4, n=4)
    receipt = build_verdict_receipt(
        baseline_outcomes=base,
        candidate_outcomes=cand,
        candidate_config_id="c",
        baseline_config_id="b",
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=1,
    )
    assert receipt.evidence.min_n_satisfied is False
    flagged = check_eval_gate_min_n_advisory(tmp_path)
    assert flagged["status"] == "WARN"
    assert "below the minimum n" in flagged["detail"]
