"""Integration test for the self-evolution feedback loop wired into the Orchestrator.

Full pipeline:
  1. Orchestrator with evolution_enabled=True
  2. Mock task completions → record_task_completion → FileMetricsCollector
  3. Metrics persist to .sdd/metrics/tasks.jsonl
  4. Evolution tick (tick_count % interval == 0) → run_analysis_cycle()
  5. ProposalGenerator (deterministic, no LLM) → UpgradeProposal objects
  6. Proposals tracked in coordinator + pending.json written
  7. ApprovalGate logs decisions to decisions.jsonl
  8. Approved proposals queued or applied (history.jsonl)

No real LLM calls — ProposalGenerator and analysis pipeline are fully deterministic.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from bernstein.core.models import AgentSession, ModelConfig, OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.server import create_app
from bernstein.core.spawner import AgentSpawner
from bernstein.evolution import EvolutionCoordinator
from bernstein.evolution.aggregator import FileMetricsCollector, TaskMetrics
from bernstein.evolution.gate import ApprovalGate, ApprovalOutcome
from bernstein.evolution.proposals import AnalysisTrigger, UpgradeStatus
from bernstein.evolution.types import RiskLevel
from bernstein.evolution.types import UpgradeProposal as EvolutionUpgradeProposal

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TASK_PAYLOAD = {
    "title": "Implement caching layer",
    "description": "Add Redis caching for hot paths",
    "role": "backend",
    "priority": 1,
    "scope": "small",
    "complexity": "low",
    "estimated_minutes": 15,
}


def _seed_declining_metrics(
    collector: FileMetricsCollector,
    count: int = 25,
) -> None:
    """Seed task metrics with a declining success-rate pattern.

    First half: ~90% pass. Second half: ~40% pass. Overall < 80%, which
    guarantees OpportunityDetector fires a success-rate improvement proposal.
    """
    now = time.time()
    for i in range(count):
        if i < count // 2:
            passed = i % 10 != 0  # 90 % pass rate
        else:
            passed = i % 10 < 4  # 40 % pass rate

        collector.record_task_metrics(
            TaskMetrics(
                timestamp=now - (count - i) * 120,  # 2-min intervals in the past
                task_id=f"T-fbloop-{i:03d}",
                role="backend",
                model="sonnet",
                provider="anthropic",
                duration_seconds=30.0 + i,
                cost_usd=0.02 * (i + 1),
                janitor_passed=passed,
            )
        )


def _make_mock_spawner(
    session_id: str = "agent-001",
    role: str = "backend",
    pid: int = 9999,
) -> MagicMock:
    mock_spawner = MagicMock(spec=AgentSpawner)
    session = AgentSession(
        id=session_id,
        role=role,
        pid=pid,
        model_config=ModelConfig("sonnet", "high"),
        status="working",
    )
    mock_spawner.spawn_for_tasks.return_value = session
    mock_spawner.check_alive.return_value = True
    mock_spawner.get_worktree_path.return_value = None
    return mock_spawner


def _make_orchestrator(
    tmp_path: Path,
    client: TestClient,
    mock_spawner: MagicMock,
    coordinator: EvolutionCoordinator | None = None,
    evolution_tick_interval: int = 1,
) -> Orchestrator:
    config = OrchestratorConfig(
        server_url="http://testserver",
        max_agents=4,
        max_tasks_per_agent=2,
        poll_interval_s=1,
        evolution_enabled=True,
        evolution_tick_interval=evolution_tick_interval,
        evolve_mode=False,
    )
    return Orchestrator(
        config=config,
        spawner=mock_spawner,
        workdir=tmp_path,
        client=client,
        evolution=coordinator,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvolutionFeedbackLoop:
    """Full orchestrator → metrics → analysis → proposal → gate cycle."""

    def test_metrics_persist_from_coordinator_record_api(self, tmp_path: Path) -> None:
        """record_task_completion() writes metrics that survive a reload.

        Simulates what the orchestrator does in _process_completed_tasks.
        """
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        coordinator = EvolutionCoordinator(state_dir=state_dir)

        # Simulate 20 task completions (75 % pass rate — below 80 % threshold)
        for i in range(20):
            task = MagicMock()
            task.id = f"synthetic-{i}"
            task.role = "backend"
            coordinator.record_task_completion(
                task=task,
                duration_seconds=30.0,
                cost_usd=0.05,
                janitor_passed=(i % 4 != 0),  # 75 %
                model="sonnet",
                provider="anthropic",
            )

        recent = coordinator.collector.get_recent_task_metrics(hours=1)
        assert len(recent) == 20, "All 20 task completions must be recorded in metrics"

        # Metrics must persist to .sdd/metrics/tasks.jsonl
        tasks_jsonl = state_dir / "metrics" / "tasks.jsonl"
        assert tasks_jsonl.exists(), "Metrics must be written to .sdd/metrics/tasks.jsonl"
        lines = tasks_jsonl.read_text().strip().splitlines()
        assert len(lines) == 20

    def test_evolution_cycle_generates_proposals_from_declining_metrics(self, tmp_path: Path) -> None:
        """run_analysis_cycle() returns proposals when success rate < 80 %."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            analysis_interval_minutes=0,  # always due
        )

        proposals = coordinator.run_analysis_cycle(trigger=AnalysisTrigger.SCHEDULED)

        assert len(proposals) > 0, "Declining metrics (< 80 % success) must produce ≥ 1 upgrade proposal"
        for p in proposals:
            assert p.id.startswith("UPG-"), f"Unexpected proposal ID: {p.id}"
            assert p.title, "Proposal must have a non-empty title"
            assert 0.0 <= p.confidence <= 1.0, f"Confidence out of range: {p.confidence}"

    def test_proposals_tracked_as_pending_after_cycle(self, tmp_path: Path) -> None:
        """All proposals returned by run_analysis_cycle are held in get_pending_upgrades()."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            analysis_interval_minutes=0,
        )
        proposals = coordinator.run_analysis_cycle(trigger=AnalysisTrigger.SCHEDULED)
        assert len(proposals) > 0

        pending = coordinator.get_pending_upgrades()
        # Every proposal from the cycle must appear in the pending list
        pending_ids = {p.id for p in pending}
        for p in proposals:
            assert p.id in pending_ids, f"Proposal {p.id} missing from pending queue"

        for p in pending:
            assert p.status in (
                UpgradeStatus.PENDING,
                UpgradeStatus.APPROVED,
                UpgradeStatus.IN_PROGRESS,
            ), f"Unexpected status {p.status} for proposal {p.id}"

    def test_approval_gate_logs_decision_for_every_proposal(self, tmp_path: Path) -> None:
        """ApprovalGate writes a decisions.jsonl entry for each routed proposal."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            analysis_interval_minutes=0,
        )
        proposals = coordinator.run_analysis_cycle(trigger=AnalysisTrigger.SCHEDULED)
        assert len(proposals) > 0, "Need proposals to test the gate"

        decisions_dir = state_dir / "evolution"
        gate = ApprovalGate(decisions_dir=decisions_dir)

        routed_ids: list[str] = []
        for p in proposals:
            evo_proposal = EvolutionUpgradeProposal(
                id=p.id,
                title=p.title,
                description=p.description,
                risk_level=RiskLevel.L0_CONFIG,
                target_files=[".sdd/config/providers.yaml"],
                diff=(
                    "--- a/.sdd/config/providers.yaml\n"
                    "+++ b/.sdd/config/providers.yaml\n"
                    "@@ -1,2 +1,2 @@\n"
                    "-prefer_free_tier: false\n"
                    "+prefer_free_tier: true\n"
                ),
                rationale=p.description,
                expected_impact=p.expected_improvement,
                confidence=p.confidence,
            )
            decision = gate.route(evo_proposal)
            assert decision.outcome != ApprovalOutcome.BLOCKED, (
                f"L0_CONFIG proposal {p.id} must not be outright blocked"
            )
            routed_ids.append(p.id)

        # All decisions persisted
        decisions_log = decisions_dir / "decisions.jsonl"
        assert decisions_log.exists(), "ApprovalGate must create decisions.jsonl"

        logged = {json.loads(line)["proposal_id"] for line in decisions_log.read_text().strip().splitlines()}
        for pid in routed_ids:
            assert pid in logged, f"No decision logged for proposal {pid}"

    def test_orchestrator_tick_triggers_evolution_and_writes_pending_json(self, tmp_path: Path) -> None:
        """Tick with evolution_tick_interval=1 fires the cycle and writes pending.json."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        # Pre-seed 25 declining records so the analysis has enough data
        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            analysis_interval_minutes=0,
        )

        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        mock_spawner = _make_mock_spawner()

        with TestClient(app) as client:
            orchestrator = _make_orchestrator(
                tmp_path,
                client,
                mock_spawner,
                coordinator=coordinator,
                evolution_tick_interval=1,  # every tick fires the cycle
            )

            result = orchestrator.tick()

        # No evolution errors
        evo_errors = [e for e in result.errors if "evolution" in e.lower()]
        assert not evo_errors, f"Evolution cycle errors: {evo_errors}"

        # pending.json must exist with at least one entry
        pending_path = state_dir / "upgrades" / "pending.json"
        assert pending_path.exists(), ".sdd/upgrades/pending.json must be written after the evolution cycle"
        pending_data = json.loads(pending_path.read_text())
        assert isinstance(pending_data, list)
        assert len(pending_data) > 0, "Declining metrics must produce at least one pending proposal"

        # Each entry must carry the required fields
        for entry in pending_data:
            assert "id" in entry
            assert "title" in entry
            assert "status" in entry

    def test_orchestrator_tick_interval_controls_when_cycle_fires(self, tmp_path: Path) -> None:
        """Evolution cycle fires only when tick_count % interval == 0."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            analysis_interval_minutes=0,
        )

        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        mock_spawner = _make_mock_spawner()

        with TestClient(app) as client:
            orchestrator = _make_orchestrator(
                tmp_path,
                client,
                mock_spawner,
                coordinator=coordinator,
                evolution_tick_interval=3,  # fires on ticks 3, 6, 9 …
            )

            # Ticks 1 and 2 should NOT fire the cycle
            orchestrator.tick()  # tick 1 — no cycle
            orchestrator.tick()  # tick 2 — no cycle

            # Pending proposals should not yet exist (cycle hasn't run)
            pending_path = state_dir / "upgrades" / "pending.json"
            # If the file was written it must be empty or not exist
            if pending_path.exists():
                data = json.loads(pending_path.read_text())
                assert data == [], "pending.json must be empty before evolution cycle fires"

            # Tick 3 fires the cycle
            orchestrator.tick()

        pending_path = state_dir / "upgrades" / "pending.json"
        assert pending_path.exists(), "pending.json must exist after tick 3"
        data = json.loads(pending_path.read_text())
        assert len(data) > 0, "Cycle on tick 3 must produce proposals"

    def test_full_feedback_loop(self, tmp_path: Path) -> None:
        """Complete integration: task completions → metrics → evolution → proposals → pending."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()

        # ── Step 1: Create coordinator with pre-seeded declining metrics ──────
        collector = FileMetricsCollector(state_dir)
        _seed_declining_metrics(collector, count=25)

        coordinator = EvolutionCoordinator(
            state_dir=state_dir,
            collector=collector,
            analysis_interval_minutes=0,
        )

        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        mock_spawner = _make_mock_spawner()

        with TestClient(app) as client:
            # ── Step 2: Create and complete a task ────────────────────────────
            resp = client.post("/tasks", json=TASK_PAYLOAD)
            assert resp.status_code == 201
            task_id = resp.json()["id"]

            orchestrator = _make_orchestrator(
                tmp_path,
                client,
                mock_spawner,
                coordinator=coordinator,
                evolution_tick_interval=2,  # cycle fires on tick 2
            )

            # Tick 1: spawn agent
            orchestrator.tick()

            # Simulate agent completing the task
            client.post(
                f"/tasks/{task_id}/complete",
                json={"result_summary": "Caching layer implemented"},
            )
            mock_spawner.check_alive.return_value = False

            # Tick 2: processes completion + fires evolution cycle
            result2 = orchestrator.tick()

        # ── Step 3: Verify metrics are in .sdd/metrics/ ───────────────────────
        tasks_jsonl = state_dir / "metrics" / "tasks.jsonl"
        assert tasks_jsonl.exists(), ".sdd/metrics/tasks.jsonl must exist after task completions"
        lines = tasks_jsonl.read_text().strip().splitlines()
        # At minimum, the 25 seeded records must be there
        assert len(lines) >= 25

        # ── Step 4: Verify evolution cycle ran (pending.json written) ─────────
        pending_path = state_dir / "upgrades" / "pending.json"
        assert pending_path.exists(), ".sdd/upgrades/pending.json must be written by the evolution cycle"

        # ── Step 5: Verify proposals were generated ───────────────────────────
        pending_data = json.loads(pending_path.read_text())
        assert isinstance(pending_data, list)
        assert len(pending_data) > 0, "At least one proposal must be generated"

        # ── Step 6: Verify each proposal has a proper structure ───────────────
        for entry in pending_data:
            assert entry.get("id", "").startswith("UPG-"), f"Proposal ID must start with 'UPG-': {entry.get('id')}"
            assert entry.get("title"), "Proposal must have a non-empty title"
            assert entry.get("status") in (
                "pending",
                "approved",
                "in_progress",
            ), f"Unexpected status: {entry.get('status')}"

        # ── Step 7: Verify no evolution errors occurred ────────────────────────
        evo_errors = [e for e in result2.errors if "evolution" in e.lower()]
        assert not evo_errors, f"Evolution cycle produced errors: {evo_errors}"

        # ── Step 8: Coordinator holds proposals in pending or applied ──────────
        pending_upgrades = coordinator.get_pending_upgrades()
        applied_upgrades = coordinator.get_applied_upgrades()
        total = len(pending_upgrades) + len(applied_upgrades)
        assert total > 0, "After the evolution cycle, coordinator must hold at least one proposal in pending or applied"
