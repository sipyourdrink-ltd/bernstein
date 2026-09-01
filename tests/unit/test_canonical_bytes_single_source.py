"""Guard: canonical JSON for signing has one implementation (#5094).

Every hash or signature in this package is computed over bytes derived from a
JSON-shaped payload. When two modules derive those bytes differently, a
signature made under one does not verify under the other, and the artefact
alone cannot say which rule produced it. ``core.security.canonical`` owns the
rule; this test walks the package with ``ast`` and fails when a second
definition appears under any of the names the copies used to have.

``REMAINING_UNTIL_SLICE_4`` lists the definitions that still exist on purpose:
method-style wrappers that delegate to the one rule, and the producers whose
byte rule differs and need a canonicalization version on their artefacts
before they can move. The set is asserted in both directions, so removing one
of them without pruning it here fails too: the list can only shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "bernstein"
_NAMES = frozenset({"_canonical_bytes", "_canonical_json", "canonical_bytes"})

THE_ONE = "core/security/canonical.py::canonical_bytes"

REMAINING_UNTIL_SLICE_4 = frozenset(
    {
        "adapters/capability_profile.py::AdapterCapabilityProfile.canonical_bytes",
        "core/agents/context_capsule.py::ContextCapsule.canonical_bytes",
        "core/agents/subagent_delegation.py::_canonical_bytes",
        "core/cost/scheduling/knob_matrix.py::KnobMatrix._canonical_bytes",
        "core/cost/scheduling/policy.py::DispatchDecision.canonical_bytes",
        "core/cost/scheduling/price_table.py::PriceTable._canonical_bytes",
        "core/datasources/result.py::canonical_bytes",
        "core/datasources/schema.py::SchemaSnapshot.canonical_bytes",
        "core/events/actions.py::_canonical_json",
        "core/evidence/run_artifacts.py::ArtifactPayload.canonical_bytes",
        "core/finding_verify.py::FindingVerifyReceipt.canonical_bytes",
        "core/finding_verify.py::_canonical_bytes",
        "core/git/read_set_receipt.py::ReadSetRefusalReceipt.canonical_bytes",
        "core/lineage/artifact_events.py::ArtifactProductionEvent.canonical_bytes",
        "core/lineage/artifact_health.py::_canonical_json",
        "core/lineage/run_graph.py::RunGraphReceipt.canonical_bytes",
        "core/observability/ticket_bundle.py::BundleManifest.canonical_bytes",
        "core/orchestration/activity.py::_canonical_bytes",
        "core/orchestration/activity_modalities.py::_canonical_bytes",
        "core/orchestration/mission_digest.py::MissionDigest.canonical_bytes",
        "core/orchestration/mission_digest.py::_canonical_bytes",
        "core/orchestration/missions.py::MissionStatus.canonical_bytes",
        "core/orchestration/missions.py::_canonical_bytes",
        "core/orchestration/sla_receipt.py::_canonical_json",
        "core/orchestration/supervisor_receipt.py::_canonical_json",
        "core/orchestration/tracker_pipeline.py::ClaimState.canonical_bytes",
        "core/persistence/journal_export.py::ReceiptManifest.canonical_bytes",
        "core/planning/recovery_receipt.py::RecoveryReceipt.canonical_bytes",
        "core/planning/recovery_receipt.py::_canonical_bytes",
        "core/planning/sla_store.py::SLAContract.canonical_bytes",
        "core/quality/verifier_ladder.py::LadderReceipt.canonical_bytes",
        "core/quality/verifier_ladder.py::TierRecord.canonical_bytes",
        "core/replay/diagnose.py::_canonical_json",
        "core/sandbox/pool.py::_canonical_json",
        "core/sandbox/pool_enrolment.py::_canonical_json",
        "core/sandbox/pool_placement.py::_canonical_json",
        "core/sandbox/selection_receipt.py::_canonical_json",
        "core/security/audit_dsse.py::_canonical_json",
        "core/security/change_receipt.py::ChangeReceipt.canonical_bytes",
        "core/security/input_refusal.py::InputRefusalReceipt.canonical_bytes",
        "core/security/result_receipt_bundle.py::ResultBundle.canonical_bytes",
        "core/security/result_receipt_bundle.py::canonical_bytes",
        "core/security/toolcall_identity.py::ToolCallIdentityAttestation.canonical_bytes",
        "core/skills/catalog/revocation.py::RevocationEntry.canonical_bytes",
        "core/skills/catalog/transparency.py::SignedTreeHead.canonical_bytes",
        "core/tasks/checkpoint_retry.py::RetryDecision.canonical_bytes",
        "core/tasks/completion_record.py::TaskCompletionRecord.canonical_bytes",
        "core/tasks/decomposition_guard.py::DecompositionRefusalReceipt.canonical_bytes",
        "core/tasks/task_pack.py::TaskContextPack.canonical_bytes",
        "core/volunteer/clean_room.py::CleanRoomVerificationReceipt.canonical_bytes",
        "core/volunteer/clean_room.py::canonical_bytes",
        "core/volunteer/consent.py::ConsentReceipt.canonical_bytes",
        "core/volunteer/consent.py::canonical_bytes",
        "eval/bench/reliability.py::_canonical_bytes",
        "eval/clean_run.py::CleanRunAttestation.canonical_bytes",
        "eval/clean_run.py::ContrabandSet.canonical_bytes",
        "eval/clean_run.py::EquivalenceAttestation.canonical_bytes",
        "eval/gate_receipt.py::VerdictReceipt.canonical_bytes",
        "eval/promotion.py::RevocationReceipt.canonical_bytes",
        "eval/trajectory_receipt.py::TrajectoryReceipt.canonical_bytes",
    }
)


def _definitions() -> set[str]:
    found: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        stack: list[tuple[ast.AST, str]] = [(ast.parse(path.read_text(encoding="utf-8")), "")]
        while stack:
            node, prefix = stack.pop()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    stack.append((child, prefix + child.name + "."))
                elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    if child.name in _NAMES:
                        found.add(f"{rel}::{prefix}{child.name}")
                    stack.append((child, prefix + child.name + "."))
    return found


def test_canonical_json_has_exactly_one_free_implementation() -> None:
    """Every definition outside the allowlist is a second byte rule; none may exist."""
    found = _definitions()
    unexpected = sorted(found - REMAINING_UNTIL_SLICE_4 - {THE_ONE})
    assert THE_ONE in found, f"{THE_ONE} is missing"
    assert unexpected == [], f"new canonical-JSON definitions outside core.security.canonical: {unexpected}"


def test_allowlist_only_shrinks() -> None:
    """A wrapper removed from the code must be removed here too, or the list rots."""
    stale = sorted(REMAINING_UNTIL_SLICE_4 - _definitions())
    assert stale == [], f"allowlisted definitions no longer exist; prune them: {stale}"
