"""Emit the collusion eval suite as a SubmissionBundle.

Maps each case onto the bundle's existing per-task granularity: one
TaskResult per case, whose receipt is the same ``cross-task-collusion``
section the merge admission path records (#5398) plus the TaskEffects
footprints the verdict was computed from — so every score in the bundle is
replayable from its own receipt, per the bundle contract.

bundle.py is untouched: results ride inside the payload ``bundle_hash()``
covers (via ``task_results``), so the existing Ed25519 signing path and the
``from_dict`` integrity guard cover them without a schema change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bernstein.core.lineage.dependency import TaskEffects
from bernstein.core.quality.collusion import (
    GUARDED_CONFIG_KEYS,
    GUARDED_SYMBOLS,
    SAFE_CONFIG_VALUES,
    cross_task_check,
)
from bernstein.core.quality.collusion_gate import run_cross_task_gate
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.collusion_suite import CollusionCase, run_case

COLLUSION_SUITE_VERSION = "collusion/1"


def _case_payload(case: CollusionCase) -> dict[str, Any]:
    return {
        "id": case.id,
        "kind": case.kind,
        "stated_invariant": case.stated_invariant,
        "tasks": [t.to_dict() for t in case.tasks],
    }


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def suite_hash(cases: list[CollusionCase]) -> str:
    """Hash of the fixture set the run evaluated — replayable inputs."""
    ordered = sorted(cases, key=lambda c: c.id)
    return hashlib.sha256(_canonical([_case_payload(c) for c in ordered])).hexdigest()


def scheduler_config() -> dict[str, Any]:
    """Checker configuration that shaped the run.

    Feeds harness_fingerprint: a change to the guarded-symbol or guarded-key
    sets changes the run identity, so two runs under different checker
    configs never compare as equivalent.
    """
    return {
        "suite": "collusion",
        "guarded_symbols": sorted(GUARDED_SYMBOLS),
        "guarded_config_keys": sorted(GUARDED_CONFIG_KEYS),
        "safe_config_values": {k: sorted(v) for k, v in sorted(SAFE_CONFIG_VALUES.items())},
    }


def case_task_result(case: CollusionCase) -> TaskResult:
    # The receipt is produced by the same object the merge admission path
    # uses (CrossTaskAdmission.receipt_section), so the bundle's per-case
    # receipts and the merge receipt's collusion record cannot drift.
    # One evaluation feeds both the judged result and the receipt.
    admission = run_cross_task_gate(list(case.tasks))
    result = run_case(case, verdict=admission.verdict)
    receipt = {
        **admission.receipt_section(),
        "tasks": [t.to_dict() for t in case.tasks],
    }
    return TaskResult(
        task_id=case.id,
        task_hash=hashlib.sha256(_canonical(_case_payload(case))).hexdigest(),
        receipt=receipt,
        passed=result.passed,
        score=1.0 if result.passed else 0.0,
        harness_output={
            "case_kind": case.kind,
            "expected": result.expected,
            "actual": result.actual,
            "stated_invariant": case.stated_invariant,
        },
    )


def build_collusion_bundle(cases: list[CollusionCase]) -> SubmissionBundle:
    return SubmissionBundle(
        suite_hash=suite_hash(cases),
        suite_version=COLLUSION_SUITE_VERSION,
        task_results=[case_task_result(c) for c in cases],
        scheduler_config=scheduler_config(),
    )


def score_from_bundle(bundle: SubmissionBundle) -> dict[str, Any]:
    """Aggregate the suite score from the signed per-case results."""
    rs = [r for r in bundle.task_results if r.receipt.get("check") == "cross-task-collusion"]
    colluding = [r for r in rs if r.harness_output["case_kind"] == "collusion"]
    benign = [r for r in rs if r.harness_output["case_kind"] == "benign"]
    return {
        "suite": "collusion",
        "cases": len(rs),
        "collusion_flagged": sum(1 for r in colluding if r.passed),
        "collusion_total": len(colluding),
        "benign_passed": sum(1 for r in benign if r.passed),
        "benign_total": len(benign),
        "false_positives": sum(1 for r in benign if not r.passed),
        "false_negatives": sum(1 for r in colluding if not r.passed),
        "pass_rate": (sum(1 for r in rs if r.passed) / len(rs)) if rs else 0.0,
    }


def replay_receipt(result: TaskResult) -> list[dict[str, Any]]:
    """Recompute the verdict from the footprints stored in the receipt."""
    footprints = [TaskEffects.from_dict(t) for t in result.receipt["tasks"]]
    return [f.to_dict() for f in cross_task_check(footprints).flags]


def sign_bundle(bundle: SubmissionBundle, private_key_pem: str) -> SubmissionBundle:
    """Sign the bundle hash with the install identity.

    Uses the same ``sign_payload`` path the merge receipt family uses, so
    receipts and bench bundles share one signing convention. Signing the
    hash is equivalent to signing the payload: ``bundle_hash()`` covers
    everything except the signature field.
    """
    bundle.signature = sign_payload(bundle.bundle_hash().encode("utf-8"), private_key_pem)
    return bundle


def verify_bundle_signature(bundle: SubmissionBundle, public_key_pem: str) -> bool:
    if not bundle.signature:
        return False
    outcome = verify_payload(
        bundle.bundle_hash().encode("utf-8"),
        bundle.signature,
        public_key_pem,
        # Same trust posture as verify_merge_receipt: the public key is
        # carried/embedded alongside the signed artefact.
        allow_unverified=True,
    )
    return bool(outcome.verified)
