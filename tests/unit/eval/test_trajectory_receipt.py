"""Tests for signed, independently-replayable trajectory receipts (#2925).

Acceptance criteria under test:

1. ``build_trajectory_receipt`` produces byte-identical receipt bytes, head
   hash, and receipt hash across two independent recordings of the same suite;
   no wall-clock value enters the signed bytes.  (determinism)

2. ``verify_trajectory_receipt`` re-derives the aggregate from per-task
   components and rejects any receipt whose embedded trajectory does not entail
   its published number.

3. Adversarial tests, in two rounds per failure mode.  The first edits the file
   and leaves ``receipt_hash`` stale, which the hash-recompute step catches.
   The second re-seals the hash so the attacker reaches the semantic check the
   guard actually exists for:
   (a) rename a golden task → suite-hash mismatch (contamination)
   (b) inflate the aggregate away from its anchors → re-derivation mismatch
   (c) hand-edit the published scalar → formula mismatch (scalar edit)
   (d) delete ``best_of_n`` from a best-of-N receipt → cherry-pick rejection

4. Byte integrity: an injected key, a duplicate key, re-spaced bytes, and a
   ``NaN`` literal are each rejected.  Verification hashes the stored bytes,
   not a decoded projection of them that drops what it does not recognise.

5. A receipt carrying all best-of-N heads re-selects the published index
   deterministically.

6. An empty suite produces a distinct ``NO_TASKS`` status receipt, never a
   trivial pass.

7. Round-trip: emit → reload from stored bytes → verify → assert clean.

8. ``EVENT_TRAJECTORY_RECEIPT`` is present in the HMAC chain when a chain is
   supplied.

All tests are hermetic: separate tmp dirs per run, no live providers, no
wall-clock in sealed bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_TRAJECTORY_RECEIPT,
    AuditChainStore,
)
from bernstein.eval.metrics import EvalScoreComponents, TierScores
from bernstein.eval.trajectory_receipt import (
    NO_TASKS_STATUS,
    SELECTION_BEST_OF_N,
    SELECTION_SINGLE_SHOT,
    BestOfNProvenance,
    TaskTrajectoryAnchor,
    TrajectoryReceipt,
    TrajectoryVerifyResult,
    build_trajectory_receipt,
    read_trajectory_receipt,
    trajectory_receipt_path,
    verify_trajectory_receipt,
)

_KEY = b"k" * 32

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_JOURNAL_HEAD = "sha256:" + "a" * 64
_FAKE_EVENTS_HASH = "sha256:" + "b" * 64


def _anchor(
    task_id: str,
    *,
    task_success: float = 1.0,
    code_quality: float = 0.9,
    efficiency: float = 0.8,
    reliability: float = 1.0,
    safety: float = 1.0,
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
            code_quality=code_quality,
            efficiency=efficiency,
            reliability=reliability,
            safety=safety,
        ),
    )


def _two_task_anchors() -> list[TaskTrajectoryAnchor]:
    """Canonical 2-task smoke suite used in determinism tests."""
    return [
        _anchor("smoke-001", journal_head="sha256:" + "1" * 64),
        _anchor("smoke-002", journal_head="sha256:" + "2" * 64),
    ]


def _per_tier() -> TierScores:
    return TierScores(smoke=1.0, standard=0.0, stretch=0.0, adversarial=0.0)


def _build(
    workdir: Path,
    anchors: list[TaskTrajectoryAnchor] | None = None,
    *,
    run_id: str = "run-test-001",
    per_tier: TierScores | None = None,
    best_of_n: BestOfNProvenance | None = None,
    chain: AuditChainStore | None = None,
) -> TrajectoryReceipt:
    return build_trajectory_receipt(
        run_id=run_id,
        task_anchors=anchors if anchors is not None else _two_task_anchors(),
        per_tier=per_tier if per_tier is not None else _per_tier(),
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        best_of_n=best_of_n,
        chain=chain,
    )


def _verify(workdir: Path, receipt_hash: str) -> TrajectoryVerifyResult:
    return verify_trajectory_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt_hash,
    )


def _canonical(payload: dict) -> str:
    """The exact encoding the receipt writer produces."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_payload(workdir: Path, receipt_hash: str) -> dict:
    return json.loads(trajectory_receipt_path(workdir, receipt_hash).read_text(encoding="utf-8"))


def _rewrite_canonical(workdir: Path, receipt_hash: str, payload: dict) -> None:
    """Store a tampered *payload* in canonical form under the same name.

    Tamper tests must write canonical bytes, otherwise the byte-integrity gate
    fires on the re-encoding alone and the semantic check under test is never
    reached.
    """
    trajectory_receipt_path(workdir, receipt_hash).write_text(_canonical(payload), encoding="utf-8")


def _reseal(workdir: Path, payload: dict) -> str:
    """Re-hash a tampered payload and store it under its new content address.

    Models the strongest attacker short of holding the HMAC key: one who edits
    the receipt and recomputes ``receipt_hash`` so the hash-recompute step
    cannot catch them.  Without this the semantic checks below step 1 are never
    exercised.
    """
    body = {k: v for k, v in payload.items() if k not in ("receipt_hash", "journal_entry_hash")}
    new_hash = "sha256:" + hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    payload["receipt_hash"] = new_hash
    trajectory_receipt_path(workdir, new_hash).write_text(_canonical(payload), encoding="utf-8")
    return new_hash


# ---------------------------------------------------------------------------
# AC1 -- determinism
# ---------------------------------------------------------------------------


def test_two_independent_runs_produce_identical_receipt(tmp_path: Path) -> None:
    """Two independent workdirs, same inputs → byte-identical receipt + hash."""
    dir_a = tmp_path / "run-a"
    dir_b = tmp_path / "run-b"

    anchors = _two_task_anchors()
    receipt_a = _build(dir_a, anchors)
    # Reverse task order to prove order-canonical, not order-lucky.
    receipt_b = _build(dir_b, list(reversed(anchors)))

    assert receipt_a.receipt_hash == receipt_b.receipt_hash
    assert receipt_a.canonical_payload_without_anchor() == receipt_b.canonical_payload_without_anchor()
    assert receipt_a.canonical_bytes() == receipt_b.canonical_bytes()
    # The lineage anchor is assigned post-seal, so it is the field most likely
    # to pick up ambient state. Two fresh spines must still agree on it.
    assert receipt_a.journal_entry_hash == receipt_b.journal_entry_hash
    # And the published artefact itself -- what an operator actually ships --
    # must match byte for byte, not merely agree on its hash.
    bytes_a = trajectory_receipt_path(dir_a, receipt_a.receipt_hash).read_bytes()
    bytes_b = trajectory_receipt_path(dir_b, receipt_b.receipt_hash).read_bytes()
    assert bytes_a == bytes_b


def test_receipt_hash_is_stable_across_identical_inputs(tmp_path: Path) -> None:
    """Two calls with identical inputs (same dir is fine for this) → same hash."""
    dir_a = tmp_path / "run-a"
    dir_b = tmp_path / "run-b"
    anchors = _two_task_anchors()
    r1 = _build(dir_a, anchors)
    r2 = _build(dir_b, anchors)
    assert r1.receipt_hash == r2.receipt_hash


# ---------------------------------------------------------------------------
# AC5 -- empty suite → NO_TASKS, not trivial pass
# ---------------------------------------------------------------------------


def test_empty_suite_produces_no_tasks_status(tmp_path: Path) -> None:
    receipt = _build(tmp_path, anchors=[])
    assert receipt.status == NO_TASKS_STATUS
    assert receipt.published_score == 0.0
    assert receipt.task_anchors == []
    # The NO_TASKS receipt must verify cleanly (it is a legitimate sealed state)
    result = _verify(tmp_path, receipt.receipt_hash)
    assert result.ok, result.reason


def test_empty_suite_receipt_hash_differs_from_non_empty(tmp_path: Path) -> None:
    empty_r = _build(tmp_path / "empty", anchors=[])
    full_r = _build(tmp_path / "full", _two_task_anchors())
    assert empty_r.receipt_hash != full_r.receipt_hash


# ---------------------------------------------------------------------------
# AC2 / AC6 -- round-trip and offline verification
# ---------------------------------------------------------------------------


def test_receipt_verifies_offline_clean(tmp_path: Path) -> None:
    receipt = _build(tmp_path)
    result = _verify(tmp_path, receipt.receipt_hash)
    assert result.ok, result.reason
    assert result.receipt is not None
    assert result.receipt.receipt_hash == receipt.receipt_hash


def test_round_trip_reload_and_verify(tmp_path: Path) -> None:
    """Emit → reload from stored bytes → verify → assert clean."""
    receipt = _build(tmp_path)
    reloaded = read_trajectory_receipt(tmp_path, receipt.receipt_hash)
    assert reloaded is not None
    assert reloaded.to_dict() == receipt.to_dict()
    result = _verify(tmp_path, receipt.receipt_hash)
    assert result.ok, result.reason


def test_missing_receipt_returns_not_ok(tmp_path: Path) -> None:
    fake_hash = "sha256:" + "f" * 64
    result = _verify(tmp_path, fake_hash)
    assert not result.ok
    assert "no trajectory receipt" in result.reason


# ---------------------------------------------------------------------------
# AC3a -- contamination: flip one task id → suite-hash mismatch
# ---------------------------------------------------------------------------


def test_contamination_mutated_task_id_fails_hash_recompute(tmp_path: Path) -> None:
    """A rename with a stale receipt_hash is caught by the hash-recompute step."""
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    # Silently rename a task — simulates a mutated golden suite.
    payload["task_anchors"][0]["task_id"] = "smoke-TAMPERED"
    # Do NOT recompute receipt_hash — leave it stale so tamper is visible.
    _rewrite_canonical(tmp_path, receipt.receipt_hash, payload)
    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "does not recompute" in result.reason


def test_contamination_survives_resealing_and_hits_suite_hash(tmp_path: Path) -> None:
    """A rename WITH a recomputed hash must still fail on suite_content_hash.

    This is the check the contamination guard exists for.  Re-sealing carries
    the attacker past the hash-recompute step, so without step 2 a mutated
    golden suite would verify clean.
    """
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["task_anchors"][0]["task_id"] = "smoke-TAMPERED"
    tampered_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, tampered_hash)
    assert not result.ok
    assert "suite_content_hash mismatch" in result.reason
    assert "contamination" in result.reason


# ---------------------------------------------------------------------------
# AC3b -- fabrication: edit a per-task component → re-derivation mismatch
# ---------------------------------------------------------------------------


def test_edited_component_fails_verification(tmp_path: Path) -> None:
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    # Drop task_success to 0.0 on the first task without recomputing hashes.
    payload["task_anchors"][0]["components"]["task_success"] = 0.0
    _rewrite_canonical(tmp_path, receipt.receipt_hash, payload)
    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "does not recompute" in result.reason


def test_inflated_aggregate_survives_resealing_and_hits_rederivation(tmp_path: Path) -> None:
    """An aggregate raised away from honest anchors fails re-derivation."""
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["aggregate"]["task_success"] = 1.0
    payload["task_anchors"][0]["components"]["task_success"] = 0.0
    tampered_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, tampered_hash)
    assert not result.ok
    assert "do not re-derive" in result.reason


def test_aggregate_divergence_blames_no_individual_task(tmp_path: Path) -> None:
    """Regression: the report must not name an honest outlier as the culprit.

    Only the aggregate is inflated here; every anchor is untouched.  A blame
    heuristic that picks the anchor furthest from the recomputed mean would
    accuse ``task-d``, which is simply the suite's genuine low scorer.
    """
    anchors = [
        _anchor("task-a", journal_head="sha256:" + "1" * 64),
        _anchor("task-b", journal_head="sha256:" + "2" * 64),
        _anchor("task-c", journal_head="sha256:" + "3" * 64),
        _anchor("task-d", task_success=0.0, journal_head="sha256:" + "4" * 64),
    ]
    receipt = _build(tmp_path, anchors)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["aggregate"]["task_success"] = 1.0
    tampered_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, tampered_hash)
    assert not result.ok
    assert result.failing_task_index == -1
    assert "task-d" not in result.reason
    # The report names the divergent aggregate field instead.
    assert "task_success" in result.reason


# ---------------------------------------------------------------------------
# AC3c -- scalar edit: hand-edit the published score → formula mismatch
# ---------------------------------------------------------------------------


def test_scalar_edit_fails_verification(tmp_path: Path) -> None:
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    # Bump the published score without touching anything else.
    payload["published_score"] = 0.9999
    _rewrite_canonical(tmp_path, receipt.receipt_hash, payload)
    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "does not recompute" in result.reason


def test_scalar_edit_survives_resealing_and_hits_formula(tmp_path: Path) -> None:
    """A re-sealed scalar edit must still fail the formula check."""
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["published_score"] = 0.9999
    tampered_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, tampered_hash)
    assert not result.ok
    assert "scalar edit" in result.reason


# ---------------------------------------------------------------------------
# AC3d -- cherry-pick: drop all-but-winner candidate heads → rejected
# ---------------------------------------------------------------------------


def test_cherry_pick_missing_candidate_heads_fails(tmp_path: Path) -> None:
    bon = BestOfNProvenance(
        n_candidates=3,
        # Claim 3 candidates but only supply 1 head → cherry-pick
        candidate_journal_heads=["sha256:" + "c" * 64],
        selection_rule="highest_final_score",
        selected_index=0,
    )
    receipt = _build(tmp_path, best_of_n=bon)
    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "cherry-pick" in result.reason or "missing heads" in result.reason


def test_cherry_pick_all_heads_present_verifies_ok(tmp_path: Path) -> None:
    bon = BestOfNProvenance(
        n_candidates=3,
        candidate_journal_heads=[
            "sha256:" + "c" * 64,
            "sha256:" + "d" * 64,
            "sha256:" + "e" * 64,
        ],
        selection_rule="highest_final_score",
        selected_index=1,
    )
    receipt = _build(tmp_path, best_of_n=bon)
    result = _verify(tmp_path, receipt.receipt_hash)
    assert result.ok, result.reason


def test_cherry_pick_selected_index_out_of_range_fails(tmp_path: Path) -> None:
    bon = BestOfNProvenance(
        n_candidates=2,
        candidate_journal_heads=["sha256:" + "c" * 64, "sha256:" + "d" * 64],
        selection_rule="highest_final_score",
        selected_index=5,  # out of range
    )
    receipt = _build(tmp_path, best_of_n=bon)
    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok


# ---------------------------------------------------------------------------
# AC7 -- HMAC chain mirror
# ---------------------------------------------------------------------------


def test_audit_chain_receives_trajectory_receipt_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    receipt = _build(tmp_path, chain=chain)
    events = chain.query(event_type=EVENT_TRAJECTORY_RECEIPT)
    assert len(events) == 1
    e = events[0]
    assert e.details.get("receipt_hash") == receipt.receipt_hash
    assert e.details.get("run_id") == receipt.run_id
    assert e.details.get("n_tasks") == len(receipt.task_anchors)


# ---------------------------------------------------------------------------
# Structural sanity
# ---------------------------------------------------------------------------


def test_receipt_schema_version_is_1(tmp_path: Path) -> None:
    receipt = _build(tmp_path)
    assert receipt.schema_version == 1


def test_published_score_matches_final_score(tmp_path: Path) -> None:
    anchors = _two_task_anchors()
    receipt = _build(tmp_path, anchors)
    # Re-derive manually: mean of per-task components → final_score
    n = len(anchors)
    ts = sum(a.components.task_success for a in anchors) / n
    cq = sum(a.components.code_quality for a in anchors) / n
    eff = sum(a.components.efficiency for a in anchors) / n
    rel = sum(a.components.reliability for a in anchors) / n
    saf = sum(a.components.safety for a in anchors) / n
    expected = (0.5 * ts + 0.3 * cq + 0.2 * eff) * rel * saf
    assert abs(receipt.published_score - expected) < 1e-9


def test_read_trajectory_receipt_returns_none_for_bad_hash(tmp_path: Path) -> None:
    # Build one receipt so the directory exists
    _build(tmp_path)
    result = read_trajectory_receipt(tmp_path, "sha256:" + "0" * 64)
    assert result is None


def test_trajectory_receipt_path_rejects_non_sha256(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="canonical sha256"):
        trajectory_receipt_path(tmp_path, "not-a-hash")


def test_trajectory_receipt_path_refuses_hash_that_resolves_outside_bench_dir(tmp_path: Path) -> None:
    """A pre-planted symlink named ``<hash>.json`` must not smuggle a read/write outside bench.

    The hash itself can never carry ``..`` -- ``_RECEIPT_HASH_RE`` only admits
    hex -- so the only way this check can fire is the final path component
    already existing as a symlink out of the bench directory before this
    function resolves it.
    """
    receipt_hash = "sha256:" + "c" * 64
    workdir = tmp_path / "workdir"
    bench_dir = workdir.joinpath(".sdd", "eval", "bench")
    bench_dir.mkdir(parents=True)
    outside = tmp_path / "host-secret.json"
    outside.write_text("not a receipt", encoding="utf-8")
    (bench_dir / f"{receipt_hash}.json").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes bench directory"):
        trajectory_receipt_path(workdir, receipt_hash)


def test_trajectory_receipt_path_accepts_the_ordinary_case(tmp_path: Path) -> None:
    """Positive control: an un-planted hash still resolves under bench, unaltered."""
    receipt_hash = "sha256:" + "d" * 64
    path = trajectory_receipt_path(tmp_path, receipt_hash)
    assert path == tmp_path.resolve() / ".sdd" / "eval" / "bench" / f"{receipt_hash}.json"


# ---------------------------------------------------------------------------
# Byte integrity -- verification must hash the stored bytes, not a projection
# ---------------------------------------------------------------------------


def test_injected_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    """A key the schema does not know must not be droppable before hashing.

    Decoding into the dataclass discards unrecognised keys.  If the verifier
    then rehashes that parsed projection, anything outside the schema rides
    along in the published file while verification still reports clean.
    """
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["published_score_display"] = 0.99
    _rewrite_canonical(tmp_path, receipt.receipt_hash, payload)

    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "canonical encoding" in result.reason


def test_injected_unknown_key_inside_anchor_is_rejected(tmp_path: Path) -> None:
    """The same hole one level down, inside a task anchor and its components."""
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["task_anchors"][0]["real_task_success"] = 0.0
    payload["task_anchors"][0]["components"]["shadow"] = 999
    _rewrite_canonical(tmp_path, receipt.receipt_hash, payload)

    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "canonical encoding" in result.reason


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    """Duplicate keys make the file mean different things to different parsers.

    Python keeps the last occurrence, so a decoy inserted ahead of the honest
    value leaves this verifier reading the honest one while a first-wins
    parser elsewhere reads the decoy.
    """
    receipt = _build(tmp_path)
    path = trajectory_receipt_path(tmp_path, receipt.receipt_hash)
    text = path.read_text(encoding="utf-8")
    tampered = text.replace('{"aggregate"', '{"published_score":0.99,"aggregate"', 1)
    assert tampered != text, "canonical layout changed; update this fixture"
    path.write_text(tampered, encoding="utf-8")

    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "canonical encoding" in result.reason


def test_semantically_identical_reformatting_is_rejected(tmp_path: Path) -> None:
    """Re-spaced but otherwise identical bytes are not the sealed bytes."""
    receipt = _build(tmp_path)
    path = trajectory_receipt_path(tmp_path, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "canonical encoding" in result.reason


def test_non_finite_literal_is_rejected(tmp_path: Path) -> None:
    """``NaN`` is a Python extension to JSON and defeats tolerance checks.

    ``abs(x - nan) > tol`` is False, so a NaN scalar would pass the published
    score check rather than fail it, and the file would not parse at all in a
    conforming third-party verifier.
    """
    receipt = _build(tmp_path)
    path = trajectory_receipt_path(tmp_path, receipt.receipt_hash)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"published_score":0.93', '"published_score":NaN', 1), encoding="utf-8")

    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert "non-finite" in result.reason


def test_clean_receipt_bytes_are_byte_identical_to_canonical_form(tmp_path: Path) -> None:
    """The writer's output is exactly what the verifier re-derives."""
    receipt = _build(tmp_path)
    stored = trajectory_receipt_path(tmp_path, receipt.receipt_hash).read_text(encoding="utf-8")
    assert stored == _canonical(receipt.to_dict())


def test_requested_hash_is_retained_when_bytes_are_rejected(tmp_path: Path) -> None:
    """An operator must be able to name the file even on an undecodable one."""
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["mystery"] = 1
    _rewrite_canonical(tmp_path, receipt.receipt_hash, payload)

    result = _verify(tmp_path, receipt.receipt_hash)
    assert not result.ok
    assert result.receipt is None
    assert result.requested_hash == receipt.receipt_hash


# ---------------------------------------------------------------------------
# Selection disclosure -- "not best-of-N" is a claim, not an absent field
# ---------------------------------------------------------------------------


def test_single_shot_receipt_states_its_selection_mode(tmp_path: Path) -> None:
    receipt = _build(tmp_path)
    assert receipt.selection_mode == SELECTION_SINGLE_SHOT
    assert receipt.body()["selection_mode"] == SELECTION_SINGLE_SHOT


def test_best_of_n_receipt_states_its_selection_mode(tmp_path: Path) -> None:
    bon = BestOfNProvenance(
        n_candidates=2,
        candidate_journal_heads=["sha256:" + "c" * 64, "sha256:" + "d" * 64],
        selection_rule="highest_final_score",
        selected_index=0,
    )
    receipt = _build(tmp_path, best_of_n=bon)
    assert receipt.selection_mode == SELECTION_BEST_OF_N


def test_stripping_best_of_n_from_a_sealed_receipt_is_rejected(tmp_path: Path) -> None:
    """Downgrading a sealed best-of-N receipt to single-shot must fail.

    Selection mode lives in the signed body, so deleting ``best_of_n`` leaves
    a receipt that contradicts its own declared mode even after re-sealing.
    """
    bon = BestOfNProvenance(
        n_candidates=5,
        candidate_journal_heads=["sha256:" + c * 64 for c in "abcde"],
        selection_rule="highest_final_score",
        selected_index=3,
    )
    receipt = _build(tmp_path, best_of_n=bon)
    assert _verify(tmp_path, receipt.receipt_hash).ok

    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["best_of_n"] = None
    tampered_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, tampered_hash)
    assert not result.ok
    assert "cherry-pick" in result.reason


def test_declaring_best_of_n_without_provenance_is_rejected(tmp_path: Path) -> None:
    receipt = _build(tmp_path)
    payload = _load_payload(tmp_path, receipt.receipt_hash)
    payload["selection_mode"] = SELECTION_BEST_OF_N
    tampered_hash = _reseal(tmp_path, payload)

    result = _verify(tmp_path, tampered_hash)
    assert not result.ok
    assert "cherry-pick" in result.reason


# ---------------------------------------------------------------------------
# Build-time input validation
# ---------------------------------------------------------------------------


def test_duplicate_task_id_is_rejected_at_build(tmp_path: Path) -> None:
    """A repeated task would be counted twice behind an honest suite hash.

    ``suite_content_hash`` de-duplicates its input, so two anchors sharing an
    id hash the same as one while both still feed the aggregate mean.
    """
    anchors = [_anchor("smoke-001"), _anchor("smoke-001", task_success=0.0)]
    with pytest.raises(ValueError, match="duplicate task_id"):
        _build(tmp_path, anchors)


def test_non_finite_component_is_rejected_at_build(tmp_path: Path) -> None:
    anchors = [_anchor("smoke-001", task_success=float("nan"))]
    with pytest.raises(ValueError, match="non-finite"):
        _build(tmp_path, anchors)


def test_receipt_no_wall_clock_in_body(tmp_path: Path) -> None:
    """Confirm that 'timestamp' does not appear in the canonical body."""
    receipt = _build(tmp_path)
    body_str = receipt.canonical_payload_without_anchor()
    # The body must not carry any wall-clock timestamp field.
    body = json.loads(body_str)
    assert "timestamp" not in body
