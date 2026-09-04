"""Tests for change_contract_replay and ChangeContract/ReplayVerdict types.

Covers:
    - ThinCorpusError / ReceiptMismatch exceptions
    - PREDICATE_REGISTRY / register_invariant
    - contract_canonical_bytes / contract_fingerprint / _contract_from_canonical roundtrip
    - select_corpus with mock read_sealed_journal_head
    - _replay_one_run verdict logic for each ReplayVerdict case
    - replay_contract aggregation
    - _receipt_body / _receipt_hash
    - write_verdict_receipt file output
    - verify_verdict_receipt happy-path and mismatch paths
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import bernstein.evolution.change_contract_replay as replay_mod
from bernstein.core.security.governance import (
    GovernanceDecision,
    decisions_dir,
)
from bernstein.evolution.change_contract_replay import (
    PREDICATE_REGISTRY,
    ReceiptMismatch,
    ThinCorpusError,
    _contract_from_canonical,
    _receipt_body,
    _receipt_hash,
    contract_canonical_bytes,
    contract_fingerprint,
    register_invariant,
    replay_contract,
    select_corpus,
    verify_verdict_receipt,
    write_verdict_receipt,
)
from bernstein.evolution.types import (
    ChangeContract,
    ContractInvariant,
    PredictedDecisionChange,
    ReplayServiceResult,
    ReplayVerdict,
    RunVerdict,
)


# ------------------------------------------------------------------
# Test helpers
# ------------------------------------------------------------------


def _patch_head(mapping: dict[str, str]) -> Any:
    """Return a context manager that patches ``read_sealed_journal_head`` in
    the replay module's namespace to a function returning values from mapping
    keyed on run_id.  Returns None for unknown run_ids.
    """
    from unittest.mock import patch

    def mock_head(run_id: str, sdd_dir: Path | str) -> str | None:
        return mapping.get(run_id)

    return patch.dict(replay_mod.__dict__, {"read_sealed_journal_head": mock_head})


def _patch_head_with(fn: Any) -> Any:
    from unittest.mock import patch

    return patch.dict(replay_mod.__dict__, {"read_sealed_journal_head": fn})


# ------------------------------------------------------------------
# ThinCorpusError
# ------------------------------------------------------------------


def test_thin_corpus_error_str() -> None:
    err = ThinCorpusError(found=2, required=5, fingerprint="sha256:abc123")
    assert "found 2" in str(err)
    assert "required 5" in str(err)
    assert "abc123" in str(err)
    assert err.found == 2
    assert err.required == 5
    assert err.fingerprint == "sha256:abc123"


# ------------------------------------------------------------------
# ReceiptMismatch
# ------------------------------------------------------------------


def test_receipt_mismatch_str(tmp_path: Path) -> None:
    p = tmp_path / "receipt.json"
    err = ReceiptMismatch(receipt_path=p, detail="hash mismatch")
    assert str(p) in str(err)
    assert "hash mismatch" in str(err)
    assert err.receipt_path == p
    assert err.detail == "hash mismatch"


# ------------------------------------------------------------------
# PREDICATE_REGISTRY / register_invariant
# ------------------------------------------------------------------


def test_register_invariant_inserts_into_registry() -> None:
    PREDICATE_REGISTRY.clear()
    predicate_hash = "abc123def456"
    mock_pred = MagicMock(return_value=True)
    register_invariant(predicate_hash, mock_pred)
    assert PREDICATE_REGISTRY[predicate_hash] is mock_pred


def test_register_invariant_overwrites_existing() -> None:
    PREDICATE_REGISTRY.clear()
    h = "overwrite_hash"
    pred1 = MagicMock(return_value=True)
    pred2 = MagicMock(return_value=False)
    register_invariant(h, pred1)
    register_invariant(h, pred2)
    assert PREDICATE_REGISTRY[h] is pred2


# ------------------------------------------------------------------
# contract_canonical_bytes / contract_fingerprint / _contract_from_canonical
# ------------------------------------------------------------------


def test_contract_canonical_bytes_deterministic() -> None:
    contract = ChangeContract(
        target_fingerprint="sha256:deadbeef",
        predicted_changes=(
            PredictedDecisionChange(
                subject="agent-1",
                action="allow",
                expected_verdict="allow",
            ),
        ),
        invariants=(
            ContractInvariant(
                name="no-deny",
                predicate_hash="abc123",
            ),
        ),
        min_corpus_size=3,
    )
    cb1 = contract_canonical_bytes(contract)
    cb2 = contract_canonical_bytes(contract)
    assert cb1 == cb2


def test_contract_fingerprint_stable_hex() -> None:
    contract = ChangeContract(
        target_fingerprint="sha256:deadbeef",
        predicted_changes=(),
        invariants=(),
        min_corpus_size=5,
    )
    fp = contract_fingerprint(contract)
    assert len(fp) == 64  # sha256 hex
    assert fp == contract_fingerprint(contract)  # stable


def test_contract_fingerprint_differs_for_different_contracts() -> None:
    c1 = ChangeContract(
        target_fingerprint="sha256:aaaa",
        predicted_changes=(),
        invariants=(),
    )
    c2 = ChangeContract(
        target_fingerprint="sha256:bbbb",
        predicted_changes=(),
        invariants=(),
    )
    assert contract_fingerprint(c1) != contract_fingerprint(c2)


def test_contract_from_canonical_roundtrip() -> None:
    original = ChangeContract(
        target_fingerprint="sha256:c0ffee",
        predicted_changes=(
            PredictedDecisionChange(
                subject="role:backend",
                action="budget",
                expected_verdict="allow",
            ),
            PredictedDecisionChange(
                subject="role:frontend",
                action="budget",
                expected_verdict="deny",
            ),
        ),
        invariants=(
            ContractInvariant(
                name="always-has-run-id",
                predicate_hash="hash42",
            ),
        ),
        min_corpus_size=7,
    )
    canonical = json.loads(contract_canonical_bytes(original))
    rebuilt = _contract_from_canonical(canonical)
    assert rebuilt.target_fingerprint == original.target_fingerprint
    assert rebuilt.predicted_changes == original.predicted_changes
    assert rebuilt.invariants == original.invariants
    assert rebuilt.min_corpus_size == original.min_corpus_size


def test_contract_from_canonical_missing_fields_defaults() -> None:
    raw: dict[str, object] = {"target_fingerprint": "sha256:base"}
    rebuilt = _contract_from_canonical(raw)
    assert rebuilt.target_fingerprint == "sha256:base"
    assert rebuilt.predicted_changes == ()
    assert rebuilt.invariants == ()
    assert rebuilt.min_corpus_size == 5  # default


# ------------------------------------------------------------------
# select_corpus
# ------------------------------------------------------------------


def test_select_corpus_happy_path(tmp_path: Path) -> None:
    """select_corpus finds runs matching fingerprint prefix, sorted ascending."""
    lineage = tmp_path / "lineage"
    lineage.mkdir()

    # Create three run dirs
    for run_id in ["run-a", "run-b", "run-c"]:
        (lineage / run_id).mkdir()

    # Heads whose first 8 hex chars differ so we can verify ordering.
    # All three start with "deadbeef" (the target prefix); later chars
    # determine the sort.
    mapping = {
        "run-a": "sha256:deadbeef00000000000000000000000000",
        "run-b": "sha256:deadbeef11111111111111111111111111",
        "run-c": "sha256:cafebabe00000000000000000000000000",
    }
    with _patch_head(mapping):
        result = select_corpus(
            sdd_dir=tmp_path,
            target_fingerprint="deadbeef",
            n=2,
        )

    assert len(result) == 2
    assert result == ["run-a", "run-b"]  # sorted ascending by hash


def test_select_corpus_prefix_stripping(tmp_path: Path) -> None:
    """sha256: prefix is stripped before matching."""
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    (lineage / "run-x").mkdir()

    mapping = {"run-x": "sha256:abcdef1200000000000000000000000000"}
    with _patch_head(mapping):
        result = select_corpus(
            sdd_dir=tmp_path,
            target_fingerprint="sha256:abcdef12",  # with prefix
            n=1,
        )
    assert result == ["run-x"]


def test_select_corpus_not_enough_runs_raises(tmp_path: Path) -> None:
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    (lineage / "run-only-one").mkdir()

    mapping = {"run-only-one": "sha256:abcdef1200000000000000000000000000"}
    with _patch_head(mapping):
        with pytest.raises(ThinCorpusError) as exc_info:
            select_corpus(sdd_dir=tmp_path, target_fingerprint="abcdef12", n=3)
        assert exc_info.value.found == 1
        assert exc_info.value.required == 3


def test_select_corpus_no_lineage_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ThinCorpusError) as exc_info:
        select_corpus(sdd_dir=tmp_path, target_fingerprint="deadbeef", n=1)
    assert exc_info.value.found == 0


def test_select_corpus_skips_unsealed_runs(tmp_path: Path) -> None:
    """Runs where read_sealed_journal_head returns None are silently skipped."""
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    for run_id in ["sealed-run", "unsealed-run", "another-sealed"]:
        (lineage / run_id).mkdir()

    mapping = {
        "sealed-run": "sha256:abcdef12ffffffffffffffffffffffff",
        "another-sealed": "sha256:abcdef1200000000000000000000000000",
    }
    with _patch_head(mapping):
        result = select_corpus(
            sdd_dir=tmp_path, target_fingerprint="abcdef12", n=2
        )
    assert result == ["another-sealed", "sealed-run"]


# ------------------------------------------------------------------
# _replay_one_run — verdict logic per case
# ------------------------------------------------------------------


def _make_decision(
    subject: str,
    action: str,
    verdict: str,
) -> GovernanceDecision:
    return GovernanceDecision(
        run_id="run-1",
        subject=subject,
        action=action,
        verdict=verdict,
        inputs_hash="sha256:abc123",
        timestamp=1000,
    )


def _run_verdict(
    decisions: list[GovernanceDecision],
    contract: ChangeContract,
    run_id: str = "run-1",
) -> RunVerdict:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        lineage = Path(tmp)
        out_dir = decisions_dir(lineage, run_id)
        out_dir.mkdir(parents=True)
        for i, dec in enumerate(decisions):
            (out_dir / f"{i:06d}-dec.json").write_text(json.dumps(dec.to_dict()))
        return replay_mod._replay_one_run(
            run_id=run_id, lineage_root=lineage, contract=contract
        )


def test_verdict_unchanged_no_decisions() -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(),
        invariants=(),
    )
    rv = _run_verdict([], contract)
    assert rv.verdict == ReplayVerdict.UNCHANGED


def test_verdict_changed_as_predicted() -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(
            PredictedDecisionChange(
                subject="role:backend", action="budget", expected_verdict="allow"
            ),
        ),
        invariants=(),
    )
    decisions = [
        _make_decision("role:backend", "budget", "allow"),
    ]
    rv = _run_verdict(decisions, contract)
    assert rv.verdict == ReplayVerdict.CHANGED_AS_PREDICTED


def test_verdict_changed_unexpectedly_unpredicted_decision() -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(),  # no predictions
        invariants=(),
    )
    decisions = [
        _make_decision("role:backend", "budget", "allow"),
    ]
    rv = _run_verdict(decisions, contract)
    assert rv.verdict == ReplayVerdict.CHANGED_UNEXPECTEDLY


def test_verdict_changed_unexpectedly_wrong_verdict() -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(
            PredictedDecisionChange(
                subject="role:backend", action="budget", expected_verdict="allow"
            ),
        ),
        invariants=(),
    )
    decisions = [
        _make_decision("role:backend", "budget", "deny"),  # wrong verdict
    ]
    rv = _run_verdict(decisions, contract)
    assert rv.verdict == ReplayVerdict.CHANGED_UNEXPECTEDLY
    assert rv.changed_subjects == ["role:backend"]


def test_verdict_changed_unexpectedly_predicted_missing() -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(
            PredictedDecisionChange(
                subject="role:backend", action="budget", expected_verdict="allow"
            ),
        ),
        invariants=(),
    )
    decisions = []  # predicted decision missing
    rv = _run_verdict(decisions, contract)
    assert rv.verdict == ReplayVerdict.CHANGED_UNEXPECTEDLY


def test_verdict_invariant_violated() -> None:
    PREDICATE_REGISTRY.clear()
    pred_hash = "invariant-hash-violated"
    PREDICATE_REGISTRY[pred_hash] = lambda decisions: False  # fails
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(),
        invariants=(
            ContractInvariant(name="always-true", predicate_hash=pred_hash),
        ),
    )
    decisions = [_make_decision("role:backend", "budget", "allow")]
    rv = _run_verdict(decisions, contract)
    assert rv.verdict == ReplayVerdict.INVARIANT_VIOLATED
    assert "always-true" in rv.violated_invariants


def test_verdict_inconclusive_unregistered_predicate() -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(),
        invariants=(
            ContractInvariant(
                name="unknown-invariant", predicate_hash="not-registered-hash"
            ),
        ),
    )
    decisions = []
    rv = _run_verdict(decisions, contract)
    # inconclusive does not dominate — check details
    assert "not registered" in rv.details


def test_verdict_inconclusive_predicate_raises() -> None:
    PREDICATE_REGISTRY.clear()
    pred_hash = "raises-hash"
    PREDICATE_REGISTRY[pred_hash] = lambda decisions: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(),
        invariants=(
            ContractInvariant(name="boom-invariant", predicate_hash=pred_hash),
        ),
    )
    decisions: list[GovernanceDecision] = []
    rv = _run_verdict(decisions, contract)
    assert "inconclusive" in rv.details.lower()


# ------------------------------------------------------------------
# replay_contract aggregation
# ------------------------------------------------------------------


def test_replay_contract_aggregates_invariant_violated(tmp_path: Path) -> None:
    """Any INVARIANT_VIOLATED run makes the aggregate INVARIANT_VIOLATED."""
    PREDICATE_REGISTRY.clear()
    pred_hash = "agg-violated-hash"
    PREDICATE_REGISTRY[pred_hash] = lambda decisions: False

    contract = ChangeContract(
        target_fingerprint="sha256:abcdef12",
        predicted_changes=(
            PredictedDecisionChange(
                subject="role:backend", action="budget", expected_verdict="allow"
            ),
        ),
        invariants=(ContractInvariant(name="fail", predicate_hash=pred_hash),),
        min_corpus_size=1,
    )

    (tmp_path / "lineage").mkdir()
    (tmp_path / "lineage" / "run-1").mkdir()

    with _patch_head({"run-1": "sha256:abcdef1200000000000000000000000000"}):
        result = replay_contract(sdd_dir=tmp_path, contract=contract)
    assert result.verdict == ReplayVerdict.INVARIANT_VIOLATED
    assert result.thin_corpus is False


def test_replay_contract_aggregates_changed_unexpectedly(tmp_path: Path) -> None:
    """CHANGED_UNEXPECTEDLY dominates over CHANGED_AS_PREDICTED."""
    PREDICATE_REGISTRY.clear()

    contract = ChangeContract(
        target_fingerprint="sha256:abcdef12",
        predicted_changes=(
            PredictedDecisionChange(
                subject="role:backend", action="budget", expected_verdict="allow"
            ),
        ),
        invariants=(),
        min_corpus_size=1,
    )

    (tmp_path / "lineage").mkdir()
    (tmp_path / "lineage" / "run-1").mkdir()

    with _patch_head({"run-1": "sha256:abcdef1200000000000000000000000000"}):
        result = replay_contract(sdd_dir=tmp_path, contract=contract)
    # run has no decisions — predicted missing → CHANGED_UNEXPECTEDLY
    assert result.verdict == ReplayVerdict.CHANGED_UNEXPECTEDLY


def test_replay_contract_all_changed_as_predicted(tmp_path: Path) -> None:
    """All CHANGED_AS_PREDICTED → CHANGED_AS_PREDICTED aggregate."""
    PREDICATE_REGISTRY.clear()

    contract = ChangeContract(
        target_fingerprint="sha256:abcdef12",
        predicted_changes=(
            PredictedDecisionChange(
                subject="role:backend", action="budget", expected_verdict="allow"
            ),
        ),
        invariants=(),
        min_corpus_size=1,
    )

    lineage = tmp_path / "lineage"
    lineage.mkdir()
    run_dir = lineage / "run-1"
    run_dir.mkdir()
    dec_dir = decisions_dir(lineage, "run-1")
    dec_dir.mkdir(parents=True)
    dec = _make_decision("role:backend", "budget", "allow")
    # Filename pattern: {seq:06d}-{safe_subject}-{hash_frag}.json
    (dec_dir / "000000-role_backend-abc123.json").write_text(
        json.dumps(dec.to_dict())
    )

    with _patch_head({"run-1": "sha256:abcdef1200000000000000000000000000"}):
        result = replay_contract(sdd_dir=tmp_path, contract=contract)
    assert result.verdict == ReplayVerdict.CHANGED_AS_PREDICTED


def test_replay_contract_unchanged(tmp_path: Path) -> None:
    """No decisions and no predictions → UNCHANGED."""
    PREDICATE_REGISTRY.clear()

    contract = ChangeContract(
        target_fingerprint="sha256:abcdef12",
        predicted_changes=(),
        invariants=(),
        min_corpus_size=1,
    )

    (tmp_path / "lineage").mkdir()
    (tmp_path / "lineage" / "run-1").mkdir()

    with _patch_head({"run-1": "sha256:abcdef1200000000000000000000000000"}):
        result = replay_contract(sdd_dir=tmp_path, contract=contract)
    assert result.verdict == ReplayVerdict.UNCHANGED


# ------------------------------------------------------------------
# _receipt_body / _receipt_hash
# ------------------------------------------------------------------


def test_receipt_body_contains_expected_keys() -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(),
        invariants=(),
    )
    result = ReplayServiceResult(
        verdict=ReplayVerdict.UNCHANGED,
        contract_fingerprint=contract_fingerprint(contract),
        selected_run_ids=["run-1"],
        run_verdicts=[
            RunVerdict(
                run_id="run-1",
                verdict=ReplayVerdict.UNCHANGED,
                changed_subjects=[],
                violated_invariants=[],
                details="ok",
            )
        ],
        thin_corpus=False,
    )
    body = _receipt_body(result, contract)
    assert "contract_fingerprint" in body
    assert "selected_run_ids" in body
    assert "run_verdicts" in body
    assert "contract_canonical" in body


def test_receipt_hash_stable_and_hex() -> None:
    body = {"key": "value", "number": 42}
    h1 = _receipt_hash(body)
    h2 = _receipt_hash(body)
    assert h1 == h2
    assert len(h1) == 64


def test_receipt_hash_differs_for_different_body() -> None:
    h1 = _receipt_hash({"a": 1})
    h2 = _receipt_hash({"a": 2})
    assert h1 != h2


# ------------------------------------------------------------------
# write_verdict_receipt
# ------------------------------------------------------------------


def test_write_verdict_receipt_writes_valid_json(tmp_path: Path) -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:x",
        predicted_changes=(),
        invariants=(),
    )
    result = ReplayServiceResult(
        verdict=ReplayVerdict.UNCHANGED,
        contract_fingerprint=contract_fingerprint(contract),
        selected_run_ids=["run-1"],
        run_verdicts=[
            RunVerdict(
                run_id="run-1",
                verdict=ReplayVerdict.UNCHANGED,
                changed_subjects=[],
                violated_invariants=[],
                details="ok",
            )
        ],
        thin_corpus=False,
    )
    out = tmp_path / "receipt.json"
    write_verdict_receipt(result=result, contract=contract, out_path=out)
    assert out.exists()
    stored = json.loads(out.read_text(encoding="utf-8"))
    assert "service_receipt_hash" in stored
    assert len(stored["service_receipt_hash"]) == 64


def test_write_verdict_receipt_roundtrips_through_verify(tmp_path: Path) -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:abcdef12",
        predicted_changes=(),
        invariants=(),
        min_corpus_size=1,
    )
    result = ReplayServiceResult(
        verdict=ReplayVerdict.UNCHANGED,
        contract_fingerprint=contract_fingerprint(contract),
        selected_run_ids=["run-1"],
        run_verdicts=[
            RunVerdict(
                run_id="run-1",
                verdict=ReplayVerdict.UNCHANGED,
                changed_subjects=[],
                violated_invariants=[],
                details="ok",
            )
        ],
        thin_corpus=False,
    )
    out = tmp_path / "receipt.json"
    write_verdict_receipt(result=result, contract=contract, out_path=out)

    # Seed the lineage so verify can replay. The contract has no
    # predicted_changes, so empty decisions dirs match the receipt's
    # UNCHANGED verdict.
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    run_dir = lineage / "run-1"
    run_dir.mkdir()
    dec_dir = run_dir / "decisions"
    dec_dir.mkdir()

    with _patch_head({"run-1": "sha256:abcdef1200000000000000000000000000"}):
        ok = verify_verdict_receipt(receipt_path=out, lineage_root=lineage)
    assert ok is True


# ------------------------------------------------------------------
# verify_verdict_receipt mismatch paths
# ------------------------------------------------------------------


def test_verify_receipt_invalid_json(tmp_path: Path) -> None:
    receipt = tmp_path / "bad.json"
    receipt.write_text("not json {", encoding="utf-8")
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    with pytest.raises(ReceiptMismatch) as exc_info:
        verify_verdict_receipt(receipt_path=receipt, lineage_root=lineage)
    assert "invalid JSON" in exc_info.value.detail


def test_verify_receipt_missing_contract_canonical(tmp_path: Path) -> None:
    receipt = tmp_path / "missing_canonical.json"
    receipt.write_text(
        json.dumps({"service_receipt_hash": "x" * 64}), encoding="utf-8"
    )
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    with pytest.raises(ReceiptMismatch) as exc_info:
        verify_verdict_receipt(receipt_path=receipt, lineage_root=lineage)
    assert "contract_canonical" in exc_info.value.detail


def test_verify_receipt_bad_hex_raises(tmp_path: Path) -> None:
    receipt = tmp_path / "bad_hex.json"
    receipt.write_text(
        json.dumps({
            "service_receipt_hash": "x" * 64,
            "contract_canonical": "not-hex-xyz",
        }),
        encoding="utf-8",
    )
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    with pytest.raises(ReceiptMismatch) as exc_info:
        verify_verdict_receipt(receipt_path=receipt, lineage_root=lineage)
    assert "not hex" in exc_info.value.detail


def test_verify_receipt_fingerprint_mismatch(tmp_path: Path) -> None:
    # Write a receipt whose stored fingerprint doesn't match recomputed
    contract = ChangeContract(
        target_fingerprint="sha256:abcdef12",
        predicted_changes=(),
        invariants=(),
        min_corpus_size=1,
    )
    # Create the lineage so select_corpus can find it
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    run_dir = lineage / "run-1"
    run_dir.mkdir()
    dec_dir = run_dir / "decisions"
    dec_dir.mkdir()

    result = ReplayServiceResult(
        verdict=ReplayVerdict.UNCHANGED,
        contract_fingerprint="wrong-fingerprint",  # intentional mismatch
        selected_run_ids=["run-1"],
        run_verdicts=[
            RunVerdict(
                run_id="run-1",
                verdict=ReplayVerdict.UNCHANGED,
                changed_subjects=[],
                violated_invariants=[],
                details="ok",
            )
        ],
        thin_corpus=False,
    )
    out = tmp_path / "receipt.json"
    write_verdict_receipt(result=result, contract=contract, out_path=out)

    with _patch_head({"run-1": "sha256:abcdef1200000000000000000000000000"}):
        with pytest.raises(ReceiptMismatch) as exc_info:
            verify_verdict_receipt(receipt_path=out, lineage_root=lineage)
    assert "fingerprint mismatch" in exc_info.value.detail


def test_verify_receipt_selected_run_ids_mismatch(tmp_path: Path) -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:abcdef12",
        predicted_changes=(),
        invariants=(),
        min_corpus_size=1,
    )
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    run_dir = lineage / "run-1"
    run_dir.mkdir()
    dec_dir = run_dir / "decisions"
    dec_dir.mkdir()

    result = ReplayServiceResult(
        verdict=ReplayVerdict.UNCHANGED,
        contract_fingerprint=contract_fingerprint(contract),
        selected_run_ids=["run-different"],  # mismatch
        run_verdicts=[
            RunVerdict(
                run_id="run-different",
                verdict=ReplayVerdict.UNCHANGED,
                changed_subjects=[],
                violated_invariants=[],
                details="ok",
            )
        ],
        thin_corpus=False,
    )
    out = tmp_path / "receipt.json"
    write_verdict_receipt(result=result, contract=contract, out_path=out)

    with _patch_head({"run-1": "sha256:abcdef1200000000000000000000000000"}):
        with pytest.raises(ReceiptMismatch) as exc_info:
            verify_verdict_receipt(receipt_path=out, lineage_root=lineage)
    assert "selected_run_ids mismatch" in exc_info.value.detail


def test_verify_receipt_service_receipt_hash_tampered(tmp_path: Path) -> None:
    PREDICATE_REGISTRY.clear()
    contract = ChangeContract(
        target_fingerprint="sha256:abcdef12",
        predicted_changes=(),
        invariants=(),
        min_corpus_size=1,
    )
    result = ReplayServiceResult(
        verdict=ReplayVerdict.UNCHANGED,
        contract_fingerprint=contract_fingerprint(contract),
        selected_run_ids=["run-1"],
        run_verdicts=[
            RunVerdict(
                run_id="run-1",
                verdict=ReplayVerdict.UNCHANGED,
                changed_subjects=[],
                violated_invariants=[],
                details="ok",
            )
        ],
        thin_corpus=False,
    )
    out = tmp_path / "receipt.json"
    write_verdict_receipt(result=result, contract=contract, out_path=out)

    # Tamper with the receipt
    stored = json.loads(out.read_text(encoding="utf-8"))
    stored["service_receipt_hash"] = "a" * 64
    out.write_text(json.dumps(stored, indent=2), encoding="utf-8")

    lineage = tmp_path / "lineage"
    lineage.mkdir()
    run_dir = lineage / "run-1"
    run_dir.mkdir()
    dec_dir = run_dir / "decisions"
    dec_dir.mkdir()

    with _patch_head({"run-1": "sha256:abcdef1200000000000000000000000000"}):
        with pytest.raises(ReceiptMismatch) as exc_info:
            verify_verdict_receipt(receipt_path=out, lineage_root=lineage)
    assert "service_receipt_hash mismatch" in exc_info.value.detail
