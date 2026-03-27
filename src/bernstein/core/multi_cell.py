"""Multi-cell orchestrator: coordinates multiple cells, each with its own manager + workers.

The VP cell sits above all other cells and handles cross-cell coordination:
decomposing goals into subsystem objectives, resolving inter-cell blockers,
and rebalancing work when cells are overloaded or stuck.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.models import (
    AgentSession,
    Cell,
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
)
from bernstein.core.orchestrator import TickResult, group_by_role

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.spawner import AgentSpawner

logger = logging.getLogger(__name__)


@dataclass
class CellStatus:
    """Snapshot of a cell's current state.

    Args:
        cell_id: Cell identifier.
        open_tasks: Number of open tasks in this cell.
        active_agents: Number of alive agents in this cell.
        blocked_tasks: Number of blocked tasks.
        done_tasks: Number of completed tasks.
        failed_tasks: Number of failed tasks.
    """

    cell_id: str
    open_tasks: int = 0
    active_agents: int = 0
    blocked_tasks: int = 0
    done_tasks: int = 0
    failed_tasks: int = 0


@dataclass
class MultiCellTickResult:
    """Summary of one multi-cell orchestrator cycle.

    Args:
        cell_results: Per-cell tick results keyed by cell_id.
        vp_actions: Actions taken by the VP (rebalances, new cells, etc.).
        blockers_found: Number of active blockers on the bulletin board.
        errors: Any errors encountered during the tick.
    """

    cell_results: dict[str, TickResult] = field(default_factory=lambda: {})
    vp_actions: list[str] = field(default_factory=lambda: [])
    blockers_found: int = 0
    errors: list[str] = field(default_factory=lambda: [])


def _fetch_tasks_for_cell(
    client: httpx.Client,
    base_url: str,
    status: str,
    cell_id: str,
) -> list[Task]:
    """Fetch tasks from the server filtered by status and cell_id.

    Args:
        client: httpx client.
        base_url: Server base URL.
        status: Task status filter.
        cell_id: Cell to filter tasks for.

    Returns:
        Tasks matching both status and cell_id.
    """
    resp = client.get(
        f"{base_url}/tasks",
        params={"status": status, "cell_id": cell_id},
    )
    resp.raise_for_status()
    tasks: list[Task] = []
    for raw in resp.json():
        tasks.append(
            Task(
                id=raw["id"],
                title=raw["title"],
                description=raw["description"],
                role=raw["role"],
                priority=raw.get("priority", 2),
                scope=Scope(raw.get("scope", "medium")),
                complexity=Complexity(raw.get("complexity", "medium")),
                estimated_minutes=raw.get("estimated_minutes", 30),
                status=TaskStatus(raw.get("status", "open")),
                depends_on=raw.get("depends_on", []),
                owned_files=raw.get("owned_files", []),
                assigned_agent=raw.get("assigned_agent"),
                result_summary=raw.get("result_summary"),
                cell_id=raw.get("cell_id"),
            )
        )
    return tasks


def cell_status(cell: Cell) -> CellStatus:
    """Build a status snapshot for a cell.

    Args:
        cell: Cell to summarise.

    Returns:
        CellStatus with task and agent counts.
    """
    alive = sum(1 for w in cell.workers if w.status not in ("dead",))
    if cell.manager and cell.manager.status != "dead":
        alive += 1

    open_count = sum(1 for t in cell.task_queue if t.status == TaskStatus.OPEN)
    blocked_count = sum(1 for t in cell.task_queue if t.status == TaskStatus.BLOCKED)
    done_count = sum(1 for t in cell.task_queue if t.status == TaskStatus.DONE)
    failed_count = sum(1 for t in cell.task_queue if t.status == TaskStatus.FAILED)

    return CellStatus(
        cell_id=cell.id,
        open_tasks=open_count,
        active_agents=alive,
        blocked_tasks=blocked_count,
        done_tasks=done_count,
        failed_tasks=failed_count,
    )


class MultiCellOrchestrator:
    """Coordinates multiple cells, each with its own manager + workers.

    The multi-cell orchestrator adds a VP layer above individual cell
    orchestrators. Each tick:
      1. VP reviews cross-cell status via the bulletin board.
      2. Each cell runs its own orchestrator tick.
      3. Bulletin board is checked for blockers.
      4. Rebalancing is triggered if any cell is overloaded/stuck.

    Args:
        config: Orchestrator configuration (shared across cells).
        spawner: Agent spawner for all cells.
        workdir: Project working directory.
        bulletin: Shared bulletin board instance.
        client: httpx client for server communication.
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        spawner: AgentSpawner,
        workdir: Path,
        bulletin: BulletinBoard | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._spawner = spawner
        self._workdir = workdir
        self._bulletin = bulletin or BulletinBoard()
        self._client = client or httpx.Client(timeout=10.0)
        self._cells: dict[str, Cell] = {}
        self._running = False
        self._last_bulletin_ts: float = 0.0

    @property
    def cells(self) -> dict[str, Cell]:
        """Currently tracked cells, keyed by cell_id."""
        return dict(self._cells)

    @property
    def bulletin(self) -> BulletinBoard:
        """The shared bulletin board."""
        return self._bulletin

    def register_cell(self, cell: Cell) -> None:
        """Register a cell with the multi-cell orchestrator.

        Args:
            cell: Cell to add. Overwrites if cell.id already exists.
        """
        self._cells[cell.id] = cell
        logger.info("Registered cell %s (%s)", cell.id, cell.name)

    def remove_cell(self, cell_id: str) -> Cell | None:
        """Remove a cell from tracking.

        Args:
            cell_id: ID of cell to remove.

        Returns:
            The removed Cell, or None if not found.
        """
        return self._cells.pop(cell_id, None)

    def tick(self) -> MultiCellTickResult:
        """Execute one multi-cell orchestrator cycle.

        Steps:
            1. Check bulletin board for new blockers.
            2. For each cell: run cell-level tick.
            3. Collect cross-cell status.
            4. Rebalance if needed.

        Returns:
            Summary of what happened this tick.
        """
        result = MultiCellTickResult()

        # 1. Check bulletin for blockers
        new_messages = self._bulletin.read_since(self._last_bulletin_ts)
        if new_messages:
            self._last_bulletin_ts = max(m.timestamp for m in new_messages)

        blockers = [m for m in new_messages if m.type == "blocker"]
        result.blockers_found = len(blockers)

        for blocker in blockers:
            logger.warning(
                "Blocker from %s (cell=%s): %s",
                blocker.agent_id,
                blocker.cell_id or "global",
                blocker.content,
            )

        # 2. For each cell: run cell-level tick
        for cell_id, cell in self._cells.items():
            try:
                cell_tick = self._tick_cell(cell)
                result.cell_results[cell_id] = cell_tick
            except Exception as exc:
                logger.error("Cell %s tick failed: %s", cell_id, exc)
                result.errors.append(f"cell_{cell_id}: {exc}")

        # 3. Collect statuses and check for rebalancing
        statuses = {cid: cell_status(c) for cid, c in self._cells.items()}
        rebalance_actions = self._check_rebalance(statuses)
        result.vp_actions.extend(rebalance_actions)

        # 4. Log summary
        self._log_summary(result)

        return result

    def _tick_cell(self, cell: Cell) -> TickResult:
        """Run one orchestrator tick scoped to a single cell.

        Fetches open tasks for this cell, groups them, spawns agents
        within the cell's capacity, and checks done tasks.

        Args:
            cell: Cell to tick.

        Returns:
            TickResult for this cell.
        """
        result = TickResult()
        base = self._config.server_url

        # Fetch open tasks for this cell
        try:
            open_tasks = _fetch_tasks_for_cell(
                self._client,
                base,
                "open",
                cell.id,
            )
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch tasks for cell %s: %s", cell.id, exc)
            result.errors.append(f"fetch_open_{cell.id}: {exc}")
            return result

        result.open_tasks = len(open_tasks)

        # Group into batches
        batches = group_by_role(open_tasks, self._config.max_tasks_per_agent)

        # Count alive agents in this cell
        alive_count = sum(1 for w in cell.workers if w.status not in ("dead",))
        if cell.manager and cell.manager.status != "dead":
            alive_count += 1
        result.active_agents = alive_count

        # Spawn agents if capacity allows
        for batch in batches:
            if alive_count >= cell.max_workers + 1:  # +1 for manager
                break
            try:
                session = self._spawner.spawn_for_tasks(batch)
                session.cell_id = cell.id
                cell.workers.append(session)
                alive_count += 1
                result.spawned.append(session.id)
                logger.info(
                    "Spawned %s in cell %s for %d tasks",
                    session.id,
                    cell.id,
                    len(batch),
                )
            except Exception as exc:
                logger.error(
                    "Spawn failed in cell %s for batch %s: %s",
                    cell.id,
                    [t.id for t in batch],
                    exc,
                )
                result.errors.append(f"spawn_{cell.id}: {exc}")

        # Reap dead workers
        self._reap_dead_workers(cell, result)

        return result

    def _reap_dead_workers(self, cell: Cell, result: TickResult) -> None:
        """Remove dead workers from a cell's worker list.

        Args:
            cell: Cell to clean up.
            result: TickResult to record reaped agents into.
        """
        now = time.time()
        alive_workers: list[AgentSession] = []

        for worker in cell.workers:
            if worker.status == "dead":
                continue
            if not self._spawner.check_alive(worker):
                worker.status = "dead"
                result.reaped.append(worker.id)
                logger.info("Reaped dead worker %s from cell %s", worker.id, cell.id)
                continue
            # Check heartbeat timeout
            if worker.heartbeat_ts > 0 and now - worker.heartbeat_ts > self._config.heartbeat_timeout_s:
                self._spawner.kill(worker)
                result.reaped.append(worker.id)
                logger.warning(
                    "Reaped stale worker %s from cell %s (heartbeat %.0fs ago)",
                    worker.id,
                    cell.id,
                    now - worker.heartbeat_ts,
                )
                continue
            alive_workers.append(worker)

        cell.workers = alive_workers

    def _check_rebalance(self, statuses: dict[str, CellStatus]) -> list[str]:
        """Check if any cells need rebalancing and return VP actions taken.

        Current heuristic: flag cells with >15 open tasks or >3 blockers.

        Args:
            statuses: Per-cell status snapshots.

        Returns:
            List of human-readable action descriptions.
        """
        actions: list[str] = []

        for cell_id, status in statuses.items():
            if status.open_tasks > 15:
                msg = f"Cell {cell_id} overloaded ({status.open_tasks} open tasks). Consider splitting into a new cell."
                actions.append(msg)
                self._bulletin.post(
                    BulletinMessage(
                        agent_id="vp",
                        type="alert",
                        content=msg,
                        cell_id=cell_id,
                    )
                )
                logger.warning(msg)

            if status.blocked_tasks > 3:
                msg = f"Cell {cell_id} has {status.blocked_tasks} blocked tasks. VP escalation needed."
                actions.append(msg)
                self._bulletin.post(
                    BulletinMessage(
                        agent_id="vp",
                        type="blocker",
                        content=msg,
                        cell_id=cell_id,
                    )
                )
                logger.warning(msg)

        return actions

    def _log_summary(self, result: MultiCellTickResult) -> None:
        """Write a one-line summary to the orchestrator log.

        Args:
            result: MultiCellTickResult from the current tick.
        """
        log_dir = self._workdir / ".sdd" / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "multi_cell.log"

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        total_spawned = sum(len(cr.spawned) for cr in result.cell_results.values())
        total_open = sum(cr.open_tasks for cr in result.cell_results.values())
        line = (
            f"[{ts}] cells={len(self._cells)} open={total_open} "
            f"spawned={total_spawned} blockers={result.blockers_found} "
            f"vp_actions={len(result.vp_actions)} errors={len(result.errors)}\n"
        )
        with log_path.open("a") as f:
            f.write(line)

    def run(self) -> None:
        """Run the multi-cell orchestrator loop until stopped.

        Blocks the calling thread.
        """
        import time as _time

        self._running = True
        logger.info(
            "MultiCellOrchestrator started (cells=%d, poll=%ds)",
            len(self._cells),
            self._config.poll_interval_s,
        )
        while self._running:
            self.tick()
            _time.sleep(self._config.poll_interval_s)
        logger.info("MultiCellOrchestrator stopped")

    def stop(self) -> None:
        """Signal the run loop to exit after the current tick."""
        self._running = False
