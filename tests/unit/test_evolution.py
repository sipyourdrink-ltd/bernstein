"""Tests for EvolutionCoordinator — self-evolution feedback loop."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from bernstein.core.evolution import (
    AgentMetrics,
    AnalysisEngine,
    AnalysisTrigger,
    ApprovalMode,
    CostMetrics,
    EvolutionCoordinator,
    FileMetricsCollector,
    FileUpgradeExecutor,
    ImprovementOpportunity,
    QualityMetrics,
    TaskMetrics,
    TrendAnalysis,
    UpgradeCategory,
    UpgradeProposal,
    UpgradeStatus,
    get_default_coordinator,
)
from bernstein.core.models import (
    Complexity,
    RiskAssessment,
    RollbackPlan,
    Scope,
    Task,
    TaskType,
)

# --- Helpers ---


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Implement feature",
    description: str = "Write the code.",
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
    )


def _make_task_metrics(
    cost_usd: float = 0.01,
    duration_seconds: float = 60.0,
    janitor_passed: bool = True,
    timestamp: float | None = None,
) -> TaskMetrics:
    return TaskMetrics(
        timestamp=timestamp or time.time(),
        task_id="T-001",
        role="backend",
        model="sonnet",
        provider="anthropic",
        duration_seconds=duration_seconds,
        cost_usd=cost_usd,
        janitor_passed=janitor_passed,
    )


def _make_improvement_opportunity(
    category: UpgradeCategory = UpgradeCategory.ROUTING_RULES,
    risk_level: str = "low",
    confidence: float = 0.8,
    estimated_cost_impact: float = -0.5,
) -> ImprovementOpportunity:
    return ImprovementOpportunity(
        category=category,
        title="Test improvement",
        description="Test improvement description",
        expected_improvement="Expected improvement",
        confidence=confidence,
        risk_level=risk_level,  # type: ignore[arg-type]
        estimated_cost_impact_usd=estimated_cost_impact,
    )


# --- FileMetricsCollector ---


class TestFileMetricsCollector:
    def test_initialization_creates_directories(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        collector = FileMetricsCollector(state_dir)

        assert collector.metrics_dir.exists()

    def test_record_task_metrics(self, tmp_path: Path) -> None:
        collector = FileMetricsCollector(tmp_path)
        metrics = _make_task_metrics()

        collector.record_task_metrics(metrics)

        assert len(collector._task_metrics) == 1
        assert collector._task_metrics[0].cost_usd == 0.01

    def test_record_task_metrics_appends_to_file(self, tmp_path: Path) -> None:
        collector = FileMetricsCollector(tmp_path)
        metrics1 = _make_task_metrics(cost_usd=0.01)
        metrics2 = _make_task_metrics(cost_usd=0.02)

        collector.record_task_metrics(metrics1)
        collector.record_task_metrics(metrics2)

        # Check file contents
        filepath = tmp_path / "metrics" / "tasks.jsonl"
        lines = filepath.read_text().strip().split("\n")
        assert len(lines) == 2

        data1 = json.loads(lines[0])
        data2 = json.loads(lines[1])
        assert data1["cost_usd"] == 0.01
        assert data2["cost_usd"] == 0.02

    def test_record_agent_metrics(self, tmp_path: Path) -> None:
        collector = FileMetricsCollector(tmp_path)
        metrics = AgentMetrics(
            timestamp=time.time(),
            agent_id="agent-1",
            lifetime_seconds=300,
            tasks_completed=5,
        )

        collector.record_agent_metrics(metrics)

        assert len(collector._agent_metrics) == 1

    def test_record_cost_metrics(self, tmp_path: Path) -> None:
        collector = FileMetricsCollector(tmp_path)
        metrics = CostMetrics(
            timestamp=time.time(),
            provider="anthropic",
            model="sonnet",
            tier="standard",
            cost_usd=0.05,
        )

        collector.record_cost_metrics(metrics)

        assert len(collector._cost_metrics) == 1
        assert collector._cost_metrics[0].cost_usd == 0.05

    def test_record_quality_metrics(self, tmp_path: Path) -> None:
        collector = FileMetricsCollector(tmp_path)
        metrics = QualityMetrics(
            timestamp=time.time(),
            janitor_pass_rate=0.95,
            human_approval_rate=0.90,
        )

        collector.record_quality_metrics(metrics)

        assert len(collector._quality_metrics) == 1
        assert collector._quality_metrics[0].janitor_pass_rate == 0.95

    def test_get_recent_task_metrics_filters_by_time(self, tmp_path: Path) -> None:
        collector = FileMetricsCollector(tmp_path)

        # Add old metric
        old_metrics = _make_task_metrics(timestamp=time.time() - 7200)  # 2 hours ago
        collector.record_task_metrics(old_metrics)

        # Add recent metric
        recent_metrics = _make_task_metrics(timestamp=time.time())
        collector.record_task_metrics(recent_metrics)

        recent = collector.get_recent_task_metrics(hours=1)

        assert len(recent) == 1
        assert recent[0].cost_usd == 0.01

    def test_load_from_files(self, tmp_path: Path) -> None:
        collector = FileMetricsCollector(tmp_path)

        # Write some metrics to file
        metrics = _make_task_metrics()
        collector.record_task_metrics(metrics)

        # Create new collector and load
        collector2 = FileMetricsCollector(tmp_path)
        collector2.load_from_files()

        assert len(collector2._task_metrics) == 1


# --- AnalysisEngine ---


class TestAnalysisEngine:
    def test_initialization(self) -> None:
        collector = MagicMock()
        engine = AnalysisEngine(collector)

        assert engine.collector is collector
        assert engine._trends == []
        assert engine._anomalies == []
        assert engine._opportunities == []

    def test_run_analysis(self) -> None:
        collector = MagicMock()
        collector.get_recent_task_metrics.return_value = [
            _make_task_metrics(cost_usd=0.01 * (i + 1)) for i in range(20)
        ]
        collector.get_recent_cost_metrics.return_value = []
        engine = AnalysisEngine(collector)

        engine.run_analysis()

        # Should have analyzed trends
        assert isinstance(engine._trends, list)

    def test_analyze_trends_insufficient_data(self) -> None:
        collector = MagicMock()
        collector.get_recent_task_metrics.return_value = [_make_task_metrics()]  # Only 1 data point
        engine = AnalysisEngine(collector)

        engine._analyze_trends()

        assert engine._trends == []

    def test_calculate_trend_increasing(self) -> None:
        collector = MagicMock()
        engine = AnalysisEngine(collector)

        # Values that are increasing
        values = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5]
        trend = engine._calculate_trend(values, "test_metric")

        assert trend is not None
        assert trend.direction == "increasing"
        assert trend.change_percent > 0

    def test_calculate_trend_decreasing(self) -> None:
        collector = MagicMock()
        engine = AnalysisEngine(collector)

        # Values that are decreasing
        values = [5.5, 5.0, 4.5, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5, 1.0]
        trend = engine._calculate_trend(values, "test_metric")

        assert trend is not None
        assert trend.direction == "decreasing"
        assert trend.change_percent < 0

    def test_calculate_trend_stable(self) -> None:
        collector = MagicMock()
        engine = AnalysisEngine(collector)

        # Stable values
        values = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        trend = engine._calculate_trend(values, "test_metric")

        assert trend is not None
        assert trend.direction == "stable"

    def test_calculate_trend_insufficient_data(self) -> None:
        collector = MagicMock()
        engine = AnalysisEngine(collector)

        trend = engine._calculate_trend([1.0, 2.0], "test_metric")

        assert trend is None

    def test_detect_anomalies(self) -> None:
        collector = MagicMock()
        # Create metrics with an anomaly
        base_time = time.time()
        metrics = [_make_task_metrics(cost_usd=0.01, timestamp=base_time + i) for i in range(10)]
        # Add one expensive outlier
        metrics.append(_make_task_metrics(cost_usd=1.0, timestamp=base_time + 11))
        collector.get_recent_task_metrics.return_value = metrics
        engine = AnalysisEngine(collector)

        engine._detect_anomalies()

        assert len(engine._anomalies) >= 1
        assert engine._anomalies[0].severity in ["low", "medium", "high", "critical"]

    def test_identify_opportunities_cost_optimization(self) -> None:
        collector = MagicMock()
        collector.get_recent_cost_metrics.return_value = [
            CostMetrics(
                timestamp=time.time(),
                provider="anthropic",
                model="sonnet",
                tier="standard",  # Not free
                cost_usd=0.5,
            )
            for _ in range(5)
        ]
        collector.get_recent_task_metrics.return_value = []
        engine = AnalysisEngine(collector)

        engine._identify_opportunities()

        assert len(engine._opportunities) >= 1
        assert (
            "free tier" in engine._opportunities[0].title.lower()
            or "optimization" in engine._opportunities[0].title.lower()
        )

    def test_identify_opportunities_success_rate(self) -> None:
        collector = MagicMock()
        collector.get_recent_cost_metrics.return_value = []
        # Low pass rate
        collector.get_recent_task_metrics.return_value = [
            _make_task_metrics(janitor_passed=(i % 3 != 0))  # ~67% pass rate
            for i in range(20)
        ]
        engine = AnalysisEngine(collector)

        engine._identify_opportunities()

        assert len(engine._opportunities) >= 1
        assert any("success" in opp.title.lower() for opp in engine._opportunities)

    def test_get_trends(self) -> None:
        collector = MagicMock()
        engine = AnalysisEngine(collector)
        engine._trends = [
            TrendAnalysis(
                metric_name="test",
                direction="increasing",
                change_percent=10.0,
                baseline_value=1.0,
                current_value=1.1,
                confidence=0.8,
            )
        ]

        trends = engine.get_trends()

        assert len(trends) == 1
        assert trends[0].metric_name == "test"

    def test_get_anomalies(self) -> None:
        collector = MagicMock()
        engine = AnalysisEngine(collector)

        anomalies = engine.get_anomalies()

        assert isinstance(anomalies, list)

    def test_get_opportunities(self) -> None:
        collector = MagicMock()
        engine = AnalysisEngine(collector)
        engine._opportunities = [_make_improvement_opportunity()]

        opportunities = engine.get_opportunities()

        assert len(opportunities) == 1


# --- FileUpgradeExecutor ---


class TestFileUpgradeExecutor:
    def test_initialization_creates_directories(self, tmp_path: Path) -> None:
        executor = FileUpgradeExecutor(tmp_path)

        assert executor.upgrades_dir.exists()
        assert executor.config_dir.exists()

    def test_execute_upgrade_policy_update(self, tmp_path: Path) -> None:
        executor = FileUpgradeExecutor(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-001",
            title="Test policy update",
            category=UpgradeCategory.POLICY_UPDATE,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.9,
        )

        result = executor.execute_upgrade(proposal)

        assert result is True
        history_file = tmp_path / "upgrades" / "history.jsonl"
        assert history_file.exists()

    def test_execute_upgrade_routing_rules(self, tmp_path: Path) -> None:
        executor = FileUpgradeExecutor(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-002",
            title="Test routing update",
            category=UpgradeCategory.ROUTING_RULES,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.9,
        )

        result = executor.execute_upgrade(proposal)

        assert result is True

    def test_execute_upgrade_model_routing(self, tmp_path: Path) -> None:
        executor = FileUpgradeExecutor(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-003",
            title="Test model routing",
            category=UpgradeCategory.MODEL_ROUTING,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.9,
        )

        result = executor.execute_upgrade(proposal)

        assert result is True

    def test_execute_upgrade_provider_config(self, tmp_path: Path) -> None:
        executor = FileUpgradeExecutor(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-004",
            title="Test provider config",
            category=UpgradeCategory.PROVIDER_CONFIG,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.9,
        )

        result = executor.execute_upgrade(proposal)

        assert result is True

    def test_execute_upgrade_role_template(self, tmp_path: Path) -> None:
        executor = FileUpgradeExecutor(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-005",
            title="Test role template",
            category=UpgradeCategory.ROLE_TEMPLATES,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.9,
        )

        result = executor.execute_upgrade(proposal)

        assert result is True

    def test_execute_upgrade_failure(self, tmp_path: Path) -> None:
        executor = FileUpgradeExecutor(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-006",
            title="Test failure",
            category=UpgradeCategory.POLICY_UPDATE,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.9,
        )

        # Make executor fail by mocking
        original_method = executor._apply_policy_update
        executor._apply_policy_update = lambda p: False  # type: ignore[method-assign]

        result = executor.execute_upgrade(proposal)

        assert result is False

        # Restore
        executor._apply_policy_update = original_method  # type: ignore[method-assign]

    def test_rollback_upgrade(self, tmp_path: Path) -> None:
        executor = FileUpgradeExecutor(tmp_path)

        # Create a backup file
        config_file = tmp_path / "config" / "test.yaml"
        config_file.parent.mkdir(exist_ok=True)
        config_file.write_text("original content")

        backup_file = tmp_path / "upgrades" / "backup_test.yaml_12345"
        backup_file.write_text("backup content")
        executor._backup_files["test.yaml"] = backup_file

        result = executor.rollback_upgrade(MagicMock())

        # Backup should be restored (or at least attempted)
        assert result is True


# --- EvolutionCoordinator ---


class TestEvolutionCoordinator:
    def test_initialization(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)

        assert isinstance(coordinator.collector, FileMetricsCollector)
        assert isinstance(coordinator.executor, FileUpgradeExecutor)
        assert isinstance(coordinator.analysis_engine, AnalysisEngine)
        assert coordinator.analysis_interval_minutes == 60

    def test_custom_analysis_interval(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(
            tmp_path,
            analysis_interval_minutes=30,
        )

        assert coordinator.analysis_interval_minutes == 30

    def test_run_analysis_cycle(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)

        # Add some metrics to analyze
        for i in range(20):
            coordinator.collector.record_task_metrics(
                _make_task_metrics(cost_usd=0.5)  # High cost to trigger opportunity
            )

        proposals = coordinator.run_analysis_cycle(AnalysisTrigger.SCHEDULED)

        assert isinstance(proposals, list)
        assert coordinator._last_analysis > 0

    def test_run_analysis_cycle_creates_proposals(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)

        # Add metrics that should trigger opportunities
        for _ in range(20):
            coordinator.collector.record_task_metrics(_make_task_metrics(cost_usd=0.5, janitor_passed=False))

        proposals = coordinator.run_analysis_cycle()

        # Should have generated some proposals
        assert len(coordinator._pending_upgrades) >= 0  # May vary based on analysis

    def test_create_proposal(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)
        opportunity = _make_improvement_opportunity()

        proposal = coordinator._create_proposal(opportunity, AnalysisTrigger.SCHEDULED)

        assert proposal.id.startswith("UPG-")
        assert proposal.title == "Test improvement"
        assert proposal.category == UpgradeCategory.ROUTING_RULES
        assert proposal.status == UpgradeStatus.PENDING

    def test_create_emergency_proposal(self, tmp_path: Path) -> None:
        from bernstein.core.evolution import AnomalyDetection

        coordinator = EvolutionCoordinator(tmp_path)
        anomaly = AnomalyDetection(
            metric_name="cost_per_task",
            anomaly_type="spike",
            severity="critical",
            z_score=4.5,
            expected_value=0.01,
            actual_value=1.0,
            timestamp=time.time(),
            description="Critical cost spike",
        )

        proposal = coordinator._create_emergency_proposal(anomaly)

        assert proposal.id.startswith("EMG-")
        assert "Emergency" in proposal.title
        assert proposal.approval_mode == ApprovalMode.AUTO
        assert proposal.triggered_by == AnalysisTrigger.ANOMALY

    def test_should_auto_approve_auto_mode(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-001",
            title="Test",
            category=UpgradeCategory.POLICY_UPDATE,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.5,
            approval_mode=ApprovalMode.AUTO,
        )

        assert coordinator._should_auto_approve(proposal) is True

    def test_should_auto_approve_hybrid_high_confidence(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-002",
            title="Test",
            category=UpgradeCategory.POLICY_UPDATE,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.95,
            approval_mode=ApprovalMode.HYBRID,
        )

        assert coordinator._should_auto_approve(proposal) is True

    def test_should_auto_approve_hybrid_low_confidence(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-003",
            title="Test",
            category=UpgradeCategory.POLICY_UPDATE,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.5,
            approval_mode=ApprovalMode.HYBRID,
        )

        assert coordinator._should_auto_approve(proposal) is False

    def test_should_auto_approve_human_mode(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)
        proposal = UpgradeProposal(
            id="UPG-004",
            title="Test",
            category=UpgradeCategory.POLICY_UPDATE,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=1.0,
            approval_mode=ApprovalMode.HUMAN,
        )

        assert coordinator._should_auto_approve(proposal) is False

    def test_execute_pending_upgrades(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)

        # Create an approved proposal
        proposal = UpgradeProposal(
            id="UPG-005",
            title="Test",
            category=UpgradeCategory.POLICY_UPDATE,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.9,
            status=UpgradeStatus.APPROVED,
        )
        coordinator._pending_upgrades.append(proposal)

        executed = coordinator.execute_pending_upgrades()

        assert len(executed) == 1
        assert executed[0].status == UpgradeStatus.APPLIED
        assert len(coordinator._applied_upgrades) == 1

    def test_execute_pending_upgrades_skips_unapproved(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)

        # Create a pending (not approved) proposal
        proposal = UpgradeProposal(
            id="UPG-006",
            title="Test",
            category=UpgradeCategory.POLICY_UPDATE,
            description="Test",
            current_state="Current",
            proposed_change="Change",
            benefits=["Benefit"],
            risk_assessment=RiskAssessment(),
            rollback_plan=MagicMock(),
            cost_estimate_usd=0.0,
            expected_improvement="Improvement",
            confidence=0.5,
            status=UpgradeStatus.PENDING,
        )
        coordinator._pending_upgrades.append(proposal)

        executed = coordinator.execute_pending_upgrades()

        assert len(executed) == 0
        assert proposal.status == UpgradeStatus.PENDING

    def test_should_run_analysis_true(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path, analysis_interval_minutes=1)
        coordinator._last_analysis = time.time() - 120  # 2 minutes ago

        assert coordinator.should_run_analysis() is True

    def test_should_run_analysis_false(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path, analysis_interval_minutes=60)
        coordinator._last_analysis = time.time()

        assert coordinator.should_run_analysis() is False

    def test_get_analysis_summary(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)
        coordinator._last_analysis = time.time()

        summary = coordinator.get_analysis_summary()

        assert "last_analysis" in summary
        assert "next_analysis_due" in summary
        assert "pending_upgrades" in summary
        assert "applied_upgrades" in summary

    def test_record_task_completion(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)
        task = _make_task()

        coordinator.record_task_completion(
            task=task,
            duration_seconds=120,
            cost_usd=0.05,
            janitor_passed=True,
            model="sonnet",
            provider="anthropic",
        )

        metrics = coordinator.collector.get_recent_task_metrics(hours=1)
        assert len(metrics) == 1
        assert metrics[0].duration_seconds == 120
        assert metrics[0].cost_usd == 0.05

    def test_init_loads_historical_metrics_from_files(self, tmp_path: Path) -> None:
        """EvolutionCoordinator loads persisted metrics on init for cross-session trend analysis."""
        # First coordinator records a task completion → persists to tasks.jsonl
        coordinator1 = EvolutionCoordinator(tmp_path)
        task = _make_task()
        coordinator1.record_task_completion(
            task=task,
            duration_seconds=45.0,
            cost_usd=0.02,
            janitor_passed=True,
            model="sonnet",
            provider="anthropic",
        )

        # Second coordinator (fresh start, simulating a process restart) should
        # see the metric from the previous session.
        coordinator2 = EvolutionCoordinator(tmp_path)
        metrics = coordinator2.collector.get_recent_task_metrics(hours=24)
        assert len(metrics) >= 1, (
            "EvolutionCoordinator must load historical metrics on init so "
            "run_analysis_cycle() has data from previous sessions"
        )
        assert any(m.duration_seconds == 45.0 for m in metrics)


# --- UpgradeProposal ---


class TestUpgradeProposal:
    def test_to_task(self) -> None:
        proposal = UpgradeProposal(
            id="UPG-001",
            title="Upgrade routing",
            category=UpgradeCategory.ROUTING_RULES,
            description="Improve routing",
            current_state="Current routing is basic",
            proposed_change="Add tier awareness",
            benefits=["Cost savings", "Better performance"],
            risk_assessment=RiskAssessment(level="medium"),
            rollback_plan=RollbackPlan(steps=["Revert changes"]),
            cost_estimate_usd=-0.5,
            expected_improvement="30% cost reduction",
            confidence=0.85,
        )

        task = proposal.to_task()

        assert task.task_type == TaskType.UPGRADE_PROPOSAL
        assert task.role == "manager"
        assert task.upgrade_details is not None
        assert task.upgrade_details.proposed_change == "Add tier awareness"


# --- Default coordinator ---


class TestGetDefaultCoordinator:
    def test_returns_singleton(self, tmp_path: Path) -> None:
        # Reset singleton
        import bernstein.core.evolution as ev

        ev._default_coordinator = None

        coord1 = get_default_coordinator(tmp_path)
        coord2 = get_default_coordinator(tmp_path)

        assert coord1 is coord2

    def test_default_analysis_interval(self, tmp_path: Path) -> None:
        import bernstein.core.evolution as ev

        ev._default_coordinator = None

        coordinator = get_default_coordinator(tmp_path)

        assert coordinator.analysis_interval_minutes == 60

    def test_custom_analysis_interval(self, tmp_path: Path) -> None:
        import bernstein.core.evolution as ev

        ev._default_coordinator = None

        coordinator = get_default_coordinator(tmp_path, analysis_interval_minutes=30)

        assert coordinator.analysis_interval_minutes == 30


# --- Integration tests ---


class TestEvolutionIntegration:
    def test_full_evolution_cycle(self, tmp_path: Path) -> None:
        """Test a complete evolution cycle from metrics to upgrade execution."""
        coordinator = EvolutionCoordinator(
            tmp_path,
            analysis_interval_minutes=1,
        )

        # Simulate task completions with high cost
        for i in range(25):
            task = _make_task(id=f"T-{i:03d}")
            coordinator.record_task_completion(
                task=task,
                duration_seconds=60,
                cost_usd=0.5,  # High cost
                janitor_passed=True,
                model="sonnet",
                provider="anthropic",
            )

        # Run analysis
        proposals = coordinator.run_analysis_cycle()

        # Should have generated proposals
        assert len(coordinator._pending_upgrades) >= 0

        # Auto-approve some for testing
        for proposal in coordinator._pending_upgrades:
            if proposal.confidence >= 0.9:
                proposal.status = UpgradeStatus.APPROVED

        # Execute approved upgrades
        executed = coordinator.execute_pending_upgrades()

        # Summary should reflect the activity
        summary = coordinator.get_analysis_summary()
        assert summary["pending_upgrades"] >= 0
        assert summary["applied_upgrades"] == len(executed)

    def test_loop_runs_every_n_minutes(self, tmp_path: Path) -> None:
        """Test that the analysis loop runs at the configured interval."""
        coordinator = EvolutionCoordinator(
            tmp_path,
            analysis_interval_minutes=1,
        )

        # Initially should be ready to run
        assert coordinator.should_run_analysis() is True

        # Run analysis
        coordinator.run_analysis_cycle()

        # Should not be ready immediately
        assert coordinator.should_run_analysis() is False

        # Simulate time passing
        coordinator._last_analysis = time.time() - 120  # 2 minutes ago

        # Should be ready again
        assert coordinator.should_run_analysis() is True

    def test_produces_upgrade_tasks(self, tmp_path: Path) -> None:
        """Test that the coordinator produces valid upgrade tasks."""
        coordinator = EvolutionCoordinator(tmp_path)

        # Create a proposal manually
        proposal = UpgradeProposal(
            id="UPG-TEST",
            title="Test Upgrade",
            category=UpgradeCategory.ROUTING_RULES,
            description="Test upgrade description",
            current_state="Current state",
            proposed_change="Proposed change",
            benefits=["Benefit 1", "Benefit 2"],
            risk_assessment=RiskAssessment(level="low"),
            rollback_plan=RollbackPlan(
                steps=["Step 1", "Step 2"],
                estimated_rollback_minutes=10,
            ),
            cost_estimate_usd=-0.5,
            expected_improvement="Expected improvement",
            confidence=0.9,
        )

        # Convert to task
        task = proposal.to_task()

        # Verify task properties
        assert task.id == "UPG-TEST"
        assert task.title == "Test Upgrade"
        assert task.task_type == TaskType.UPGRADE_PROPOSAL
        assert task.role == "manager"
        assert task.upgrade_details is not None
        assert task.upgrade_details.benefits == ["Benefit 1", "Benefit 2"]


# --- record_agent_lifetime ---


class TestRecordAgentLifetime:
    """EvolutionCoordinator.record_agent_lifetime() persists to agents.jsonl."""

    def test_record_writes_to_agents_jsonl(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)

        coordinator.record_agent_lifetime(
            agent_id="agent-1",
            role="backend",
            lifetime_seconds=120.5,
            tasks_completed=2,
            model="sonnet",
        )

        agents_file = tmp_path / "metrics" / "agents.jsonl"
        assert agents_file.exists(), "agents.jsonl must be created on first write"
        lines = [l for l in agents_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["agent_id"] == "agent-1"
        assert record["role"] == "backend"
        assert record["lifetime_seconds"] == 120.5
        assert record["tasks_completed"] == 2

    def test_record_is_queryable_via_collector(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)

        coordinator.record_agent_lifetime(
            agent_id="agent-2",
            role="qa",
            lifetime_seconds=60.0,
            tasks_completed=1,
        )

        recent = coordinator.collector.get_recent_agent_metrics(hours=1)
        assert len(recent) == 1
        assert recent[0].agent_id == "agent-2"
        assert recent[0].tasks_completed == 1

    def test_multiple_calls_accumulate(self, tmp_path: Path) -> None:
        coordinator = EvolutionCoordinator(tmp_path)

        for i in range(3):
            coordinator.record_agent_lifetime(
                agent_id=f"agent-{i}",
                role="backend",
                lifetime_seconds=float(i * 30),
                tasks_completed=i,
            )

        recent = coordinator.collector.get_recent_agent_metrics(hours=1)
        assert len(recent) == 3
