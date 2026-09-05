"""Tests for constraint layer manifest, invariants hash-locking, and drift guards (#5440)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.evolution.admission import (
    AdmissionMode,
    AdmissionPolicy,
    ColdStartMode,
)
from bernstein.evolution.gate import (
    ApprovalGate,
    ApprovalOutcome,
)
from bernstein.evolution.invariants import (
    CONSTRAINT_MANIFEST,
    LOCKED_FILES,
    is_constraint_path,
    resolve_locked_files,
)
from bernstein.evolution.types import RiskLevel, UpgradeProposal

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEOWNERS_PATH = REPO_ROOT / ".github" / "CODEOWNERS"


def _make_proposal(
    *,
    id: str = "UPG-TEST",
    risk_level: RiskLevel = RiskLevel.L0_CONFIG,
    target_files: list[str] | None = None,
    confidence: float = 0.99,
) -> UpgradeProposal:
    return UpgradeProposal(
        id=id,
        title="Test Proposal",
        description="Testing constraint layer",
        risk_level=risk_level,
        target_files=target_files or ["src/bernstein/core/security/audit.py"],
        diff="--- a\n+++ b",
        rationale="Testing",
        expected_impact="None",
        confidence=confidence,
    )


class TestConstraintLayerRejection:
    """Proposal fixture touching core/audit rejected at L0, L1, L2, L3 with the same reason."""

    @pytest.mark.parametrize(
        "level",
        [
            RiskLevel.L0_CONFIG,
            RiskLevel.L1_TEMPLATE,
            RiskLevel.L2_LOGIC,
            RiskLevel.L3_STRUCTURAL,
        ],
    )
    def test_proposal_touching_core_audit_rejected_at_all_levels_with_same_reason(
        self,
        tmp_path: Path,
        level: RiskLevel,
    ) -> None:
        decisions_dir = tmp_path / "evolution"
        gate = ApprovalGate(decisions_dir=decisions_dir)

        # Target touching core/audit
        proposal = _make_proposal(
            id=f"P-{level.name}",
            risk_level=level,
            target_files=["src/bernstein/core/security/audit.py"],
            confidence=0.99,
        )

        decision = gate.route(proposal)

        assert decision.outcome == ApprovalOutcome.BLOCKED
        assert (
            decision.reason
            == "Proposal targets constraint layer / locked file(s): src/bernstein/core/security/audit.py"
        )
        assert decision.requires_human is True

        # Verify audit record was written to decisions.jsonl
        decisions_log = decisions_dir / "decisions.jsonl"
        assert decisions_log.exists()
        logged = [json.loads(line) for line in decisions_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any(d["proposal_id"] == f"P-{level.name}" and d["outcome"] == "blocked" for d in logged)

    def test_proposal_touching_shorthand_core_audit_rejected(self, tmp_path: Path) -> None:
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        proposal = _make_proposal(
            id="P-shorthand",
            target_files=["core/audit/audit_chain.py"],
            confidence=0.99,
        )
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.BLOCKED
        assert "core/audit/audit_chain.py" in decision.reason

    def test_admission_policy_refuses_constraint_layer_proposal(self) -> None:
        admission = AdmissionPolicy(mode=AdmissionMode.ENFORCE, cold_start=ColdStartMode.FAIL_OPEN)
        proposal = _make_proposal(
            target_files=["src/bernstein/core/security/audit_chain.py"],
        )
        decision = admission.evaluate(proposal)
        assert decision.admitted is False
        assert "Proposal targets constraint layer / locked file(s)" in decision.reason


class TestCodeownersDrift:
    """Manifest / CODEOWNERS tests."""

    def test_manifest_subset_of_codeowners(self) -> None:
        """Every path in CONSTRAINT_MANIFEST must be covered by CODEOWNERS."""
        assert CODEOWNERS_PATH.exists(), "CODEOWNERS file must exist"

        owner_patterns: list[str] = []
        for line in CODEOWNERS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            pattern = parts[0].lstrip("/")
            owner_patterns.append(pattern)

        # Each entry in manifest must match at least one codeowner pattern
        for entry in CONSTRAINT_MANIFEST:
            norm_entry = entry.lstrip("/")
            matched = any(norm_entry.startswith(p.rstrip("*")) or p == "*" for p in owner_patterns)
            assert matched, f"Constraint manifest entry {entry!r} not covered by any pattern in {owner_patterns}"

    def test_every_hash_locked_module_in_manifest(self) -> None:
        """Every module returned by resolve_locked_files / compute_invariants is in the manifest."""
        locked_files = resolve_locked_files(REPO_ROOT)
        assert len(locked_files) > 0

        for f in locked_files:
            assert is_constraint_path(f), f"Locked file {f} is not recognized by is_constraint_path"

        for f in LOCKED_FILES:
            assert is_constraint_path(f), f"LOCKED_FILES entry {f} is not recognized by is_constraint_path"


class TestWholeTreeConstraintGuard:
    """Whole-tree guard asserts that all constraint subtrees are fully locked."""

    def test_whole_tree_constraint_guard(self) -> None:
        """Scan constraint directories in src/ to ensure every module is locked."""
        subtrees = [
            REPO_ROOT / "src" / "bernstein" / "core" / "identity",
            REPO_ROOT / "src" / "bernstein" / "core" / "audit",
        ]

        for subtree in subtrees:
            if not subtree.exists():
                continue
            for py_file in subtree.rglob("*.py"):
                if py_file.name == "__pycache__":
                    continue
                rel_path = py_file.relative_to(REPO_ROOT).as_posix()
                assert is_constraint_path(rel_path), f"Module {rel_path} under constraint subtree is not locked!"


class TestNoBehaviorChangeOutsideManifest:
    """Verify proposals outside the manifest retain their standard behavior."""

    def test_proposals_outside_manifest_unchanged(self, tmp_path: Path) -> None:
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")

        # L0 config auto-approved
        l0 = _make_proposal(
            id="L0-safe", risk_level=RiskLevel.L0_CONFIG, target_files=[".sdd/config.yaml"], confidence=0.98
        )
        assert gate.route(l0).outcome == ApprovalOutcome.AUTO_APPROVED

        # L1 template auto-approved
        l1 = _make_proposal(
            id="L1-safe", risk_level=RiskLevel.L1_TEMPLATE, target_files=["templates/roles/backend.md"], confidence=0.98
        )
        assert gate.route(l1).outcome == ApprovalOutcome.AUTO_APPROVED

        # L2 logic human review
        l2 = _make_proposal(
            id="L2-safe", risk_level=RiskLevel.L2_LOGIC, target_files=[".sdd/config/routing.yaml"], confidence=0.98
        )
        assert gate.route(l2).outcome == ApprovalOutcome.HUMAN_REVIEW_4H

        # L3 structural blocked with standard L3 message
        l3 = _make_proposal(
            id="L3-safe",
            risk_level=RiskLevel.L3_STRUCTURAL,
            target_files=["src/bernstein/core/models.py"],
            confidence=0.98,
        )
        d3 = gate.route(l3)
        assert d3.outcome == ApprovalOutcome.BLOCKED
        assert "L3_STRUCTURAL changes require human-only review" in d3.reason
