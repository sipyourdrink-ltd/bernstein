"""Integration test: full self-evolution feedback loop from task completion to upgrade.

Verifies the end-to-end data flow:
1. Task completion → MetricsCollector.record_task()
2. EvolutionCoordinator.run_analysis_cycle() reads metrics and runs analysis
3. Analysis finds opportunity → creates UpgradeProposal
4. Approved proposal → UpgradeExecutor.execute() applies change
5. Failed execution → rollback
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from bernstein.core.evolution import (
    EvolutionCoordinator,
    FileMetricsCollector,
    FileUpgradeExecutor,
    TaskMetrics,
    UpgradeStatus,
)

# --- Helpers ---


def _seed_task_metrics(collector: FileMetricsCollector, count: int = 20) -> None:
    """Seed the collector with enough task metrics to trigger analysis.

    Creates metrics with mixed janitor_passed to trigger success rate opportunity.
    """
    now = time.time()
    for i in range(count):
        metrics = TaskMetrics(
            timestamp=now - (count - i) * 60,
            task_id=f"T-seed-{i:03d}",
            role="backend",
            model="sonnet",
            provider="test_provider",
            duration_seconds=30.0 + i,
            cost_usd=0.01 * (i + 1),
            # Make 30% of tasks fail janitor to trigger below-80% success rate
            janitor_passed=(i % 3 != 0),
        )
        collector.record_task_metrics(metrics)


# --- Tests ---


class TestEvolutionEndToEnd:
    """Full cycle: task completion → metrics → analysis → proposal → execute."""

    def test_full_evolution_cycle(self, tmp_path: Path, make_task) -> None:
        """Complete loop from task completion through upgrade application."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        executor = FileUpgradeExecutor(state_dir)
        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            executor=executor,
        )

        # Step 1: Record task completions (simulating orchestrator callback)
        task = make_task()
        coordinator.record_task_completion(
            task=task,
            duration_seconds=45.0,
            cost_usd=0.05,
            janitor_passed=True,
            model="sonnet",
            provider="test_provider",
        )

        # Verify metric was recorded
        recent = collector.get_recent_task_metrics(hours=1)
        assert len(recent) == 1
        assert recent[0].task_id == "T-001"
        assert recent[0].janitor_passed is True

        # Seed enough metrics for analysis to find patterns
        _seed_task_metrics(collector, count=20)

        # Step 2: Run analysis cycle
        proposals = coordinator.run_analysis_cycle()

        # With 30% failure rate, analysis should find improvement opportunity
        assert len(proposals) > 0

        # Verify proposals were generated
        pending = coordinator.get_pending_upgrades()
        assert len(pending) > 0

        # Step 3: Auto-approve and execute
        # Mark as approved for execution
        for p in pending:
            p.status = UpgradeStatus.APPROVED

        executed = coordinator.execute_pending_upgrades()
        assert len(executed) > 0

        # Step 4: Verify upgrade was applied
        applied = coordinator.get_applied_upgrades()
        assert len(applied) > 0
        assert applied[0].status == UpgradeStatus.APPLIED
        assert applied[0].applied_at is not None

    def test_record_task_completion_persists_to_file(self, tmp_path: Path, make_task) -> None:
        """Verify task metrics are persisted to JSONL files."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        coordinator = EvolutionCoordinator(state_dir=state_dir, collector=collector)

        task = make_task(id="T-persist")
        coordinator.record_task_completion(
            task=task,
            duration_seconds=10.0,
            cost_usd=0.01,
            janitor_passed=True,
        )

        # Check file was written
        tasks_jsonl = state_dir / "metrics" / "tasks.jsonl"
        assert tasks_jsonl.exists()

        lines = tasks_jsonl.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["task_id"] == "T-persist"
        assert record["janitor_passed"] is True

    def test_analysis_cycle_generates_proposals_from_metrics(self, tmp_path: Path) -> None:
        """Analysis cycle reads metrics and generates upgrade proposals."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        coordinator = EvolutionCoordinator(state_dir=state_dir, collector=collector)

        # Seed metrics with poor success rate (< 80%)
        _seed_task_metrics(collector, count=20)

        proposals = coordinator.run_analysis_cycle()

        # Should find at least the success rate improvement opportunity
        has_success_rate_proposal = any("success rate" in p.title.lower() for p in proposals)
        assert has_success_rate_proposal

    def test_approved_proposal_gets_executed(self, tmp_path: Path) -> None:
        """Auto-approved proposals are executed by the UpgradeExecutor."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        executor = FileUpgradeExecutor(state_dir)
        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            executor=executor,
        )

        # Seed and run analysis
        _seed_task_metrics(collector, count=20)
        proposals = coordinator.run_analysis_cycle()
        assert len(proposals) > 0

        # Force approve all
        for p in coordinator.get_pending_upgrades():
            p.status = UpgradeStatus.APPROVED

        executed = coordinator.execute_pending_upgrades()
        assert len(executed) > 0

        # Verify history was recorded
        history_file = state_dir / "upgrades" / "history.jsonl"
        assert history_file.exists()
        lines = history_file.read_text().strip().split("\n")
        assert len(lines) >= 1
        record = json.loads(lines[0])
        assert record["status"] == "applied"

    def test_failed_execution_triggers_rollback(self, tmp_path: Path) -> None:
        """When executor.execute_upgrade fails, rollback is attempted."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)

        # Mock executor that fails execution but succeeds rollback
        mock_executor = MagicMock()
        mock_executor.execute_upgrade.return_value = False
        mock_executor.rollback_upgrade.return_value = True

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            executor=mock_executor,
        )

        # Seed and run analysis
        _seed_task_metrics(collector, count=20)
        proposals = coordinator.run_analysis_cycle()
        assert len(proposals) > 0

        # Force approve
        for p in coordinator.get_pending_upgrades():
            p.status = UpgradeStatus.APPROVED

        executed = coordinator.execute_pending_upgrades()

        # No proposals successfully executed
        assert len(executed) == 0

        # Rollback was called
        mock_executor.rollback_upgrade.assert_called()

        # Proposal should be marked as rolled back and removed from pending
        assert len(coordinator.get_pending_upgrades()) == 0

    def test_failed_rollback_marks_rejected(self, tmp_path: Path) -> None:
        """When both execution and rollback fail, proposal is rejected."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)

        mock_executor = MagicMock()
        mock_executor.execute_upgrade.return_value = False
        mock_executor.rollback_upgrade.return_value = False

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            executor=mock_executor,
        )

        _seed_task_metrics(collector, count=20)
        coordinator.run_analysis_cycle()

        for p in coordinator.get_pending_upgrades():
            p.status = UpgradeStatus.APPROVED

        executed = coordinator.execute_pending_upgrades()
        assert len(executed) == 0
        assert len(coordinator.get_pending_upgrades()) == 0

    def test_pending_proposals_persisted_by_orchestrator(self, tmp_path: Path) -> None:
        """Verify _persist_pending_proposals writes to pending.json."""
        import httpx

        from bernstein.adapters.base import CLIAdapter, SpawnResult
        from bernstein.core.models import OrchestratorConfig
        from bernstein.core.orchestrator import Orchestrator
        from bernstein.core.spawner import AgentSpawner

        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        coordinator = EvolutionCoordinator(state_dir=state_dir, collector=collector)

        # Seed metrics to generate proposals
        _seed_task_metrics(collector, count=20)
        coordinator.run_analysis_cycle()

        # Build orchestrator with evolution wired in
        cfg = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=True,
        )
        adapter = MagicMock(spec=CLIAdapter)
        adapter.spawn.return_value = SpawnResult(pid=1, log_path=Path("/tmp/t.log"))
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(
            cfg,
            spawner,
            tmp_path,
            client=client,
            evolution=coordinator,
        )

        # Call persist
        orch._persist_pending_proposals()

        pending_path = tmp_path / ".sdd" / "upgrades" / "pending.json"
        assert pending_path.exists()
        data = json.loads(pending_path.read_text())
        assert isinstance(data, list)
        assert len(data) > 0
        assert "id" in data[0]
        assert "title" in data[0]
        assert "status" in data[0]

    def test_evolution_coordinator_should_run_analysis_timing(self, tmp_path: Path) -> None:
        """should_run_analysis respects the analysis interval."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            analysis_interval_minutes=60,
        )

        # First call — should run (never ran before)
        assert coordinator.should_run_analysis() is True

        # Run it
        coordinator.run_analysis_cycle()

        # Immediately after — should not run
        assert coordinator.should_run_analysis() is False
