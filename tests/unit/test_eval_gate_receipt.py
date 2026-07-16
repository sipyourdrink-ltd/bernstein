"""Tests for the signed verdict receipt behind statistical eval gating (#2520).

A gate verdict is a receipt that carries its statistical evidence and is
anchored to the lineage spine and mirrored into the HMAC audit chain. Offline
verification re-derives the verdict from the stored evidence, so a receipt
whose evidence does not entail its verdict is rejected even when its hashes are
internally consistent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_EVAL_GATE_VERDICT,
    AuditChainStore,
)
from bernstein.eval.gate_receipt import (
    build_verdict_receipt,
    read_verdict_receipt,
    recompute_receipt_hash,
    verdict_receipt_path,
    verify_verdict_receipt,
)
from bernstein.eval.significance import Verdict

_KEY = b"k" * 32


def _outcomes(base_passes: int, cand_passes: int, n: int) -> tuple[dict[str, bool], dict[str, bool]]:
    base = {f"t{i:03d}": (i < base_passes) for i in range(n)}
    cand = {f"t{i:03d}": (i < cand_passes) for i in range(n)}
    return base, cand


def _build(
    tmp_path: Path,
    base: dict[str, bool],
    cand: dict[str, bool],
    *,
    chain: AuditChainStore | None = None,
    candidate_config_id: str = "cfg-candidate",
):
    return build_verdict_receipt(
        baseline_outcomes=base,
        candidate_outcomes=cand,
        candidate_config_id=candidate_config_id,
        baseline_config_id="cfg-baseline",
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=1_700_000_000,
        chain=chain,
    )


# ---------------------------------------------------------------------------
# Determinism (AC1)
# ---------------------------------------------------------------------------


def test_same_result_sets_any_order_produce_identical_receipt(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    machine_a = _build(tmp_path / "a", base, cand)
    # Reverse ingestion order on a second, independent workdir.
    base_rev = dict(reversed(list(base.items())))
    cand_rev = dict(reversed(list(cand.items())))
    machine_b = _build(tmp_path / "b", base_rev, cand_rev)
    assert machine_a.receipt_hash == machine_b.receipt_hash
    assert machine_a.canonical_payload_without_anchor() == machine_b.canonical_payload_without_anchor()
    assert machine_a.verdict == Verdict.SIGNIFICANT_IMPROVEMENT


# ---------------------------------------------------------------------------
# Offline verification + tamper matrix (AC2)
# ---------------------------------------------------------------------------


def test_receipt_verifies_offline(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt.receipt_hash,
    )
    assert result.ok, result.reason


@pytest.mark.parametrize(
    ("mutate_path", "new_value"),
    [
        (("evidence", "n_candidate"), 99),
        (("evidence", "interval_low"), -0.9),
        (("evidence", "effect"), 0.99),
        (("evidence", "verdict"), "significant_regression"),
        (("baseline_result_set_hash",), "sha256:" + "0" * 64),
        (("candidate_result_set_hash",), "sha256:" + "0" * 64),
    ],
)
def test_tampering_any_field_breaks_verification(
    tmp_path: Path, mutate_path: tuple[str, ...], new_value: object
) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    path = verdict_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    target = payload
    for key in mutate_path[:-1]:
        target = target[key]
    target[mutate_path[-1]] = new_value
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt.receipt_hash,
    )
    assert not result.ok


def test_internally_consistent_forged_verdict_is_rejected(tmp_path: Path) -> None:
    # Flip the verdict AND recompute the receipt hash so the receipt is
    # internally consistent; re-derivation from the evidence must still reject.
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    path = verdict_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence"]["verdict"] = "significant_regression"
    payload["receipt_hash"] = recompute_receipt_hash(payload)
    # Re-key the on-disk file to the forged hash so the reader finds it.
    forged_path = verdict_receipt_path(tmp_path, payload["receipt_hash"])
    forged_path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=payload["receipt_hash"],
    )
    assert not result.ok
    assert "entail" in result.reason or "verdict" in result.reason


def test_internally_consistent_forged_effect_is_rejected(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    receipt = _build(tmp_path, base, cand)
    path = verdict_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence"]["effect"] = 0.99
    payload["receipt_hash"] = recompute_receipt_hash(payload)
    forged_path = verdict_receipt_path(tmp_path, payload["receipt_hash"])
    forged_path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_verdict_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=payload["receipt_hash"],
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# Audit chain mirroring
# ---------------------------------------------------------------------------


def test_receipt_mirrors_into_audit_chain(tmp_path: Path) -> None:
    base, cand = _outcomes(base_passes=4, cand_passes=12, n=16)
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    receipt = _build(tmp_path, base, cand, chain=chain)
    events = chain.query(event_type=EVENT_EVAL_GATE_VERDICT)
    assert len(events) == 1
    assert events[0].resource_id == receipt.receipt_hash
    assert events[0].details["verdict"] == Verdict.SIGNIFICANT_IMPROVEMENT.value
    ok, errors = chain.verify()
    assert ok, errors


def test_read_missing_receipt_returns_none(tmp_path: Path) -> None:
    assert read_verdict_receipt(tmp_path, "sha256:" + "a" * 64) is None
