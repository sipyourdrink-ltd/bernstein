"""Property: the collusion score is replayable from, and covered by, the signed bundle.

Results outside the signed payload can be edited after the fact; the
BENCHMARKS.md row publishes numbers that must be derivable from the bundle
that carries them. The replay substrate is the same cross-task receipt the
merge admission path records (#5398).
"""

from pathlib import Path

import pytest

from bernstein.core.lineage.identity import generate_keypair
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.collusion_bundle import (
    build_collusion_bundle,
    replay_receipt,
    score_from_bundle,
    sign_bundle,
    verify_bundle_signature,
)
from bernstein.eval.bench.collusion_suite import load_cases

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_collusion_results_replayable_from_signed_bundle():
    cases = load_cases()
    private_pem, public_pem = generate_keypair()  # identity.py: (private, public)
    bundle = sign_bundle(build_collusion_bundle(cases), private_pem)

    # the aggregate is derivable from the signed per-case results
    score = score_from_bundle(bundle)
    assert score["collusion_flagged"] == 5
    assert score["collusion_total"] == 5
    assert score["benign_passed"] == 5
    assert score["benign_total"] == 5
    assert score["false_positives"] == 0
    assert score["false_negatives"] == 0
    assert bundle.pass_rate == 1.0

    # every receipt replays: recomputing the cross-task check from the
    # footprints inside the receipt reproduces the recorded flags
    for r in bundle.task_results:
        assert replay_receipt(r) == r.receipt["flags"]
        assert r.receipt["admitted"] == (not r.receipt["flags"])

    # the existing from_dict integrity guard catches payload tampering
    raw = bundle.to_dict()
    raw["task_results"][0]["score"] = 0.0
    with pytest.raises(ValueError, match="tampered"):
        SubmissionBundle.from_dict(raw)

    # signature verifies, and the same signature fails over altered results
    assert verify_bundle_signature(bundle, public_pem)
    tampered_results = [
        TaskResult(
            task_id=r.task_id,
            task_hash=r.task_hash,
            receipt=r.receipt,
            passed=r.passed,
            score=0.0 if i == 0 else r.score,
            harness_output=r.harness_output,
            stored_receipt_hash=r.stored_receipt_hash,
        )
        for i, r in enumerate(bundle.task_results)
    ]
    tampered = SubmissionBundle(
        suite_hash=bundle.suite_hash,
        suite_version=bundle.suite_version,
        task_results=tampered_results,
        scheduler_config=bundle.scheduler_config,
        submitted_at=bundle.submitted_at,
        signature=bundle.signature,
    )
    assert tampered.bundle_hash() != bundle.bundle_hash()
    assert not verify_bundle_signature(tampered, public_pem)

    # BENCHMARKS.md row, derivable from the bundle
    benchmarks = (REPO_ROOT / "BENCHMARKS.md").read_text(encoding="utf-8")
    rows = [line for line in benchmarks.splitlines() if line.lstrip().startswith("|") and "collusion" in line]
    assert rows, "BENCHMARKS.md must carry a collusion suite row"
    assert any("5/5" in row for row in rows)
