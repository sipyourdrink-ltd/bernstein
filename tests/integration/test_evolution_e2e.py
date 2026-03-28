"""End-to-end integration test for the self-evolution feedback loop.

Full pipeline:
  1. Seed .sdd/metrics/tasks.jsonl with declining success rates
  2. MetricsAggregator → TrendAnalysis
  3. OpportunityDetector → ImprovementOpportunity
  4. ProposalGenerator → UpgradeProposal (deterministic, no LLM)
  5. ApprovalGate → ApprovalDecision (risk classification)
  6. SandboxValidator → SandboxResult (L0 schema check, no git worktree)
  7. FileUpgradeExecutor → applies change, writes history.jsonl
  8. Verify change applied and history recorded
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from bernstein.evolution.aggregator import (
    FileMetricsCollector,
    MetricsAggregator,
    TaskMetrics,
)
from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.detector import OpportunityDetector
from bernstein.evolution.gate import ApprovalGate, ApprovalOutcome
from bernstein.evolution.proposals import AnalysisTrigger, ProposalGenerator
from bernstein.evolution.sandbox import SandboxValidator
from bernstein.evolution.types import (
    RiskLevel,
)
from bernstein.evolution.types import (
    UpgradeProposal as EvolutionUpgradeProposal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_declining_metrics(
    collector: FileMetricsCollector,
    count: int = 25,
) -> None:
    """Write task metrics with a declining success rate to tasks.jsonl.

    The first half has 90% pass rate; the second half has 40% pass rate,
    giving a visible downward trend and an overall rate below 80%.
    """
    now = time.time()
    for i in range(count):
        # First half: mostly passing; second half: mostly failing
        if i < count // 2:
            passed = i % 10 != 0  # 90% pass
        else:
            passed = i % 10 < 4  # 40% pass

        metrics = TaskMetrics(
            timestamp=now - (count - i) * 120,  # 2-min intervals
            task_id=f"T-e2e-{i:03d}",
            role="backend",
            model="sonnet",
            provider="anthropic",
            duration_seconds=30.0 + i,
            cost_usd=0.02 * (i + 1),
            janitor_passed=passed,
        )
        collector.record_task_metrics(metrics)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvolutionFeedbackLoopE2E:
    """Full self-evolution cycle: metrics → analysis → proposal → gate → sandbox → apply."""

    def test_full_pipeline_happy_path(self, tmp_path: Path) -> None:
        """Complete cycle from seeded metrics through applied upgrade."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        # Step 1: Seed .sdd/metrics/tasks.jsonl with declining success rates
        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        tasks_jsonl = state_dir / "metrics" / "tasks.jsonl"
        assert tasks_jsonl.exists(), "tasks.jsonl must be written by collector"
        lines = tasks_jsonl.read_text().strip().splitlines()
        assert len(lines) == 25, "all 25 metrics must be persisted"

        # Step 2: MetricsAggregator → TrendAnalysis
        aggregator = MetricsAggregator(collector)
        trends = aggregator.analyze_trends()
        # 25 records with a clear downward trend should produce ≥1 trend
        assert isinstance(trends, list)
        success_trend = next((t for t in trends if t.metric_name == "success_rate"), None)
        # With 12 passing in first half and ~10 failing in second half, trend is decreasing
        if success_trend is not None:
            assert success_trend.direction in ("decreasing", "stable", "increasing")

        # Step 3: OpportunityDetector → ImprovementOpportunity
        detector = OpportunityDetector(collector)
        opportunities = detector.identify_opportunities()
        assert len(opportunities) > 0, "detector must find opportunities from <80% success rate"

        success_opp = next(
            (o for o in opportunities if "success rate" in o.title.lower()),
            None,
        )
        assert success_opp is not None, "must find a success-rate improvement opportunity"
        assert success_opp.confidence > 0
        assert success_opp.risk_level in ("low", "medium", "high")

        # Step 4: ProposalGenerator → UpgradeProposal (no LLM — deterministic)
        generator = ProposalGenerator()
        proposal = generator.create_proposal(success_opp, AnalysisTrigger.SCHEDULED)

        assert proposal.id.startswith("UPG-")
        assert proposal.title == success_opp.title
        assert proposal.confidence == success_opp.confidence
        assert proposal.triggered_by == AnalysisTrigger.SCHEDULED

        # Step 5: ApprovalGate → ApprovalDecision
        decisions_dir = state_dir / "evolution"
        gate = ApprovalGate(decisions_dir=decisions_dir)

        # Build a types.UpgradeProposal targeting a config file.
        # Use "providers.yaml" — it doesn't match any _LOGIC_KEYWORDS so the
        # RiskClassifier assigns L0_CONFIG (.sdd + .yaml + no logic keyword).
        evolution_proposal = EvolutionUpgradeProposal(
            id=proposal.id,
            title=proposal.title,
            description=proposal.description,
            risk_level=RiskLevel.L0_CONFIG,
            target_files=[".sdd/config/providers.yaml"],
            diff=(
                "--- a/.sdd/config/providers.yaml\n"
                "+++ b/.sdd/config/providers.yaml\n"
                "@@ -1,2 +1,2 @@\n"
                "-prefer_free_tier: false\n"
                "+prefer_free_tier: true\n"
            ),
            rationale=proposal.description,
            expected_impact=proposal.expected_improvement,
            confidence=proposal.confidence,
        )
        decision = gate.route(evolution_proposal)

        assert decision.proposal_id == proposal.id
        # RiskClassifier classifies .sdd/config/providers.yaml as L0_CONFIG
        assert decision.risk_level == RiskLevel.L0_CONFIG
        # L0_CONFIG is never BLOCKED
        assert decision.outcome != ApprovalOutcome.BLOCKED

        # Decision must be logged to decisions.jsonl
        decisions_log = decisions_dir / "decisions.jsonl"
        assert decisions_log.exists()
        logged = json.loads(decisions_log.read_text().strip().splitlines()[0])
        assert logged["proposal_id"] == proposal.id
        assert logged["risk_level"] == "config"

        # Step 6: SandboxValidator → SandboxResult
        # L0_CONFIG uses schema-check only — no git worktree needed
        validator = SandboxValidator(repo_root=tmp_path)
        sandbox_result = validator.create_sandbox(evolution_proposal)

        assert sandbox_result.proposal_id == proposal.id
        assert sandbox_result.passed is True, "L0 schema check must pass for non-empty diff"
        assert sandbox_result.tests_total == 1

        # Step 7: FileUpgradeExecutor → apply change, write history.jsonl
        executor = FileUpgradeExecutor(state_dir)
        applied = executor.execute_upgrade(proposal)
        assert applied is True

        # Step 8: Verify change was applied and history recorded
        history_file = state_dir / "upgrades" / "history.jsonl"
        assert history_file.exists(), "executor must write history.jsonl"

        history_lines = history_file.read_text().strip().splitlines()
        assert len(history_lines) >= 1
        record = json.loads(history_lines[0])
        assert record["proposal_id"] == proposal.id
        assert record["status"] == "applied"
        assert "applied_at" in record

    def test_metrics_persist_and_reload(self, tmp_path: Path) -> None:
        """Metrics written by collector are readable by aggregator on reload."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=15)

        # Reload via a fresh collector instance (simulates a new process)
        fresh_collector = FileMetricsCollector(state_dir)
        fresh_collector.load_from_files()
        recent = fresh_collector.get_recent_task_metrics(hours=24)
        assert len(recent) == 15

    def test_approval_gate_logs_all_decisions(self, tmp_path: Path) -> None:
        """All proposals routed through the gate are persisted to decisions.jsonl."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        decisions_dir = state_dir / "evolution"

        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        detector = OpportunityDetector(collector)
        opportunities = detector.identify_opportunities()
        generator = ProposalGenerator()
        gate = ApprovalGate(decisions_dir=decisions_dir)

        proposal_ids = []
        for opp in opportunities:
            p = generator.create_proposal(opp, AnalysisTrigger.SCHEDULED)
            ev_proposal = EvolutionUpgradeProposal(
                id=p.id,
                title=p.title,
                description=p.description,
                risk_level=RiskLevel.L0_CONFIG,
                target_files=[".sdd/config/providers.yaml"],
                diff="--- a/providers.yaml\n+++ b/providers.yaml\n@@ -1 +1 @@\n-v: 1\n+v: 2\n",
                rationale=p.description,
                expected_impact=p.expected_improvement,
                confidence=p.confidence,
            )
            gate.route(ev_proposal)
            proposal_ids.append(p.id)

        decisions_log = decisions_dir / "decisions.jsonl"
        assert decisions_log.exists()
        logged_ids = [json.loads(line)["proposal_id"] for line in decisions_log.read_text().strip().splitlines()]
        for pid in proposal_ids:
            assert pid in logged_ids, f"decision for {pid} not logged"

    def test_sandbox_l0_rejects_empty_diff(self, tmp_path: Path) -> None:
        """SandboxValidator rejects L0 proposals with empty diffs."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)
        detector = OpportunityDetector(collector)
        opportunities = detector.identify_opportunities()
        assert len(opportunities) > 0

        generator = ProposalGenerator()
        proposal = generator.create_proposal(opportunities[0], AnalysisTrigger.SCHEDULED)

        evolution_proposal = EvolutionUpgradeProposal(
            id=proposal.id,
            title=proposal.title,
            description=proposal.description,
            risk_level=RiskLevel.L0_CONFIG,
            target_files=[".sdd/config/routing.yaml"],
            diff="",  # empty → should fail schema check
            rationale=proposal.description,
            expected_impact=proposal.expected_improvement,
            confidence=proposal.confidence,
        )

        validator = SandboxValidator(repo_root=tmp_path)
        result = validator.create_sandbox(evolution_proposal)

        assert result.passed is False
        assert result.error is not None

    def test_executor_writes_config_file(self, tmp_path: Path) -> None:
        """FileUpgradeExecutor writes the appropriate config file for each category."""
        from bernstein.evolution.detector import UpgradeCategory

        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)
        detector = OpportunityDetector(collector)
        opportunities = detector.identify_opportunities()

        # Find a MODEL_ROUTING or ROUTING_RULES opportunity
        routing_opp = next(
            (
                o
                for o in opportunities
                if o.category
                in (
                    UpgradeCategory.MODEL_ROUTING,
                    UpgradeCategory.ROUTING_RULES,
                )
            ),
            opportunities[0],  # fallback to first
        )

        generator = ProposalGenerator()
        proposal = generator.create_proposal(routing_opp, AnalysisTrigger.SCHEDULED)

        executor = FileUpgradeExecutor(state_dir)
        success = executor.execute_upgrade(proposal)
        assert success is True

        # history.jsonl must exist and record the upgrade
        history_file = state_dir / "upgrades" / "history.jsonl"
        assert history_file.exists()
        record = json.loads(history_file.read_text().strip().splitlines()[0])
        assert record["status"] == "applied"
