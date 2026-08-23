"""``bernstein audit verify`` and ``bernstein benchmark receipt verify`` cover
trajectory receipts (#2925 AC3, AC6, AC7).

Acceptance criteria under test:

AC3  A tampered published score, contaminated suite, fabricated scalar, or
     cherry-picked candidate fails *both* ``bernstein benchmark receipt verify``
     (via ``verify_trajectory_receipt``) and ``bernstein audit verify``
     (via ``_verify_trajectory_receipts``), with the exact item named.

AC6  **Strip-the-substrate:** removing the journal head or fixture hash from a
     task anchor means the receipt hash no longer recomputes correctly, so the
     attacker must re-seal.  After re-sealing, the spine anchor is missing, so
     verification fails closed — not a warning, a hard False.  The score has
     no meaning without the trajectory that proves it.

AC7  ``bernstein audit verify`` treats an *absence* of trajectory receipts as
     a silent no-op (returns True, no output change) but hard-fails (returns
     False) on any present-and-tampered receipt.

All tests are hermetic: separate tmp dirs, no live providers, no wall-clock.
The audit key is pinned to ``b"k" * 32`` via monkeypatch; ``AUDIT_DIR`` is
redirected to a temp path so the CWD-relative workdir logic in
``_verify_trajectory_receipts`` resolves under the temp tree.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bernstein.cli.commands import audit_cmd
from bernstein.eval.metrics import EvalScoreComponents, TierScores
from bernstein.eval.trajectory_receipt import (
    BestOfNProvenance,
    TaskTrajectoryAnchor,
    TrajectoryVerifyResult,
    build_trajectory_receipt,
    trajectory_receipt_path,
    verify_trajectory_receipt,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KEY = b"k" * 32
_FAKE_JOURNAL_HEAD = "sha256:" + "a" * 64
_FAKE_EVENTS_HASH = "sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_audit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the install audit key so no filesystem side-effects occur."""
    monkeypatch.setattr(
        "bernstein.core.security.audit.load_audit_key",
        lambda *a, **kw: _KEY,
    )


def _anchor(
    task_id: str,
    *,
    task_success: float = 1.0,
    journal_head: str = _FAKE_JOURNAL_HEAD,
) -> TaskTrajectoryAnchor:
    return TaskTrajectoryAnchor(
        task_id=task_id,
        journal_head_hash=journal_head,
        events_content_hash=_FAKE_EVENTS_HASH,
        model_id="claude-test",
        config_fingerprint="cfg-v1",
        components=EvalScoreComponents(
            task_success=task_success,
            code_quality=0.9,
            efficiency=0.8,
            reliability=1.0,
            safety=1.0,
        ),
    )


def _per_tier() -> TierScores:
    return TierScores(smoke=1.0, standard=0.0, stretch=0.0, adversarial=0.0)


def _build(workdir: Path, anchors: list[TaskTrajectoryAnchor] | None = None, **kwargs) -> str:
    """Build a receipt and return its hash."""
    receipt = build_trajectory_receipt(
        run_id=kwargs.pop("run_id", "run-test-001"),
        task_anchors=anchors
        if anchors is not None
        else [
            _anchor("smoke-001", journal_head="sha256:" + "1" * 64),
            _anchor("smoke-002", journal_head="sha256:" + "2" * 64),
        ],
        per_tier=_per_tier(),
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        **kwargs,
    )
    return receipt.receipt_hash


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_payload(workdir: Path, receipt_hash: str) -> dict:
    return json.loads(trajectory_receipt_path(workdir, receipt_hash).read_text(encoding="utf-8"))


def _reseal(workdir: Path, payload: dict) -> str:
    """Re-hash a tampered payload and store it under its new content address.

    Models the strongest attacker: one who edits the receipt and recomputes
    ``receipt_hash`` so the hash-recompute step cannot catch them, but cannot
    fake the spine anchor.
    """
    body = {k: v for k, v in payload.items() if k not in ("receipt_hash", "journal_entry_hash")}
    new_hash = "sha256:" + hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    payload["receipt_hash"] = new_hash
    trajectory_receipt_path(workdir, new_hash).write_text(_canonical(payload), encoding="utf-8")
    return new_hash


def _verify(workdir: Path, receipt_hash: str) -> TrajectoryVerifyResult:
    return verify_trajectory_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt_hash,
    )


def _audit_verify_receipts(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Call _verify_trajectory_receipts() with workdir as CWD."""
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", workdir / ".sdd" / "audit")
    monkeypatch.chdir(workdir)
    return audit_cmd._verify_trajectory_receipts()


# ---------------------------------------------------------------------------
# AC7 — absence is a silent no-op; tampered is a hard-fail
# ---------------------------------------------------------------------------


def test_audit_verify_noop_when_no_receipts_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC7: no trajectory receipts → _verify_trajectory_receipts returns True silently."""
    result = _audit_verify_receipts(tmp_path, monkeypatch)
    assert result is True


def test_audit_verify_passes_for_intact_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC7: one intact receipt → passes and returns True."""
    _build(tmp_path)
    result = _audit_verify_receipts(tmp_path, monkeypatch)
    assert result is True


def test_audit_verify_hard_fails_for_tampered_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC7: a tampered receipt → _verify_trajectory_receipts returns False (hard-fail)."""
    receipt_hash = _build(tmp_path)

    # Tamper: inflate the published_score then reseal so hash-recompute passes
    # but the spine anchor is missing → fails closed at step 6
    payload = _load_payload(tmp_path, receipt_hash)
    payload["published_score"] = 9.99
    _reseal(tmp_path, payload)

    result = _audit_verify_receipts(tmp_path, monkeypatch)
    assert result is False


def test_audit_verify_hard_fails_contaminated_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC7 + AC3: a contaminated golden suite (task_id renamed) hard-fails audit verify."""
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)

    # Rename a task id inside the anchor list — changes suite_content_hash
    payload["task_anchors"][0]["task_id"] = "attacker-injected-task"
    _reseal(tmp_path, payload)

    result = _audit_verify_receipts(tmp_path, monkeypatch)
    assert result is False


def test_audit_verify_noop_with_multiple_intact_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC7: multiple intact receipts all pass; no false failure."""
    _build(tmp_path / "run-a", run_id="run-a")
    _build(tmp_path / "run-b", anchors=[_anchor("t-X", journal_head="sha256:" + "x" * 64)], run_id="run-b")

    # Both receipts should be under the same workdir for audit verify to find them
    # Build both into the same workdir but different run_ids
    workdir = tmp_path / "combined"
    _build(workdir, run_id="run-001")
    # Second build uses a different run_id so files don't collide
    build_trajectory_receipt(
        run_id="run-002",
        task_anchors=[_anchor("t-Y", journal_head="sha256:" + "y" * 64)],
        per_tier=_per_tier(),
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
    )
    result = _audit_verify_receipts(workdir, monkeypatch)
    assert result is True


# ---------------------------------------------------------------------------
# AC3 — benchmark receipt verify: tampered item named, fails both CLIs
# ---------------------------------------------------------------------------


def test_benchmark_receipt_verify_clean_passes(tmp_path: Path) -> None:
    """AC3 baseline: an intact receipt verifies ok."""
    receipt_hash = _build(tmp_path)
    result = _verify(tmp_path, receipt_hash)
    assert result.ok is True
    assert result.reason == ""


def test_benchmark_receipt_verify_inflated_scalar_fails(tmp_path: Path) -> None:
    """AC3: hand-editing published_score (scalar fabrication) is detected.

    First tamper round: edit the file, leave receipt_hash stale.
    The hash-recompute step catches it immediately.
    """
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)
    payload["published_score"] = 9.99
    # Overwrite without resealing — stale hash caught at step 0/1
    trajectory_receipt_path(tmp_path, receipt_hash).write_text(_canonical(payload), encoding="utf-8")
    result = _verify(tmp_path, receipt_hash)
    assert result.ok is False
    assert "tampered" in result.reason.lower() or "hash" in result.reason.lower()


def test_benchmark_receipt_verify_inflated_scalar_resealed_fails(tmp_path: Path) -> None:
    """AC3: strongest attacker re-seals the inflated scalar.

    After re-sealing, the receipt_hash recomputes correctly, but the spine
    anchor entry is missing, so step 6 catches it.
    """
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)
    payload["published_score"] = 9.99
    new_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, new_hash)
    assert result.ok is False
    # Must fail closed — not a warning
    assert result.reason  # non-empty: reason always names what failed


def test_benchmark_receipt_verify_contaminated_task_id_fails(tmp_path: Path) -> None:
    """AC3: contamination — task_id renamed so suite_content_hash mismatches."""
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)
    payload["task_anchors"][0]["task_id"] = "attacker-swapped-task"
    new_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, new_hash)
    assert result.ok is False


def test_benchmark_receipt_verify_aggregate_inflation_fails(tmp_path: Path) -> None:
    """AC3: aggregate component inflated above what the per-task anchors support."""
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)
    # Inflate aggregate task_success while leaving per-task anchors unchanged
    payload["aggregate"]["task_success"] = 9.99
    new_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, new_hash)
    assert result.ok is False


def test_benchmark_receipt_verify_cherry_pick_rejected(tmp_path: Path) -> None:
    """AC3: best-of-N receipt stripped of all-but-winner heads → cherry-pick rejection."""
    bon = BestOfNProvenance(
        n_candidates=3,
        candidate_journal_heads=[
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
        ],
        selection_rule="highest_final_score",
        selected_index=0,
    )
    receipt_hash = _build(tmp_path, best_of_n=bon)
    payload = _load_payload(tmp_path, receipt_hash)

    # Strip all-but-winner candidate heads (cherry-pick)
    payload["best_of_n"]["candidate_journal_heads"] = ["sha256:" + "1" * 64]
    # n_candidates still says 3 but only 1 head present
    new_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, new_hash)
    assert result.ok is False
    assert "cherry" in result.reason.lower() or "head" in result.reason.lower() or "candidate" in result.reason.lower()


def test_benchmark_receipt_verify_missing_receipt_fails(tmp_path: Path) -> None:
    """AC3 baseline: referencing a non-existent receipt hash returns ok=False."""
    fake_hash = "sha256:" + "f" * 64
    result = _verify(tmp_path, fake_hash)
    assert result.ok is False
    assert result.requested_hash == fake_hash


# ---------------------------------------------------------------------------
# AC6 — strip-the-substrate: missing journal/fixture → fails closed, not warning
# ---------------------------------------------------------------------------


def test_strip_journal_head_from_anchor_fails_closed(tmp_path: Path) -> None:
    """AC6: clearing journal_head_hash then re-sealing → spine anchor missing → False.

    The spine anchor was written at emit time binding the intact receipt bytes.
    After stripping, the content hash changes, so the spine entry does not
    match, and verification fails closed (not a warning).
    """
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)

    # Strip the journal head — the substrate is now absent
    payload["task_anchors"][0]["journal_head_hash"] = ""
    new_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, new_hash)
    assert result.ok is False, "strip-the-substrate must fail closed, not pass as a warning"
    assert result.reason  # must name what failed, not be empty


def test_strip_events_hash_from_anchor_fails_closed(tmp_path: Path) -> None:
    """AC6: clearing events_content_hash (fixture absent) → fails closed."""
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)

    payload["task_anchors"][0]["events_content_hash"] = ""
    new_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, new_hash)
    assert result.ok is False, "absent fixture hash must fail closed, not pass as a warning"


def test_strip_all_task_anchors_fails_closed(tmp_path: Path) -> None:
    """AC6: deleting all task anchors from a non-empty suite → fails closed.

    An empty task_anchors list would produce a different suite_content_hash
    (contamination), caught at step 2.
    """
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)

    # Remove all anchors — the suite hash no longer matches
    payload["task_anchors"] = []
    new_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, new_hash)
    assert result.ok is False, "stripping all anchors must fail closed (contamination detection)"


def test_strip_substrate_result_is_not_ok_false_is_hard_fail(tmp_path: Path) -> None:
    """AC6: the strip-the-substrate result has ok=False and a non-empty reason.

    Verifies the contract: the number is *unverifiable*, not just *suspicious*.
    The reason field must be present so the operator knows which check failed.
    """
    receipt_hash = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt_hash)
    payload["task_anchors"][0]["journal_head_hash"] = ""
    new_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, new_hash)

    # ok=False is the hard-fail; reason must not be empty (names the failure)
    assert result.ok is False
    assert isinstance(result.reason, str) and result.reason.strip(), "reason must name the failure, not be blank"
    # requested_hash is always retained so the operator can locate the file
    assert result.requested_hash == new_hash
