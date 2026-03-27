"""Tests for bernstein.core.multi_cell — CellStatus tracking, VP coordination, rebalancing."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.models import (
    AgentSession,
    Cell,
    Complexity,
    ModelConfig,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.multi_cell import (
    CellStatus,
    MultiCellOrchestrator,
    cell_status,
)
from bernstein.core.spawner import AgentSpawner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(
    *,
    id: str = "t-001",
    title: str = "Task",
    status: TaskStatus = TaskStatus.OPEN,
    role: str = "backend",
) -> Task:
    return Task(
        id=id,
        title=title,
        description="desc",
        role=role,
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=status,
    )


def _make_session(*, id: str, status: str = "working") -> AgentSession:
    return AgentSession(
        id=id,
        role="backend",
        pid=123,
        model_config=ModelConfig("sonnet", "high"),
        status=status,  # type: ignore[arg-type]
    )


def _make_cell(
    *,
    id: str = "cell-1",
    name: str = "Team Alpha",
    tasks: list[Task] | None = None,
    workers: list[AgentSession] | None = None,
    manager: AgentSession | None = None,
    max_workers: int = 4,
) -> Cell:
    return Cell(
        id=id,
        name=name,
        manager=manager,
        workers=workers or [],
        max_workers=max_workers,
        task_queue=tasks or [],
    )


# ---------------------------------------------------------------------------
# cell_status
# ---------------------------------------------------------------------------

class TestCellStatus:
    def test_empty_cell_all_zeros(self) -> None:
        cell = _make_cell()
        status = cell_status(cell)
        assert status.cell_id == "cell-1"
        assert status.open_tasks == 0
        assert status.active_agents == 0
        assert status.blocked_tasks == 0
        assert status.done_tasks == 0
        assert status.failed_tasks == 0

    def test_counts_task_statuses(self) -> None:
        tasks = [
            _make_task(id="t1", status=TaskStatus.OPEN),
            _make_task(id="t2", status=TaskStatus.OPEN),
            _make_task(id="t3", status=TaskStatus.DONE),
            _make_task(id="t4", status=TaskStatus.FAILED),
            _make_task(id="t5", status=TaskStatus.BLOCKED),
        ]
        cell = _make_cell(tasks=tasks)
        status = cell_status(cell)
        assert status.open_tasks == 2
        assert status.done_tasks == 1
        assert status.failed_tasks == 1
        assert status.blocked_tasks == 1

    def test_counts_alive_workers(self) -> None:
        workers = [
            _make_session(id="w1", status="working"),
            _make_session(id="w2", status="idle"),
            _make_session(id="w3", status="dead"),
        ]
        cell = _make_cell(workers=workers)
        status = cell_status(cell)
        assert status.active_agents == 2  # w1 and w2 alive, w3 dead

    def test_counts_alive_manager(self) -> None:
        manager = _make_session(id="mgr", status="working")
        cell = _make_cell(manager=manager)
        status = cell_status(cell)
        assert status.active_agents == 1

    def test_dead_manager_not_counted(self) -> None:
        manager = _make_session(id="mgr", status="dead")
        cell = _make_cell(manager=manager)
        status = cell_status(cell)
        assert status.active_agents == 0


# ---------------------------------------------------------------------------
# MultiCellOrchestrator register / remove
# ---------------------------------------------------------------------------

class TestRegisterRemoveCell:
    def _make_orchestrator(self, tmp_path: Path) -> MultiCellOrchestrator:
        mock_spawner = MagicMock(spec=AgentSpawner)
        config = OrchestratorConfig(server_url="http://localhost:9999")
        return MultiCellOrchestrator(
            config=config,
            spawner=mock_spawner,
            workdir=tmp_path,
        )

    def test_register_cell(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        cell = _make_cell(id="alpha")
        orch.register_cell(cell)
        assert "alpha" in orch.cells

    def test_remove_cell(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        cell = _make_cell(id="alpha")
        orch.register_cell(cell)
        removed = orch.remove_cell("alpha")
        assert removed is cell
        assert "alpha" not in orch.cells

    def test_remove_nonexistent_returns_none(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        assert orch.remove_cell("does-not-exist") is None

    def test_overwrite_cell_on_re_register(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        cell1 = _make_cell(id="alpha", name="First")
        cell2 = _make_cell(id="alpha", name="Second")
        orch.register_cell(cell1)
        orch.register_cell(cell2)
        assert orch.cells["alpha"].name == "Second"


# ---------------------------------------------------------------------------
# MultiCellOrchestrator.tick — bulletin board
# ---------------------------------------------------------------------------

class TestMultiCellTick:
    def _make_orchestrator(
        self,
        tmp_path: Path,
        bulletin: BulletinBoard | None = None,
        mock_client: MagicMock | None = None,
    ) -> MultiCellOrchestrator:
        mock_spawner = MagicMock(spec=AgentSpawner)
        config = OrchestratorConfig(server_url="http://localhost:9999")
        return MultiCellOrchestrator(
            config=config,
            spawner=mock_spawner,
            workdir=tmp_path,
            bulletin=bulletin,
            client=mock_client or MagicMock(),
        )

    def test_tick_no_cells_no_errors(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        result = orch.tick()
        assert result.cell_results == {}
        assert result.errors == []
        assert result.blockers_found == 0

    def test_tick_counts_bulletin_blockers(self, tmp_path: Path) -> None:
        board = BulletinBoard()
        board.post(BulletinMessage(
            agent_id="agent-1", type="blocker", content="DB is down", timestamp=1.0,
        ))
        board.post(BulletinMessage(
            agent_id="agent-2", type="blocker", content="API unreachable", timestamp=2.0,
        ))
        board.post(BulletinMessage(
            agent_id="agent-3", type="status", content="OK", timestamp=3.0,
        ))
        orch = self._make_orchestrator(tmp_path, bulletin=board)
        result = orch.tick()
        assert result.blockers_found == 2

    def test_tick_ignores_old_bulletin_messages(self, tmp_path: Path) -> None:
        board = BulletinBoard()
        board.post(BulletinMessage(
            agent_id="a1", type="blocker", content="old", timestamp=1.0,
        ))
        orch = self._make_orchestrator(tmp_path, bulletin=board)

        # First tick sees the blocker
        result1 = orch.tick()
        assert result1.blockers_found == 1

        # Second tick sees no new messages
        result2 = orch.tick()
        assert result2.blockers_found == 0

    def test_tick_cell_errors_captured(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("network error")
        orch = self._make_orchestrator(tmp_path, mock_client=mock_client)

        cell = _make_cell(id="alpha")
        orch.register_cell(cell)

        result = orch.tick()
        assert len(result.errors) >= 1
        assert any("alpha" in e for e in result.errors)


# ---------------------------------------------------------------------------
# _check_rebalance
# ---------------------------------------------------------------------------

class TestCheckRebalance:
    def _make_orchestrator(self, tmp_path: Path) -> MultiCellOrchestrator:
        mock_spawner = MagicMock(spec=AgentSpawner)
        config = OrchestratorConfig(server_url="http://localhost:9999")
        return MultiCellOrchestrator(
            config=config,
            spawner=mock_spawner,
            workdir=tmp_path,
        )

    def test_no_rebalance_needed_normal_load(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        statuses = {
            "c1": CellStatus(cell_id="c1", open_tasks=5, blocked_tasks=1),
        }
        actions = orch._check_rebalance(statuses)
        assert actions == []

    def test_overloaded_cell_triggers_alert(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        statuses = {
            "c1": CellStatus(cell_id="c1", open_tasks=16),
        }
        actions = orch._check_rebalance(statuses)
        assert len(actions) == 1
        assert "overloaded" in actions[0].lower()

    def test_overloaded_posts_to_bulletin(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        statuses = {
            "c1": CellStatus(cell_id="c1", open_tasks=20),
        }
        orch._check_rebalance(statuses)
        alerts = orch.bulletin.read_by_type("alert")
        assert len(alerts) == 1
        assert alerts[0].agent_id == "vp"
        assert alerts[0].cell_id == "c1"

    def test_many_blockers_triggers_escalation(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        statuses = {
            "c1": CellStatus(cell_id="c1", blocked_tasks=4),
        }
        actions = orch._check_rebalance(statuses)
        assert len(actions) == 1
        assert "blocked" in actions[0].lower()

    def test_many_blockers_posts_blocker_message(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        statuses = {
            "c1": CellStatus(cell_id="c1", blocked_tasks=5),
        }
        orch._check_rebalance(statuses)
        blockers = orch.bulletin.read_by_type("blocker")
        assert len(blockers) == 1
        assert blockers[0].cell_id == "c1"

    def test_multiple_cells_all_checked(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        statuses = {
            "c1": CellStatus(cell_id="c1", open_tasks=20),
            "c2": CellStatus(cell_id="c2", blocked_tasks=5),
            "c3": CellStatus(cell_id="c3", open_tasks=2),
        }
        actions = orch._check_rebalance(statuses)
        assert len(actions) == 2


# ---------------------------------------------------------------------------
# bulletin property
# ---------------------------------------------------------------------------

class TestBulletinProperty:
    def test_bulletin_returns_board(self, tmp_path: Path) -> None:
        mock_spawner = MagicMock(spec=AgentSpawner)
        config = OrchestratorConfig()
        board = BulletinBoard()
        orch = MultiCellOrchestrator(
            config=config,
            spawner=mock_spawner,
            workdir=tmp_path,
            bulletin=board,
        )
        assert orch.bulletin is board
