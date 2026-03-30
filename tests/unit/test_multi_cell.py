"""Tests for bernstein.core.multi_cell — CellStatus tracking, VP coordination, rebalancing."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx

from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.models import (
    AgentSession,
    Cell,
    ModelConfig,
    OrchestratorConfig,
    Task,
    TaskStatus,
)
from bernstein.core.multi_cell import (
    CellStatus,
    MultiCellOrchestrator,
    cell_status,
)
from bernstein.core.orchestrator import TickResult
from bernstein.core.spawner import AgentSpawner

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

    def test_counts_task_statuses(self, make_task) -> None:
        tasks = [
            make_task(id="t1", status=TaskStatus.OPEN),
            make_task(id="t2", status=TaskStatus.OPEN),
            make_task(id="t3", status=TaskStatus.DONE),
            make_task(id="t4", status=TaskStatus.FAILED),
            make_task(id="t5", status=TaskStatus.BLOCKED),
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
        board.post(
            BulletinMessage(
                agent_id="agent-1",
                type="blocker",
                content="DB is down",
                timestamp=1.0,
            )
        )
        board.post(
            BulletinMessage(
                agent_id="agent-2",
                type="blocker",
                content="API unreachable",
                timestamp=2.0,
            )
        )
        board.post(
            BulletinMessage(
                agent_id="agent-3",
                type="status",
                content="OK",
                timestamp=3.0,
            )
        )
        orch = self._make_orchestrator(tmp_path, bulletin=board)
        result = orch.tick()
        assert result.blockers_found == 2

    def test_tick_ignores_old_bulletin_messages(self, tmp_path: Path) -> None:
        board = BulletinBoard()
        board.post(
            BulletinMessage(
                agent_id="a1",
                type="blocker",
                content="old",
                timestamp=1.0,
            )
        )
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


# ---------------------------------------------------------------------------
# _tick_cell
# ---------------------------------------------------------------------------


def _task_json(
    *,
    id: str = "t1",
    role: str = "backend",
    cell_id: str = "cell-1",
    status: str = "open",
) -> dict:
    return {
        "id": id,
        "title": f"Task {id}",
        "description": "Do something.",
        "role": role,
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "status": status,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "cell_id": cell_id,
    }


def _mock_response(tasks: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = tasks
    resp.raise_for_status.return_value = None
    return resp


class TestTickCell:
    def _make_orchestrator(
        self,
        tmp_path: Path,
        mock_client: MagicMock | None = None,
        mock_spawner: MagicMock | None = None,
        max_tasks_per_agent: int = 1,
        heartbeat_timeout_s: int = 900,
    ) -> MultiCellOrchestrator:
        config = OrchestratorConfig(
            server_url="http://localhost:9999",
            max_tasks_per_agent=max_tasks_per_agent,
            heartbeat_timeout_s=heartbeat_timeout_s,
        )
        spawner = mock_spawner or MagicMock(spec=AgentSpawner)
        return MultiCellOrchestrator(
            config=config,
            spawner=spawner,
            workdir=tmp_path,
            client=mock_client or MagicMock(),
        )

    def test_fetches_tasks_spawns_agent_records_session(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response([_task_json(id="t1")])

        mock_spawner = MagicMock(spec=AgentSpawner)
        spawned_session = AgentSession(id="sess-1", role="backend", model_config=ModelConfig("sonnet", "high"))
        mock_spawner.spawn_for_tasks.return_value = spawned_session
        mock_spawner.check_alive.return_value = True

        orch = self._make_orchestrator(tmp_path, mock_client=mock_client, mock_spawner=mock_spawner)
        cell = _make_cell(id="cell-1", max_workers=4)

        result = orch._tick_cell(cell)

        assert result.open_tasks == 1
        assert "sess-1" in result.spawned
        assert spawned_session.cell_id == "cell-1"
        assert spawned_session in cell.workers

    def test_groups_by_role_and_spawns_per_batch(self, tmp_path: Path) -> None:
        """Two different roles → two separate spawn calls."""
        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(
            [
                _task_json(id="t1", role="backend"),
                _task_json(id="t2", role="qa"),
            ]
        )

        mock_spawner = MagicMock(spec=AgentSpawner)
        sess_be = AgentSession(id="sess-be", role="backend", model_config=ModelConfig("sonnet", "high"))
        sess_qa = AgentSession(id="sess-qa", role="qa", model_config=ModelConfig("sonnet", "high"))
        mock_spawner.spawn_for_tasks.side_effect = [sess_be, sess_qa]
        mock_spawner.check_alive.return_value = True

        orch = self._make_orchestrator(tmp_path, mock_client=mock_client, mock_spawner=mock_spawner)
        cell = _make_cell(id="cell-1", max_workers=4)

        result = orch._tick_cell(cell)

        assert mock_spawner.spawn_for_tasks.call_count == 2
        assert set(result.spawned) == {"sess-be", "sess-qa"}

    def test_http_error_on_fetch_records_error_and_returns_early(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.HTTPError("connection refused")

        mock_spawner = MagicMock(spec=AgentSpawner)
        orch = self._make_orchestrator(tmp_path, mock_client=mock_client, mock_spawner=mock_spawner)
        cell = _make_cell(id="cell-1")

        result = orch._tick_cell(cell)

        assert result.open_tasks == 0
        assert result.spawned == []
        assert len(result.errors) == 1
        assert "fetch_open_cell-1" in result.errors[0]
        mock_spawner.spawn_for_tasks.assert_not_called()

    def test_spawn_exception_records_error_and_continues(self, tmp_path: Path) -> None:
        """First spawn fails, second succeeds — both are handled."""
        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(
            [
                _task_json(id="t1", role="backend"),
                _task_json(id="t2", role="qa"),
            ]
        )

        mock_spawner = MagicMock(spec=AgentSpawner)
        good_session = AgentSession(id="sess-good", role="qa", model_config=ModelConfig("sonnet", "high"))
        mock_spawner.spawn_for_tasks.side_effect = [Exception("spawn failed"), good_session]
        mock_spawner.check_alive.return_value = True

        orch = self._make_orchestrator(tmp_path, mock_client=mock_client, mock_spawner=mock_spawner)
        cell = _make_cell(id="cell-1", max_workers=4)

        result = orch._tick_cell(cell)

        assert any("spawn_cell-1" in e for e in result.errors)
        assert "sess-good" in result.spawned

    def test_alive_count_respects_max_workers_plus_one_limit(self, tmp_path: Path) -> None:
        """No new agents spawned when cell is already at max_workers+1 (manager counts)."""
        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response([_task_json(id="t1")])

        mock_spawner = MagicMock(spec=AgentSpawner)
        mock_spawner.check_alive.return_value = True

        orch = self._make_orchestrator(tmp_path, mock_client=mock_client, mock_spawner=mock_spawner)

        # max_workers=2, 2 alive workers + 1 alive manager = 3 = max_workers+1
        workers = [
            _make_session(id="w1", status="working"),
            _make_session(id="w2", status="working"),
        ]
        manager = _make_session(id="mgr", status="working")
        cell = _make_cell(id="cell-1", max_workers=2, workers=workers, manager=manager)

        result = orch._tick_cell(cell)

        mock_spawner.spawn_for_tasks.assert_not_called()
        assert result.spawned == []


# ---------------------------------------------------------------------------
# _reap_dead_workers
# ---------------------------------------------------------------------------


class TestReapDeadWorkers:
    def _make_orchestrator(
        self,
        tmp_path: Path,
        mock_spawner: MagicMock | None = None,
        heartbeat_timeout_s: int = 60,
    ) -> MultiCellOrchestrator:
        config = OrchestratorConfig(
            server_url="http://localhost:9999",
            heartbeat_timeout_s=heartbeat_timeout_s,
        )
        spawner = mock_spawner or MagicMock(spec=AgentSpawner)
        return MultiCellOrchestrator(
            config=config,
            spawner=spawner,
            workdir=tmp_path,
        )

    def test_already_dead_workers_are_removed_not_reaped(self, tmp_path: Path) -> None:
        """Workers already marked dead are silently dropped — not counted as reaped."""
        mock_spawner = MagicMock(spec=AgentSpawner)
        mock_spawner.check_alive.return_value = True

        orch = self._make_orchestrator(tmp_path, mock_spawner=mock_spawner)
        dead = _make_session(id="dead-1", status="dead")
        alive = _make_session(id="alive-1", status="working")
        cell = _make_cell(workers=[dead, alive])

        result = TickResult()
        orch._reap_dead_workers(cell, result)

        assert len(cell.workers) == 1
        assert cell.workers[0].id == "alive-1"
        assert "dead-1" not in result.reaped

    def test_check_alive_false_marks_dead_and_appends_to_reaped(self, tmp_path: Path) -> None:
        mock_spawner = MagicMock(spec=AgentSpawner)
        mock_spawner.check_alive.return_value = False

        orch = self._make_orchestrator(tmp_path, mock_spawner=mock_spawner)
        worker = _make_session(id="w1", status="working")
        cell = _make_cell(workers=[worker])

        result = TickResult()
        orch._reap_dead_workers(cell, result)

        assert worker.status == "dead"
        assert "w1" in result.reaped
        assert cell.workers == []

    def test_heartbeat_timeout_kills_and_reaps_worker(self, tmp_path: Path) -> None:
        mock_spawner = MagicMock(spec=AgentSpawner)
        mock_spawner.check_alive.return_value = True  # Process alive but heartbeat stale

        orch = self._make_orchestrator(tmp_path, mock_spawner=mock_spawner, heartbeat_timeout_s=60)
        worker = _make_session(id="w1", status="working")
        worker.heartbeat_ts = time.time() - 200  # 200s ago, beyond 60s timeout

        cell = _make_cell(workers=[worker])

        result = TickResult()
        orch._reap_dead_workers(cell, result)

        mock_spawner.kill.assert_called_once_with(worker)
        assert "w1" in result.reaped
        assert cell.workers == []

    def test_healthy_workers_retained(self, tmp_path: Path) -> None:
        mock_spawner = MagicMock(spec=AgentSpawner)
        mock_spawner.check_alive.return_value = True

        orch = self._make_orchestrator(tmp_path, mock_spawner=mock_spawner)
        w1 = _make_session(id="w1", status="working")
        w2 = _make_session(id="w2", status="idle")
        cell = _make_cell(workers=[w1, w2])

        result = TickResult()
        orch._reap_dead_workers(cell, result)

        assert len(cell.workers) == 2
        assert result.reaped == []
        mock_spawner.kill.assert_not_called()

    def test_mixed_workers_only_dead_reaped(self, tmp_path: Path) -> None:
        """Only the check_alive=False worker is reaped; healthy worker stays."""
        mock_spawner = MagicMock(spec=AgentSpawner)

        def check_alive_side_effect(session: AgentSession) -> bool:
            return session.id != "dying"

        mock_spawner.check_alive.side_effect = check_alive_side_effect

        orch = self._make_orchestrator(tmp_path, mock_spawner=mock_spawner)
        dying = _make_session(id="dying", status="working")
        healthy = _make_session(id="healthy", status="working")
        cell = _make_cell(workers=[dying, healthy])

        result = TickResult()
        orch._reap_dead_workers(cell, result)

        assert len(cell.workers) == 1
        assert cell.workers[0].id == "healthy"
        assert "dying" in result.reaped
