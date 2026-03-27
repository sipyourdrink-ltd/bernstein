"""Tests for the risk-stratified ApprovalGate."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.evolution.gate import (
    ApprovalGate,
    ApprovalOutcome,
    RiskClassifier,
)
from bernstein.evolution.types import RiskLevel, UpgradeProposal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    id: str = "UPG-001",
    target_files: list[str] | None = None,
    confidence: float = 0.9,
    diff: str = "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new",
) -> UpgradeProposal:
    return UpgradeProposal(
        id=id,
        title="Test proposal",
        description="A test upgrade",
        risk_level=RiskLevel.L0_CONFIG,  # will be overridden by classifier
        target_files=target_files or [".sdd/config.yaml"],
        diff=diff,
        rationale="Testing",
        expected_impact="Better performance",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# RiskClassifier
# ---------------------------------------------------------------------------


class TestRiskClassifier:
    def setup_method(self) -> None:
        self.clf = RiskClassifier()

    def test_python_file_is_l3(self) -> None:
        assert self.clf.classify(["src/bernstein/core/models.py"]) == RiskLevel.L3_STRUCTURAL

    def test_pyproject_toml_is_l3(self) -> None:
        assert self.clf.classify(["pyproject.toml"]) == RiskLevel.L3_STRUCTURAL

    def test_template_file_is_l1(self) -> None:
        assert self.clf.classify(["templates/roles/backend.md"]) == RiskLevel.L1_TEMPLATE

    def test_sdd_config_yaml_is_l0(self) -> None:
        assert self.clf.classify([".sdd/config.yaml"]) == RiskLevel.L0_CONFIG

    def test_routing_yaml_is_l2(self) -> None:
        assert self.clf.classify([".sdd/config/routing.yaml"]) == RiskLevel.L2_LOGIC

    def test_orchestrator_yaml_is_l2(self) -> None:
        assert self.clf.classify([".sdd/config/orchestrator.yaml"]) == RiskLevel.L2_LOGIC

    def test_empty_files_defaults_to_l2(self) -> None:
        assert self.clf.classify([]) == RiskLevel.L2_LOGIC

    def test_highest_risk_wins(self) -> None:
        """Mix of config + python → L3 wins."""
        result = self.clf.classify([".sdd/config.yaml", "src/bernstein/cli/main.py"])
        assert result == RiskLevel.L3_STRUCTURAL

    def test_config_and_template_mix(self) -> None:
        """Config + template → L1 wins (higher than L0)."""
        result = self.clf.classify([".sdd/config.yaml", "templates/roles/qa.md"])
        assert result == RiskLevel.L1_TEMPLATE

    def test_unknown_file_defaults_to_l2(self) -> None:
        assert self.clf.classify(["some/unknown/file.txt"]) == RiskLevel.L2_LOGIC

    def test_multiple_config_files_stays_l0(self) -> None:
        files = [".sdd/config.yaml", ".sdd/other.yaml"]
        assert self.clf.classify(files) == RiskLevel.L0_CONFIG


# ---------------------------------------------------------------------------
# ApprovalGate.evaluate (legacy simple interface)
# ---------------------------------------------------------------------------


class TestApprovalGateEvaluate:
    def setup_method(self) -> None:
        self.gate = ApprovalGate()

    def test_l3_always_human_review(self) -> None:
        result = self.gate.evaluate(RiskLevel.L3_STRUCTURAL, 0.99)
        assert result == "human_review"

    def test_l2_always_human_review(self) -> None:
        result = self.gate.evaluate(RiskLevel.L2_LOGIC, 0.99)
        assert result == "human_review"

    def test_l1_requires_sandbox_first(self) -> None:
        result = self.gate.evaluate(RiskLevel.L1_TEMPLATE, 0.9)
        assert result == "sandbox_required"

    def test_l1_auto_approve_when_sandbox_passed_and_confidence_high(self) -> None:
        result = self.gate.evaluate(RiskLevel.L1_TEMPLATE, 0.9, sandbox_passed=True)
        assert result == "auto_approve"

    def test_l1_human_review_when_sandbox_failed(self) -> None:
        result = self.gate.evaluate(RiskLevel.L1_TEMPLATE, 0.9, sandbox_passed=False)
        assert result == "human_review"

    def test_l0_auto_approve_above_threshold(self) -> None:
        result = self.gate.evaluate(RiskLevel.L0_CONFIG, 0.9)
        assert result == "auto_approve"

    def test_l0_human_review_below_threshold(self) -> None:
        result = self.gate.evaluate(RiskLevel.L0_CONFIG, 0.5)
        assert result == "human_review"


# ---------------------------------------------------------------------------
# ApprovalGate.route — full confidence-threshold matrix
# ---------------------------------------------------------------------------


class TestApprovalGateRoute:
    def setup_method(self, tmp_path: Path | None = None) -> None:
        # Each test that needs a decisions_dir gets its own tmp_path
        pass

    def _gate(self, tmp_path: Path) -> ApprovalGate:
        return ApprovalGate(decisions_dir=tmp_path / ".sdd" / "evolution")

    def test_l0_high_confidence_auto_approved(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        proposal = _make_proposal(target_files=[".sdd/config.yaml"], confidence=0.97)
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.AUTO_APPROVED
        assert not decision.requires_human
        assert decision.risk_level == RiskLevel.L0_CONFIG

    def test_l0_mid_confidence_auto_audit(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        proposal = _make_proposal(target_files=[".sdd/config.yaml"], confidence=0.88)
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.AUTO_APPROVED_AUDIT
        assert not decision.requires_human

    def test_l0_low_mid_confidence_human_4h(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        proposal = _make_proposal(target_files=[".sdd/config.yaml"], confidence=0.75)
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.HUMAN_REVIEW_4H
        assert decision.requires_human

    def test_l0_very_low_confidence_immediate(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        proposal = _make_proposal(target_files=[".sdd/config.yaml"], confidence=0.5)
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE
        assert decision.requires_human

    def test_l1_high_confidence_auto_approved(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        proposal = _make_proposal(target_files=["templates/roles/backend.md"], confidence=0.96)
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.AUTO_APPROVED
        assert decision.risk_level == RiskLevel.L1_TEMPLATE

    def test_l2_always_human_review_4h_when_confident(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        proposal = _make_proposal(target_files=[".sdd/config/routing.yaml"], confidence=0.99)
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.HUMAN_REVIEW_4H
        assert decision.requires_human
        assert decision.risk_level == RiskLevel.L2_LOGIC

    def test_l2_low_confidence_immediate_review(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        proposal = _make_proposal(target_files=[".sdd/config/routing.yaml"], confidence=0.5)
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE

    def test_l3_always_blocked(self, tmp_path: Path) -> None:
        gate = self._gate(tmp_path)
        proposal = _make_proposal(target_files=["src/bernstein/core/models.py"], confidence=0.99)
        decision = gate.route(proposal)
        assert decision.outcome == ApprovalOutcome.BLOCKED
        assert decision.requires_human
        assert decision.risk_level == RiskLevel.L3_STRUCTURAL


# ---------------------------------------------------------------------------
# Decision logging
# ---------------------------------------------------------------------------


class TestDecisionLogging:
    def test_decision_logged_to_jsonl(self, tmp_path: Path) -> None:
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        proposal = _make_proposal(target_files=[".sdd/config.yaml"], confidence=0.97)
        gate.route(proposal)

        decisions_path = tmp_path / "evolution" / "decisions.jsonl"
        assert decisions_path.exists()
        lines = [l for l in decisions_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["proposal_id"] == "UPG-001"
        assert data["outcome"] == "auto_approved"

    def test_multiple_proposals_appended(self, tmp_path: Path) -> None:
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        gate.route(_make_proposal(id="P-001", target_files=[".sdd/config.yaml"], confidence=0.97))
        gate.route(_make_proposal(id="P-002", target_files=[".sdd/config.yaml"], confidence=0.5))

        lines = (tmp_path / "evolution" / "decisions.jsonl").read_text().splitlines()
        assert len(lines) == 2

    def test_no_logging_without_decisions_dir(self) -> None:
        """Gate without decisions_dir should not crash."""
        gate = ApprovalGate()
        proposal = _make_proposal(confidence=0.97)
        decision = gate.route(proposal)
        assert decision is not None


# ---------------------------------------------------------------------------
# Pending decisions and manual approval
# ---------------------------------------------------------------------------


class TestPendingAndApproval:
    def test_get_pending_returns_human_review_decisions(self, tmp_path: Path) -> None:
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        gate.route(_make_proposal(id="P-human", target_files=[".sdd/config.yaml"], confidence=0.5))
        gate.route(_make_proposal(id="P-auto", target_files=[".sdd/config.yaml"], confidence=0.97))

        pending = gate.get_pending_decisions()
        ids = {d.proposal_id for d in pending}
        assert "P-human" in ids
        assert "P-auto" not in ids

    def test_approve_clears_pending(self, tmp_path: Path) -> None:
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        gate.route(_make_proposal(id="P-pending", target_files=[".sdd/config.yaml"], confidence=0.5))

        assert len(gate.get_pending_decisions()) == 1

        approved = gate.approve("P-pending", reviewer="sasha")
        assert approved is not None
        assert approved.reviewer == "sasha"
        assert not approved.requires_human

        # After approval, should not appear in pending
        pending = gate.get_pending_decisions()
        ids = {d.proposal_id for d in pending}
        assert "P-pending" not in ids

    def test_approve_returns_none_for_unknown(self, tmp_path: Path) -> None:
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        result = gate.approve("nonexistent")
        assert result is None

    def test_blocked_l3_appears_in_pending(self, tmp_path: Path) -> None:
        gate = ApprovalGate(decisions_dir=tmp_path / "evolution")
        gate.route(_make_proposal(id="L3-1", target_files=["src/bernstein/core/models.py"]))

        pending = gate.get_pending_decisions()
        assert any(d.proposal_id == "L3-1" for d in pending)
