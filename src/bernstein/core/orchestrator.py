"""Orchestrator loop: watch tasks, spawn agents, verify completion, repeat.

The orchestrator is DETERMINISTIC CODE, not an LLM. It matches tasks to agents
via the spawner and verifies completion via the janitor. See ADR-001.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from bernstein.core.context import append_decision, refresh_knowledge_base
from bernstein.core.evolution import EvolutionCoordinator
from bernstein.core.janitor import verify_task
from bernstein.core.metrics import get_collector
from bernstein.core.models import (
    AgentSession,
    CompletionSignal,
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.router import TierAwareRouter, load_providers_from_yaml
from bernstein.evolution.types import MetricsRecord

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.spawner import AgentSpawner

logger = logging.getLogger(__name__)


def _task_from_dict(raw: dict[str, Any]) -> Task:
    """Deserialise a server JSON response into a domain Task.

    Args:
        raw: Dict from the task server JSON response.

    Returns:
        Populated Task dataclass.
    """
    # Parse task type
    task_type = TaskType.STANDARD
    if "task_type" in raw:
        try:
            task_type = TaskType(raw["task_type"])
        except ValueError:
            logger.warning("Invalid task_type %r from server", raw["task_type"])

    # Parse completion signals
    signals: list[CompletionSignal] = []
    for sig in raw.get("completion_signals", []):
        try:
            signals.append(CompletionSignal(type=sig["type"], value=sig["value"]))
        except (KeyError, TypeError):
            logger.warning("Invalid completion_signal entry: %r", sig)

    return Task(
        id=raw["id"],
        title=raw["title"],
        description=raw["description"],
        role=raw["role"],
        priority=raw.get("priority", 2),
        scope=Scope(raw.get("scope", "medium")),
        complexity=Complexity(raw.get("complexity", "medium")),
        estimated_minutes=raw.get("estimated_minutes", 30),
        status=TaskStatus(raw.get("status", "open")),
        task_type=task_type,
        depends_on=raw.get("depends_on", []),
        completion_signals=signals,
        owned_files=raw.get("owned_files", []),
        assigned_agent=raw.get("assigned_agent"),
        result_summary=raw.get("result_summary"),
        model=raw.get("model"),
        effort=raw.get("effort"),
    )


def _parse_backlog_file(filename: str, content: str) -> dict[str, Any]:
    """Parse a backlog markdown file into a task creation payload.

    Extracts title, role, priority, and description from the markdown.
    Falls back to safe defaults for any missing fields.

    Args:
        filename: The filename (e.g. "100-fix-the-bug.md"), used to derive a
            slug for the title when no H1 heading is found.
        content: Full markdown text of the backlog file.

    Returns:
        Dict suitable for POST /tasks.
    """
    lines = content.splitlines()

    # Title: first H1 line, strip leading "# " and numeric prefix like "100 -- "
    title = filename.replace(".md", "").replace("-", " ")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            raw = stripped[2:].strip()
            raw = re.sub(r"^\d+\s*--\s*", "", raw)
            title = raw
            break

    # Role: **Role:** backend
    role = "backend"
    role_match = re.search(r"\*\*Role:\*\*\s*(\S+)", content)
    if role_match:
        role = role_match.group(1).strip()

    # Priority: **Priority:** 2
    priority = 2
    priority_match = re.search(r"\*\*Priority:\*\*\s*(\d+)", content)
    if priority_match:
        priority = int(priority_match.group(1))

    # Description: everything after the header/front-matter lines
    desc_lines: list[str] = []
    past_header = False
    for line in lines:
        stripped = line.strip()
        if not past_header:
            if stripped.startswith("# ") or re.match(r"\*\*\w+:\*\*", stripped):
                past_header = True
                continue
            continue
        if re.match(r"\*\*\w+:\*\*", stripped):
            continue
        desc_lines.append(line)
    description = "\n".join(desc_lines).strip() or content.strip()

    return {
        "title": title,
        "description": description,
        "role": role,
        "priority": priority,
        "scope": "medium",
        "complexity": "medium",
    }


def _fetch_all_tasks(client: httpx.Client, base_url: str) -> dict[str, list[Task]]:
    """GET /tasks (no filter) and bucket results by status in one round-trip.

    Args:
        client: httpx client.
        base_url: Server base URL.

    Returns:
        Dict mapping status string → list of Tasks.  Always includes keys
        "open", "claimed", "done", "failed", "cancelled" even if empty.
        NOTE: "open" here includes tasks with unmet dependencies; callers
        that need the server-filtered view should apply their own dep check.
    """
    resp = client.get(f"{base_url}/tasks")
    resp.raise_for_status()
    by_status: dict[str, list[Task]] = defaultdict(list)
    for raw in resp.json():
        task = _task_from_dict(raw)
        by_status[task.status.value].append(task)
    # Ensure standard keys are always present
    for key in ("open", "claimed", "done", "failed", "cancelled"):
        by_status.setdefault(key, [])
    return dict(by_status)


def _fail_task(client: httpx.Client, base_url: str, task_id: str, reason: str) -> None:
    """POST /tasks/{task_id}/fail to mark a task as failed.

    Args:
        client: httpx client.
        base_url: Server base URL.
        task_id: ID of the task to fail.
        reason: Why the task failed.
    """
    resp = client.post(f"{base_url}/tasks/{task_id}/fail", json={"reason": reason})
    resp.raise_for_status()


def group_by_role(tasks: list[Task], max_per_batch: int) -> list[list[Task]]:
    """Group open tasks by role into batches of up to max_per_batch.

    Tasks are sorted by priority (ascending, 1=critical first) within each
    role before batching. Upgrade proposal tasks get a priority boost
    (effective priority reduced by 1) to ensure self-evolution tasks are
    processed promptly.

    Args:
        tasks: Open tasks to batch.
        max_per_batch: Maximum tasks per batch (typically 1-3).

    Returns:
        List of batches, each a list of same-role tasks.
    """
    by_role: dict[str, list[Task]] = defaultdict(list)
    for task in tasks:
        by_role[task.role].append(task)

    batches: list[list[Task]] = []
    for role_tasks in by_role.values():
        # Sort by effective priority: upgrade proposals get a boost (lower priority value)
        def _sort_key(t: Task) -> tuple[int, int]:
            # Priority boost for upgrade proposals: subtract 1 from priority value
            # (lower = higher priority). Second element is original priority for ties.
            priority_boost = t.priority - 1 if t.task_type == TaskType.UPGRADE_PROPOSAL else t.priority
            return (priority_boost, t.priority)

        role_tasks.sort(key=_sort_key)
        for i in range(0, len(role_tasks), max_per_batch):
            batches.append(role_tasks[i : i + max_per_batch])

    # Sort batches by best (lowest) priority so critical work goes first.
    batches.sort(key=lambda b: b[0].priority)
    return batches


def _compute_total_spent(workdir: Path) -> float:
    """Sum cost_efficiency metric values recorded for individual tasks.

    Reads all cost_efficiency_*.jsonl files in .sdd/metrics/ and returns the
    total cost in USD for entries that have a ``task_id`` label, avoiding
    double-counting the per-agent average entries that lack that label.

    Args:
        workdir: Project root directory.

    Returns:
        Total USD spent as recorded in metrics files.
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    total = 0.0
    for jsonl_file in metrics_dir.glob("cost_efficiency_*.jsonl"):
        try:
            for line in jsonl_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    point = json.loads(line)
                    if "task_id" in point.get("labels", {}):
                        total += point.get("value", 0.0)
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return total


class Orchestrator:
    """The main loop: watch tasks, spawn agents, verify completion, repeat.

    The orchestrator is a deterministic scheduler. It never calls an LLM
    directly. It polls the task server, groups work into batches, spawns
    short-lived agents via the spawner, and verifies done tasks via the
    janitor.

    Args:
        config: Orchestrator tuning knobs.
        spawner: Agent spawner (owns the CLI adapter).
        workdir: Project working directory for janitor verification.
        client: httpx client for server communication (injectable for testing).
    """

    _SPAWN_BACKOFF_S: float = 60.0   # seconds before retrying a failed batch
    _MAX_SPAWN_FAILURES: int = 3     # consecutive failures before marking tasks failed

    def __init__(
        self,
        config: OrchestratorConfig,
        spawner: AgentSpawner,
        workdir: Path,
        client: httpx.Client | None = None,
        evolution: EvolutionCoordinator | None = None,
        router: TierAwareRouter | None = None,
    ) -> None:
        self._config = config
        self._spawner = spawner
        self._workdir = workdir
        self._client = client or httpx.Client(
            timeout=10.0,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
        self._agents: dict[str, AgentSession] = {}
        self._file_ownership: dict[str, str] = {}  # filepath → agent_id
        self._processed_done_tasks: set[str] = set()  # avoid re-processing done tasks
        self._retried_task_ids: set[str] = set()  # tasks that already have a retry queued
        self._running = False
        self._tick_count = 0
        # Track spawn failures per batch for backoff: task_ids → (fail_count, last_fail_ts)
        self._spawn_failures: dict[frozenset[str], tuple[int, float]] = {}
        # Track last backlog replenishment timestamp
        self._last_replenish_ts: float = 0.0
        # Run completion summary state
        self._summary_written: bool = False
        self._run_start_ts: float = time.time()
        # Background thread pool for non-blocking ruff/pytest runs
        self._executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self._pending_ruff_future: concurrent.futures.Future[list[dict[str, Any]]] | None = None
        self._pending_test_future: concurrent.futures.Future[dict[str, Any]] | None = None

        # Provider-aware routing and health tracking
        self._router = router
        if self._router is not None and not self._router.state.providers:
            providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
            if providers_yaml.exists():
                load_providers_from_yaml(providers_yaml, self._router)

        # Self-evolution feedback loop
        if config.evolution_enabled:
            self._evolution = evolution or EvolutionCoordinator(
                state_dir=workdir / ".sdd",
            )
        else:
            self._evolution: EvolutionCoordinator | None = None

    @property
    def active_agents(self) -> dict[str, AgentSession]:
        """Currently tracked agent sessions, keyed by session id."""
        return dict(self._agents)

    # -- Core tick -----------------------------------------------------------

    def tick(self) -> TickResult:
        """Execute one orchestrator cycle.

        Steps:
            1. Fetch ALL tasks in one HTTP call and split by status.
            2. Group open tasks into role-based batches.
            3. Spawn agents if capacity allows.
            4. Check done tasks and run janitor.
            5. Reap dead/stale agents and fail their tasks.

        Returns:
            Summary of what happened this tick.
        """
        result = TickResult()
        self._tick_count += 1
        base = self._config.server_url
        _tick_http_reads = 0  # counts GET requests this tick (should stay at 1)

        # 0. Ingest any new backlog files before fetching tasks
        try:
            self.ingest_backlog()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("ingest_backlog failed: %s", exc)

        # 1. Fetch ALL tasks in a single round-trip and split by status.
        try:
            tasks_by_status = _fetch_all_tasks(self._client, base)
            _tick_http_reads += 1
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch tasks: %s", exc)
            result.errors.append(f"fetch_all: {exc}")
            return result

        logger.debug(
            "tick #%d: %d HTTP read(s) this tick (open=%d claimed=%d done=%d failed=%d)",
            self._tick_count,
            _tick_http_reads,
            len(tasks_by_status.get("open", [])),
            len(tasks_by_status.get("claimed", [])),
            len(tasks_by_status.get("done", [])),
            len(tasks_by_status.get("failed", [])),
        )

        # Server-filtered open tasks already exclude unmet deps when queried with
        # ?status=open, so replicate that filter client-side from the bulk fetch.
        done_tasks = tasks_by_status["done"]
        done_ids = {t.id for t in done_tasks}
        open_tasks = [
            t for t in tasks_by_status["open"]
            if all(dep in done_ids for dep in t.depends_on)
        ]
        result.open_tasks = len(open_tasks)
        # ready_tasks == open_tasks (dep filter already applied above)
        ready_tasks = open_tasks

        # 2. Group into batches
        batches = group_by_role(ready_tasks, self._config.max_tasks_per_agent)

        # 3. Count alive agents, spawn if capacity
        self._refresh_agent_states(tasks_by_status)
        alive_count = sum(1 for a in self._agents.values() if a.status != "dead")
        result.active_agents = alive_count

        # Track which task IDs are already assigned to active agents
        assigned_task_ids = set()
        for agent in self._agents.values():
            if agent.status != "dead":
                assigned_task_ids.update(agent.task_ids)

        # 3b. Claim tasks and spawn agents for ready batches (skip if budget is exhausted)
        if self._config.dry_run:
            for batch in batches:
                for task in batch:
                    logger.info(
                        "[DRY RUN] Would spawn %s agent for: %s (model=%s, effort=%s)",
                        task.role,
                        task.title,
                        task.model,
                        task.effort,
                    )
                    result.dry_run_planned.append((task.role, task.title, task.model, task.effort))
        elif self._config.budget_usd > 0:
            total_spent = _compute_total_spent(self._workdir)
            if total_spent >= self._config.budget_usd:
                logger.warning(
                    "Budget cap ($%.2f) reached ($%.2f spent), skipping agent spawning",
                    self._config.budget_usd,
                    total_spent,
                )
            else:
                self._claim_and_spawn_batches(batches, alive_count, assigned_task_ids, done_ids, result)
        else:
            self._claim_and_spawn_batches(batches, alive_count, assigned_task_ids, done_ids, result)

        # 4. Check done tasks, run janitor, record evolution metrics
        self._process_completed_tasks(done_tasks, result)

        # 4b. Use cached failed tasks and maybe retry with escalation
        failed_tasks = tasks_by_status["failed"]
        for task in failed_tasks:
            if self._maybe_retry_task(task):
                result.retried.append(task.id)

        # 5. Reap dead/stale agents and fail their tasks
        self._reap_dead_agents(result, tasks_by_status)

        # 6. Run evolution analysis cycle every N ticks
        if self._evolution is not None and self._tick_count % self._config.evolution_tick_interval == 0:
            self._run_evolution_cycle(result)

        # 6b. Refresh knowledge base every 5 evolution intervals
        if self._tick_count % (self._config.evolution_tick_interval * 5) == 0:
            try:
                refresh_knowledge_base(self._workdir)
            except Exception as exc:
                logger.warning("Knowledge base refresh failed: %s", exc)

        # 7. Check evolve mode: if all tasks done and no agents alive, trigger new cycle
        self._check_evolve(result, tasks_by_status)

        # 8. Replenish backlog in evolve mode when tasks run out
        self._replenish_backlog(result)

        # 8b. Generate run completion summary for non-evolve runs (reuse cached tasks)
        if (
            not self._config.evolve_mode
            and result.open_tasks == 0
            and result.active_agents == 0
            and not self._summary_written
        ):
            self._generate_run_summary(tasks_by_status["done"], tasks_by_status["failed"])

        # 9. Log summary
        self._log_summary(result)

        return result

    def run(self) -> None:
        """Run the orchestrator loop until stopped.

        Blocks the calling thread. Call ``stop()`` from another thread or
        a signal handler to break the loop. Individual tick failures are
        caught and logged so a single bad tick cannot kill the loop.
        """
        self._running = True
        logger.info(
            "Orchestrator started (poll=%ds, max_agents=%d, server=%s)",
            self._config.poll_interval_s,
            self._config.max_agents,
            self._config.server_url,
        )
        consecutive_failures = 0
        max_consecutive_failures = 10
        while self._running:
            try:
                self.tick()
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "Tick %d failed (%d consecutive failures)",
                    self._tick_count,
                    consecutive_failures,
                )
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        "Stopping after %d consecutive tick failures",
                        consecutive_failures,
                    )
                    break
            if self._config.dry_run:
                break
            time.sleep(self._config.poll_interval_s)

            # Check if a restart was requested (own source code changed)
            restart_flag = self._workdir / ".sdd" / "runtime" / "restart_requested"
            if restart_flag.exists():
                restart_flag.unlink(missing_ok=True)
                logger.info("Restarting orchestrator (own code updated)")
                self._restart()
                return  # _restart calls os.execv, but just in case

        logger.info("Orchestrator stopped")

    def stop(self) -> None:
        """Signal the run loop to exit after the current tick."""
        self._running = False

    def _restart(self) -> None:
        """Replace the current process with a fresh orchestrator.

        Uses os.execv to re-exec with the same arguments, picking up
        any code changes made by agents to src/bernstein/.
        """
        import os
        import sys

        logger.info("Exec'ing fresh orchestrator process")
        # Re-exec the same command that started us
        os.execv(sys.executable, [sys.executable, *sys.argv])

    # -- Evolve mode ---------------------------------------------------------

    # Priority rotation for evolve mode — each cycle emphasizes a different area
    _EVOLVE_FOCUS_AREAS: ClassVar[list[str]] = [
        "new_features",
        "test_coverage",
        "code_quality",
        "performance",
        "documentation",
    ]

    def _check_evolve(self, result: TickResult, tasks_by_status: dict[str, list[Task]]) -> None:
        """If evolve mode is on and all tasks are done, trigger a new cycle.

        Full cycle: analyze → verify → commit → plan → execute.
        Tracks budget, detects diminishing returns with backoff, rotates priorities.

        Args:
            result: Current tick result (mutated in place).
            tasks_by_status: Pre-fetched task snapshot keyed by status string,
                produced by _fetch_all_tasks().  Avoids extra HTTP round-trips.
        """
        evolve_path = self._workdir / ".sdd" / "runtime" / "evolve.json"
        if not evolve_path.exists():
            return

        try:
            evolve_cfg = json.loads(evolve_path.read_text())
        except (OSError, json.JSONDecodeError):
            return

        if not evolve_cfg.get("enabled"):
            return

        # Only trigger when idle: no open/claimed tasks, no alive agents
        open_tasks = tasks_by_status.get("open", [])
        claimed_tasks = tasks_by_status.get("claimed", [])
        alive = sum(1 for a in self._agents.values() if a.status != "dead")
        if open_tasks or claimed_tasks or alive > 0:
            return  # Still working

        # Check cycle limits
        cycle_count = evolve_cfg.get("_cycle_count", 0)
        max_cycles = evolve_cfg.get("max_cycles", 0)
        if max_cycles > 0 and cycle_count >= max_cycles:
            logger.info("Evolve: max cycles (%d) reached, stopping", max_cycles)
            return

        # Check budget cap
        budget_usd = evolve_cfg.get("budget_usd", 0)
        spent_usd = evolve_cfg.get("_spent_usd", 0.0)
        if budget_usd > 0 and spent_usd >= budget_usd:
            logger.info("Evolve: budget cap ($%.2f) reached, stopping", budget_usd)
            return

        # Diminishing returns backoff: if N consecutive cycles produced zero
        # successful changes, increase the interval exponentially (max 8x)
        consecutive_empty = evolve_cfg.get("_consecutive_empty", 0)
        backoff_factor = min(2 ** consecutive_empty, 8) if consecutive_empty >= 3 else 1

        last_cycle_ts = evolve_cfg.get("_last_cycle_ts", 0)
        base_interval = evolve_cfg.get("interval_s", 300)
        effective_interval = base_interval * backoff_factor
        if time.time() - last_cycle_ts < effective_interval:
            return

        cycle_number = cycle_count + 1
        cycle_start = time.time()
        logger.info(
            "Evolve: triggering cycle %d (backoff=%dx, interval=%ds)",
            cycle_number, backoff_factor, effective_interval,
        )

        # Step 1: ANALYZE — count results from last cycle (use cached snapshot)
        tasks_completed = len(tasks_by_status.get("done", []))
        tasks_failed = len(tasks_by_status.get("failed", []))

        # Step 2: VERIFY — run tests to get current state
        test_info = self._evolve_run_tests()

        # Step 3: COMMIT — auto-commit if tests pass
        committed = self._evolve_auto_commit()

        # Step 4: PLAN — spawn manager with priority rotation
        focus = self._EVOLVE_FOCUS_AREAS[cycle_count % len(self._EVOLVE_FOCUS_AREAS)]
        self._evolve_spawn_manager(
            cycle_number=cycle_number,
            focus_area=focus,
            test_summary=test_info.get("summary", ""),
        )

        # Track diminishing returns
        produced_changes = committed or tasks_completed > 0
        if produced_changes:
            evolve_cfg["_consecutive_empty"] = 0
        else:
            evolve_cfg["_consecutive_empty"] = consecutive_empty + 1

        # Update state
        now = time.time()
        evolve_cfg["_cycle_count"] = cycle_number
        evolve_cfg["_last_cycle_ts"] = now
        with contextlib.suppress(OSError):
            evolve_path.write_text(json.dumps(evolve_cfg))

        # Log cycle metrics
        self._log_evolve_cycle(cycle_number, now, {
            "focus_area": focus,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "tests_passed": test_info.get("passed", 0),
            "tests_failed": test_info.get("failed", 0),
            "commits_made": 1 if committed else 0,
            "backoff_factor": backoff_factor,
            "consecutive_empty": evolve_cfg.get("_consecutive_empty", 0),
            "duration_s": round(now - cycle_start, 2),
        })

    _REPLENISH_COOLDOWN_S: float = 60.0
    _REPLENISH_MAX_TASKS: int = 5

    def _run_ruff_check(self) -> list[dict[str, Any]]:
        """Run ruff check and return parsed violations (runs in a background thread)."""
        import subprocess

        proc = subprocess.run(
            ["uv", "run", "ruff", "check", ".", "--output-format", "json"],
            capture_output=True,
            text=True,
            cwd=self._workdir,
            timeout=60,
        )
        return json.loads(proc.stdout) if proc.stdout.strip() else []

    def _create_ruff_tasks(self, violations: list[dict[str, Any]]) -> None:
        """Create backlog tasks from ruff violations."""
        if not violations:
            logger.debug("Replenish: no ruff violations found, backlog is clean")
            return

        by_rule: dict[str, dict[str, Any]] = {}
        for v in violations:
            code = (v.get("code") or "unknown").strip()
            if code not in by_rule:
                by_rule[code] = v

        base = self._config.server_url
        created = 0
        for code, v in by_rule.items():
            if created >= self._REPLENISH_MAX_TASKS:
                break
            filename = v.get("filename", "")
            message = v.get("message", "")
            row = v.get("location", {}).get("row", "?")
            task_payload = {
                "title": f"Fix ruff violation {code}",
                "description": (
                    f"Fix all occurrences of ruff rule {code}.\n"
                    f"Example: {filename}:{row} — {message}\n"
                    f"Run `uv run ruff check . --select {code}` to find all instances."
                ),
                "role": "backend",
                "priority": 3,
                "model": "sonnet",
                "effort": "low",
            }
            try:
                resp = self._client.post(f"{base}/tasks", json=task_payload)
                resp.raise_for_status()
                created += 1
                logger.info("Replenish: created task for ruff rule %s", code)
            except httpx.HTTPError as exc:
                logger.warning("Replenish: failed to create task for %s: %s", code, exc)

        if created:
            logger.info("Replenish: created %d lint-fix task(s)", created)

    def _replenish_backlog(self, result: TickResult) -> None:
        """Create fix tasks from ruff lint violations when evolve mode is idle.

        Only runs when:
        - evolve_mode is enabled in config
        - open_tasks == 0
        - At least 60 seconds since last replenishment

        Ruff is run in a background thread so the tick loop is never blocked.
        On the first eligible tick a future is submitted; on subsequent ticks
        the result is harvested once the future completes.

        Caps at 5 tasks per cycle to avoid flooding the task server.

        Args:
            result: Current tick result (used to read open_tasks count).
        """
        if not self._config.evolve_mode:
            return
        if result.open_tasks > 0:
            return

        # Harvest a completed ruff future
        if self._pending_ruff_future is not None:
            if not self._pending_ruff_future.done():
                return  # still running; skip this tick
            try:
                violations: list[dict[str, Any]] = self._pending_ruff_future.result()
            except Exception as exc:
                logger.warning("Replenish: ruff check failed: %s", exc)
                self._pending_ruff_future = None
                return
            self._pending_ruff_future = None
            self._create_ruff_tasks(violations)
            return

        # Check cooldown before submitting a new run
        now = time.time()
        if now - self._last_replenish_ts < self._REPLENISH_COOLDOWN_S:
            return

        self._last_replenish_ts = now
        self._pending_ruff_future = self._executor.submit(self._run_ruff_check)
        logger.debug("Replenish: ruff check submitted to background thread")

    def _run_pytest(self) -> dict[str, Any]:
        """Run pytest and return parsed results (runs in a background thread)."""
        import subprocess

        info: dict[str, Any] = {"passed": 0, "failed": 0, "summary": ""}
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/", "-q", "--tb=line"],
            capture_output=True, text=True, cwd=self._workdir, timeout=300,
        )
        output = result.stdout + result.stderr
        info["summary"] = output.strip().splitlines()[-1] if output.strip() else ""
        match = re.search(r"(\d+) passed", output)
        if match:
            info["passed"] = int(match.group(1))
        match = re.search(r"(\d+) failed", output)
        if match:
            info["failed"] = int(match.group(1))
        return info

    def _evolve_run_tests(self) -> dict[str, Any]:
        """Return test results from a background pytest run.

        On the first call a future is submitted and an empty result is returned.
        On subsequent calls the future is checked; once done the result is
        harvested and a new future is submitted for the next cycle.

        Returns:
            Dict with 'passed', 'failed', 'summary' keys.
        """
        info: dict[str, Any] = {"passed": 0, "failed": 0, "summary": ""}

        if self._pending_test_future is not None:
            if not self._pending_test_future.done():
                return info  # still running; return empty until done
            try:
                info = self._pending_test_future.result()
            except Exception as exc:
                logger.warning("Evolve: test run failed: %s", exc)
                info["summary"] = f"test run error: {exc}"
            self._pending_test_future = None
            return info

        # Submit a new test run in the background
        self._pending_test_future = self._executor.submit(self._run_pytest)
        return info  # results available on the next call

    def _evolve_auto_commit(self) -> bool:
        """Auto-commit and push any uncommitted changes from the last cycle.

        Returns:
            True if a commit was made, False otherwise.
        """
        import subprocess

        try:
            # Check for changes
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=self._workdir, timeout=10,
            )
            if not status.stdout.strip():
                return False  # Nothing to commit

            # Stage all changes (except .sdd/runtime/)
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self._workdir, timeout=10,
            )
            # Unstage runtime artifacts
            subprocess.run(
                ["git", "reset", "HEAD", "--", ".sdd/runtime/", ".sdd/metrics/"],
                capture_output=True, cwd=self._workdir, timeout=10,
            )

            # Run tests before committing
            test_result = subprocess.run(
                ["uv", "run", "pytest", "tests/", "-x", "-q", "--tb=line"],
                capture_output=True, text=True, cwd=self._workdir, timeout=300,
            )
            if test_result.returncode != 0:
                logger.warning("Evolve: tests failed, rolling back changes")
                subprocess.run(
                    ["git", "checkout", "--", "."],
                    cwd=self._workdir, timeout=10,
                )
                return False

            # Commit
            subprocess.run(
                ["git", "commit", "-m", "Auto-evolve: improvements from self-development cycle"],
                cwd=self._workdir, timeout=10,
            )

            # Push
            subprocess.run(
                ["git", "push", "origin", "HEAD"],
                capture_output=True, cwd=self._workdir, timeout=30,
            )
            logger.info("Evolve: auto-committed and pushed changes")

            # Check if own source code changed — if so, signal restart
            changed = status.stdout.strip()
            if "src/bernstein/" in changed:
                logger.info("Evolve: own source code changed, signaling restart")
                restart_flag = self._workdir / ".sdd" / "runtime" / "restart_requested"
                restart_flag.parent.mkdir(parents=True, exist_ok=True)
                restart_flag.write_text(str(time.time()))

            return True

        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Evolve: auto-commit failed: %s", exc)
            return False

    def _evolve_spawn_manager(
        self,
        cycle_number: int = 0,
        focus_area: str = "new_features",
        test_summary: str = "",
    ) -> None:
        """Spawn a manager agent to analyze the codebase and create new tasks.

        When Tavily API is available, runs web research first and includes
        market context in the manager task description.

        Args:
            cycle_number: Current evolve cycle number.
            focus_area: Priority rotation focus for this cycle.
            test_summary: Latest test run summary line.
        """
        base = self._config.server_url

        # Run web research if Tavily is available
        research_context = ""
        try:
            from bernstein.core.researcher import format_research_context, run_research_sync

            report = run_research_sync(self._workdir)
            research_context = format_research_context(report)
            if research_context:
                logger.info("Evolve: research produced %d bytes of context", len(research_context))
        except (ImportError, OSError, RuntimeError) as exc:
            logger.debug("Evolve: research unavailable: %s", exc)

        focus_instructions = {
            "new_features": "Focus on missing features that block real usage.",
            "test_coverage": "Focus on test gaps and missing edge-case coverage.",
            "code_quality": "Focus on code smells, type safety, and refactoring.",
            "performance": "Focus on performance bottlenecks and efficiency.",
            "documentation": "Focus on missing docs that block contributors.",
        }
        focus_text = focus_instructions.get(focus_area, "Focus on high-impact improvements.")

        description = (
            f"You are a PRODUCT DIRECTOR in EVOLVE mode (cycle {cycle_number}). "
            "Think strategically: what would make this project genuinely useful "
            "to developers? What do competitors lack? What's the shortest path "
            "to a feature that gets people excited?\n\n"
            "Create tasks for specialist agents to implement. "
            "You plan, they code.\n\n"
            f"## This cycle's focus: {focus_area.replace('_', ' ')}\n"
            f"{focus_text}\n\n"
            + (f"## Current test state\n```\n{test_summary}\n```\n\n" if test_summary else "")
            + "## Rules (from self-evolving systems research)\n"
            "- NEVER create tasks that are cosmetic, trivial, or busy-work\n"
            "- Each task must have a measurable outcome (test passes, "
            "benchmark improves, bug is fixed)\n"
            "- Prefer config/prompt changes over code changes (cheaper, safer)\n"
            "- If tests already pass at 100%, focus on functionality, not more tests\n"
            "- If architecture is clean, focus on features users actually need\n"
            "- Create 3-5 tasks MAX. Quality over quantity.\n\n"
            "## Prioritization\n"
            "1. Bugs and broken functionality (P1)\n"
            "2. Missing features that block real usage (P1)\n"
            "3. Performance and reliability (P2)\n"
            "4. Code quality and test gaps (P2)\n"
            "5. Documentation (P3 — only if truly missing)\n\n"
            "## Process\n"
            "1. Run `uv run pytest tests/ -q` to see current test state\n"
            "2. Read key files to understand architecture\n"
            "3. Identify 3-5 high-impact improvements\n"
            "4. Create tasks via HTTP. YOU decide model and effort per task:\n"
            f"   curl -X POST {base}/tasks -H 'Content-Type: application/json' \\\n"
            "   -d '{\"title\": \"...\", \"description\": \"...\", "
            "\"role\": \"backend\", \"priority\": 2, "
            "\"model\": \"sonnet\", \"effort\": \"high\"}'\n\n"
            "## Model/effort selection (you decide per task)\n"
            "- model: \"opus\" (deep reasoning, slow) or \"sonnet\" (fast, default)\n"
            "- effort: \"max\" (100 turns), \"high\" (50), \"medium\" (30), \"low\" (15)\n"
            "- Use sonnet/high for most implementation tasks (fast)\n"
            "- Use opus/max ONLY for complex architecture or security reviews\n"
            "- Use sonnet/low for simple fixes, typos, config changes\n\n"
            "## Task size — KEEP THEM SMALL\n"
            "Each task MUST be completable in ONE file change, under 10 minutes.\n"
            "BAD: 'Implement entire web research module'\n"
            "GOOD: 'Add Tavily search function to researcher.py'\n"
            "GOOD: 'Add --evolve flag handling to cli/main.py'\n"
            "Break big features into 3-5 atomic file-level tasks.\n\n"
            "## README\n"
            "Every 3rd cycle, create a task to update README.md with:\n"
            "- Current feature state, correct CLI usage, accurate test count.\n\n"
            "5. Then exit.\n\n"
            "IMPORTANT: Do NOT implement changes yourself. Only create tasks."
        )

        if research_context:
            description += research_context

        task_body = {
            "title": f"Evolve cycle {cycle_number}: {focus_area.replace('_', ' ')}",
            "description": description,
            "role": "manager",
            "priority": 1,
            "scope": "medium",
            "complexity": "medium",
        }

        try:
            resp = self._client.post(f"{base}/tasks", json=task_body)
            resp.raise_for_status()
            task_id = resp.json().get("id", "?")
            logger.info("Evolve: created manager task %s (focus=%s)", task_id, focus_area)
        except httpx.HTTPError as exc:
            logger.error("Evolve: failed to create manager task: %s", exc)

    def _log_evolve_cycle(
        self,
        cycle_number: int,
        timestamp: float,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Append an entry to the evolve_cycles.jsonl log.

        Args:
            cycle_number: The 1-based cycle number.
            timestamp: Unix timestamp of this cycle.
            metrics: Additional cycle metrics to include.
        """
        metrics_dir = self._workdir / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        log_path = metrics_dir / "evolve_cycles.jsonl"
        entry: dict[str, Any] = {
            "cycle": cycle_number,
            "timestamp": timestamp,
            "iso_time": datetime.fromtimestamp(timestamp, tz=UTC).isoformat(),
            "tick": self._tick_count,
        }
        if metrics:
            entry.update(metrics)
        try:
            with log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("Evolve: failed to write cycle log: %s", exc)

    # -- Evolution integration -----------------------------------------------

    def _run_evolution_cycle(self, result: TickResult) -> None:
        """Run an evolution analysis cycle and create upgrade tasks from proposals.

        Steps:
            1. Run analysis to generate proposals from metrics.
            2. Persist pending proposals to .sdd/upgrades/pending.json.
            3. Execute any auto-approved proposals via the UpgradeExecutor.
            4. Roll back failed executions.
            5. Create server tasks for remaining pending proposals.

        Args:
            result: Current tick result to record errors into.
        """
        assert self._evolution is not None
        try:
            proposals = self._evolution.run_analysis_cycle()

            # Persist pending proposals
            self._persist_pending_proposals()

            # Execute approved proposals; rollback on failure
            executed = self._evolution.execute_pending_upgrades()
            for proposal in executed:
                logger.info(
                    "Applied upgrade %s: %s (status=%s)",
                    proposal.id, proposal.title, proposal.status.value,
                )

            if not proposals:
                return

            base = self._config.server_url
            for proposal in proposals:
                try:
                    task_body = {
                        "title": f"Upgrade: {proposal.title}",
                        "description": proposal.description,
                        "role": "backend",
                        "priority": 2,
                        "scope": "medium",
                        "complexity": "medium",
                        "estimated_minutes": 30,
                        "task_type": TaskType.UPGRADE_PROPOSAL.value,
                    }
                    resp = self._client.post(f"{base}/tasks", json=task_body)
                    resp.raise_for_status()
                    logger.info(
                        "Created upgrade task for proposal %s: %s",
                        proposal.id, proposal.title,
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "Failed to create upgrade task for proposal %s: %s",
                        proposal.id, exc,
                    )
                    result.errors.append(f"evolution_task: {exc}")
        except Exception as exc:
            logger.error("Evolution analysis cycle failed: %s", exc)
            result.errors.append(f"evolution: {exc}")

    def _persist_pending_proposals(self) -> None:
        """Write pending upgrade proposals to .sdd/upgrades/pending.json."""
        if self._evolution is None:
            return
        upgrades_dir = self._workdir / ".sdd" / "upgrades"
        upgrades_dir.mkdir(parents=True, exist_ok=True)
        pending_path = upgrades_dir / "pending.json"
        pending = self._evolution.get_pending_upgrades()
        data = [
            {
                "id": p.id,
                "title": p.title,
                "category": p.category.value,
                "description": p.description,
                "status": p.status.value,
                "confidence": p.confidence,
                "created_at": p.created_at,
            }
            for p in pending
        ]
        pending_path.write_text(json.dumps(data, indent=2))

    # -- Internal helpers ----------------------------------------------------

    def _refresh_agent_states(self, tasks_snapshot: dict[str, list[Task]]) -> None:
        """Update alive/dead status for all tracked agents.

        When an agent process dies, handles orphaned tasks via the agent
        completion protocol: checks task status on the server, runs janitor
        verification if completion signals exist, and completes or fails
        accordingly. Also releases file ownership and emits metrics.

        Args:
            tasks_snapshot: Pre-fetched tasks bucketed by status from this tick.
        """
        for session in list(self._agents.values()):
            if session.status == "dead":
                continue
            if not self._spawner.check_alive(session):
                session.status = "dead"
                # Release file ownership for this agent
                self._release_file_ownership(session.id)
                # Handle orphaned tasks
                for task_id in session.task_ids:
                    self._handle_orphaned_task(task_id, session, tasks_snapshot)

    def _handle_orphaned_task(
        self,
        task_id: str,
        session: AgentSession,
        tasks_snapshot: dict[str, list[Task]],
    ) -> None:
        """Handle a task left behind by a dead agent process.

        Checks task status using the pre-fetched snapshot (no extra HTTP call).
        Falls back to a live fetch only if the task is not found in the snapshot.
        Runs janitor verification if the task has completion signals, and marks
        it complete or failed. Emits a MetricsRecord afterward.

        Args:
            task_id: ID of the orphaned task.
            session: The dead agent's session.
            tasks_snapshot: Pre-fetched tasks bucketed by status from this tick.
        """
        base = self._config.server_url
        start_ts = session.heartbeat_ts if session.heartbeat_ts > 0 else time.time()
        success = False
        error_type: str | None = None

        # Try to find the task in the pre-fetched snapshot first (avoids HTTP call)
        all_cached: list[Task] = []
        for bucket in tasks_snapshot.values():
            all_cached.extend(bucket)
        task_by_id = {t.id: t for t in all_cached}

        if task_id in task_by_id:
            task = task_by_id[task_id]
            logger.debug("_handle_orphaned_task %s: resolved from tick snapshot", task_id)
        else:
            # Not in snapshot — fall back to a live fetch
            try:
                resp = self._client.get(f"{base}/tasks/{task_id}")
                resp.raise_for_status()
                task = _task_from_dict(resp.json())
                logger.debug("_handle_orphaned_task %s: fetched live (not in snapshot)", task_id)
            except httpx.HTTPError as exc:
                logger.error("Failed to fetch orphaned task %s: %s", task_id, exc)
                error_type = "fetch_failed"
                self._emit_orphan_metrics(
                    task_id, session, start_ts, success=False, error_type=error_type,
                )
                return

        status = task.status
        if status not in (TaskStatus.OPEN, TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
            logger.info(
                "Orphaned task %s already resolved (status=%s), skipping",
                task_id, status.value,
            )
            return

        # Collect structured completion data from agent log
        completion_data = self._collect_completion_data(session)

        if task.completion_signals:
            passed, failed_signals = verify_task(task, self._workdir)
            if passed:
                try:
                    result_payload: dict[str, Any] = {
                        "result_summary": f"Auto-completed after agent {session.id} died; janitor passed",
                        **completion_data,
                    }
                    self._client.post(
                        f"{base}/tasks/{task_id}/complete",
                        json=result_payload,
                    )
                    success = True
                    logger.info(
                        "Orphaned task %s auto-completed (janitor passed) after agent %s died",
                        task_id, session.id,
                    )
                except httpx.HTTPError as exc:
                    logger.error("Failed to complete orphaned task %s: %s", task_id, exc)
                    error_type = "complete_failed"
            else:
                try:
                    self._retry_or_fail_task(
                        task_id,
                        f"Agent {session.id} died; janitor failed: {failed_signals}",
                    )
                    logger.info(
                        "Orphaned task %s retry/failed (janitor failed: %s) after agent %s died",
                        task_id, failed_signals, session.id,
                    )
                except httpx.HTTPError as exc:
                    logger.error("Failed to retry/fail orphaned task %s: %s", task_id, exc)
                error_type = "janitor_failed"
        else:
            # No completion signals -- retry or fail the task
            try:
                self._retry_or_fail_task(
                    task_id,
                    f"Agent {session.id} died; no completion signals to verify",
                )
                logger.info(
                    "Orphaned task %s retry/failed (no signals) after agent %s died",
                    task_id, session.id,
                )
            except httpx.HTTPError as exc:
                logger.error("Failed to retry/fail orphaned task %s: %s", task_id, exc)
            error_type = "no_signals"

        self._emit_orphan_metrics(
            task_id, session, start_ts, success=success, error_type=error_type,
        )
        self._record_provider_health(session, success=success)

    def _emit_orphan_metrics(
        self,
        task_id: str,
        session: AgentSession,
        start_ts: float,
        *,
        success: bool,
        error_type: str | None,
    ) -> None:
        """Write a 14-field MetricsRecord to .sdd/metrics/YYYY-MM-DD.jsonl.

        Args:
            task_id: The task ID.
            session: The agent session that died.
            start_ts: Approximate start timestamp of the agent run.
            success: Whether the orphaned task was auto-completed.
            error_type: Error category, or None on success.
        """
        now = time.time()
        record = MetricsRecord(
            timestamp=datetime.now(UTC).isoformat(),
            task_id=task_id,
            agent_id=session.id,
            role=session.role,
            model_used=session.model_config.model,
            duration_seconds=round(now - start_ts, 2),
            token_count=0,
            cost_usd=0.0,
            success=success,
            error_type=error_type,
            files_modified=0,
            test_pass_rate=1.0 if success else 0.0,
            retry_count=0,
            step_count=0,
        )
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        metrics_dir = self._workdir / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        metrics_path = metrics_dir / f"{today}.jsonl"
        with metrics_path.open("a") as f:
            f.write(json.dumps(record.to_dict()) + "\n")

    def _collect_completion_data(self, session: AgentSession) -> dict[str, Any]:
        """Read agent log file and extract structured completion data.

        Parses the agent's runtime log for files_modified and test_results.

        Args:
            session: Agent session whose log to parse.

        Returns:
            Dict with files_modified and test_results keys.
        """
        data: dict[str, Any] = {"files_modified": [], "test_results": {}}
        log_path = self._workdir / ".sdd" / "runtime" / f"{session.id}.log"
        if not log_path.exists():
            return data

        try:
            log_content = log_path.read_text(encoding="utf-8", errors="replace")
            # Extract file modifications (lines like "Modified: path/to/file")
            files_modified: list[str] = []
            for line in log_content.splitlines():
                stripped = line.strip()
                if stripped.startswith("Modified: ") or stripped.startswith("Created: "):
                    fpath = stripped.split(": ", 1)[1].strip()
                    if fpath and fpath not in files_modified:
                        files_modified.append(fpath)
            data["files_modified"] = files_modified

            # Extract test results (look for pytest-style summary)
            for line in reversed(log_content.splitlines()):
                stripped = line.strip()
                if "passed" in stripped or "failed" in stripped:
                    data["test_results"] = {"summary": stripped}
                    break
        except OSError as exc:
            logger.debug("Could not read agent log %s: %s", log_path, exc)

        return data

    def _check_file_overlap(self, batch: list[Task]) -> bool:
        """Check if any file in the batch is owned by an active agent.

        Args:
            batch: Tasks to check for file conflicts.

        Returns:
            True if there is a conflict, False if safe to spawn.
        """
        for task in batch:
            for fpath in task.owned_files:
                if fpath in self._file_ownership:
                    owner = self._file_ownership[fpath]
                    # Only conflict if the owning agent is still alive
                    owner_session = self._agents.get(owner)
                    if owner_session and owner_session.status != "dead":
                        logger.debug(
                            "File %s owned by active agent %s, skipping batch",
                            fpath, owner,
                        )
                        return True
        return False

    def _claim_file_ownership(self, agent_id: str, tasks: list[Task]) -> None:
        """Register file ownership for files in the given tasks.

        Args:
            agent_id: The agent claiming ownership.
            tasks: Tasks whose owned_files to claim.
        """
        for task in tasks:
            for fpath in task.owned_files:
                self._file_ownership[fpath] = agent_id

    def _release_file_ownership(self, agent_id: str) -> None:
        """Release all files owned by the given agent.

        Args:
            agent_id: The agent whose files to release.
        """
        to_remove = [fp for fp, owner in self._file_ownership.items() if owner == agent_id]
        for fp in to_remove:
            del self._file_ownership[fp]

    def _maybe_retry_task(self, task: Task) -> bool:
        """Queue a retry for a failed task with model/effort escalation.

        First retry bumps effort one level (low→medium→high→max), keeps model.
        Second retry escalates model (haiku→sonnet→opus) and resets effort to high.

        Args:
            task: The failed task to potentially retry.

        Returns:
            True if a retry task was created, False otherwise.
        """
        if task.id in self._retried_task_ids:
            return False

        # Determine current retry count from title prefix [RETRY N]
        retry_count = 0
        m = re.match(r"^\[RETRY (\d+)\] ", task.title)
        if m:
            retry_count = int(m.group(1))

        if retry_count >= self._config.max_task_retries:
            return False

        next_retry = retry_count + 1

        current_model = task.model or "sonnet"
        current_effort = task.effort or "high"

        effort_ladder = ["low", "medium", "high", "max"]
        model_ladder = ["haiku", "sonnet", "opus"]

        if next_retry == 1:
            # First retry: bump effort one level, keep model
            idx = effort_ladder.index(current_effort) if current_effort in effort_ladder else 2
            new_effort = effort_ladder[min(idx + 1, len(effort_ladder) - 1)]
            new_model = current_model
        else:
            # Second+ retry: escalate model, reset effort to high
            model_lower = current_model.lower()
            model_idx = 1  # default to sonnet position
            for i, name in enumerate(model_ladder):
                if name in model_lower:
                    model_idx = i
                    break
            new_model = model_ladder[min(model_idx + 1, len(model_ladder) - 1)]
            new_effort = "high"

        base_title = re.sub(r"^\[RETRY \d+\] ", "", task.title)
        new_title = f"[RETRY {next_retry}] {base_title}"
        new_description = f"[RETRY {next_retry}] {task.description}"

        payload: dict[str, Any] = {
            "title": new_title,
            "description": new_description,
            "role": task.role,
            "priority": task.priority,
            "scope": task.scope.value,
            "complexity": task.complexity.value,
            "estimated_minutes": task.estimated_minutes,
            "model": new_model,
            "effort": new_effort,
        }

        try:
            resp = self._client.post(
                f"{self._config.server_url}/tasks", json=payload
            )
            resp.raise_for_status()
            new_task_id = resp.json().get("id", "?")
            self._retried_task_ids.add(task.id)
            logger.info(
                "Retry %d queued for failed task %s → %s (model=%s effort=%s)",
                next_retry, task.id, new_task_id, new_model, new_effort,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to queue retry for task %s: %s", task.id, exc)
            return False

    def _find_session_for_task(self, task_id: str) -> AgentSession | None:
        """Return the agent session that owns *task_id*, or None.

        Args:
            task_id: ID of the task to look up.

        Returns:
            Matching AgentSession, or None if not found.
        """
        for session in self._agents.values():
            if task_id in session.task_ids:
                return session
        return None

    def _record_provider_health(
        self,
        session: AgentSession,
        success: bool,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        tokens: int = 0,
    ) -> None:
        """Update provider health and cost in the router based on task outcome.

        No-op when no router is configured or the session has no provider.

        Args:
            session: Agent session whose provider to update.
            success: Whether the task completed successfully.
            latency_ms: Approximate task latency in milliseconds.
            cost_usd: Cost of the task in USD.
            tokens: Number of tokens used.
        """
        if self._router is not None and session.provider is not None:
            self._router.update_provider_health(session.provider, success, latency_ms)
            if cost_usd > 0 or tokens > 0:
                self._router.record_provider_cost(session.provider, tokens, cost_usd)

    def _retry_or_fail_task(
        self,
        task_id: str,
        reason: str,
        tasks_snapshot: dict[str, list[Task]] | None = None,
    ) -> None:
        """Re-queue a task for retry, or fail it permanently if max retries reached.

        Reads the current retry count from a ``[retry:N]`` marker in the task
        description.  If the count is below ``max_task_retries`` a new open task
        is created (clone of the original with the marker bumped) and the old
        task is failed silently.  Once the limit is hit the task is failed with
        a "Max retries exceeded" reason.

        Args:
            task_id: ID of the task to retry or fail.
            reason: Human-readable reason for the failure / retry.
            tasks_snapshot: Optional pre-fetched tasks snapshot to avoid an
                extra HTTP round-trip when the task is already in cache.
        """
        base = self._config.server_url
        max_retries = self._config.max_task_retries

        # Try the pre-fetched snapshot first to avoid an extra GET
        task: Task | None = None
        if tasks_snapshot is not None:
            for bucket in tasks_snapshot.values():
                for t in bucket:
                    if t.id == task_id:
                        task = t
                        break
                if task is not None:
                    break
            if task is not None:
                logger.debug("_retry_or_fail_task %s: resolved from tick snapshot", task_id)

        if task is None:
            try:
                resp = self._client.get(f"{base}/tasks/{task_id}")
                resp.raise_for_status()
                task = _task_from_dict(resp.json())
            except httpx.HTTPError as exc:
                logger.error("_retry_or_fail_task: could not fetch task %s: %s", task_id, exc)
                return

        # Extract current retry count from description marker
        marker_re = re.compile(r"^\[retry:(\d+)\]\s*")
        m = marker_re.match(task.description)
        retry_count = int(m.group(1)) if m else 0
        base_description = marker_re.sub("", task.description)

        if retry_count < max_retries:
            new_description = f"[retry:{retry_count + 1}] {base_description}"
            task_body = {
                "title": task.title,
                "description": new_description,
                "role": task.role,
                "priority": task.priority,
                "scope": task.scope.value,
                "complexity": task.complexity.value,
                "estimated_minutes": task.estimated_minutes,
                "depends_on": task.depends_on,
                "owned_files": task.owned_files,
                "task_type": task.task_type.value,
            }
            try:
                self._client.post(f"{base}/tasks", json=task_body).raise_for_status()
                logger.info(
                    "Retrying task %s (attempt %d/%d): %s",
                    task_id, retry_count + 1, max_retries, reason,
                )
            except httpx.HTTPError as exc:
                logger.error("Failed to re-create task %s for retry: %s", task_id, exc)
                # Fall through to permanent fail
                _fail_task(self._client, base, task_id, f"Max retries exceeded: {reason}")
                return
            # Fail the old task silently (it has been replaced)
            with contextlib.suppress(httpx.HTTPError):
                _fail_task(self._client, base, task_id, f"Retried: {reason}")
        else:
            _fail_task(self._client, base, task_id, f"Max retries exceeded: {reason}")

    def _claim_and_spawn_batches(
        self,
        batches: list[list[Task]],
        alive_count: int,
        assigned_task_ids: set[str],
        done_ids: set[str],
        result: TickResult,
    ) -> None:
        """Claim tasks and spawn agents for each ready batch.

        Iterates over role-grouped batches, enforces capacity/overlap/backoff
        guards, claims tasks on the server, spawns an agent, and records metrics.
        Batches that fail to spawn are tracked for backoff and eventually failed.

        Args:
            batches: Role-grouped task batches from group_by_role.
            alive_count: Current number of alive agents (used to enforce max_agents cap).
            assigned_task_ids: Task IDs already owned by active agents (mutated in-place).
            done_ids: IDs of already-completed tasks (reserved for future guard use).
            result: TickResult accumulator for spawned/error lists.
        """
        base = self._config.server_url
        for batch in batches:
            if alive_count >= self._config.max_agents:
                break

            # Skip batches where any task is already assigned to an active agent
            if any(t.id in assigned_task_ids for t in batch):
                continue

            # Skip if any owned files overlap with active agents
            if self._check_file_overlap(batch):
                continue

            # Check spawn backoff: skip batches that recently failed
            batch_key = frozenset(t.id for t in batch)
            fail_count, last_fail_ts = self._spawn_failures.get(batch_key, (0, 0.0))
            if fail_count > 0 and (time.time() - last_fail_ts) < self._SPAWN_BACKOFF_S:
                logger.warning(
                    "Skipping batch %s: in backoff after %d consecutive spawn failure(s)",
                    [t.id for t in batch],
                    fail_count,
                )
                continue

            # Claim tasks BEFORE spawning to prevent duplicate agents.
            # Abort on server errors (5xx) or transport failures (unreachable).
            # 4xx responses are treated as best-effort (claim may not be implemented).
            claim_failed = False
            for task in batch:
                try:
                    resp = self._client.post(f"{base}/tasks/{task.id}/claim")
                    if resp.status_code >= 500:
                        logger.error(
                            "Server error %d claiming task %s — aborting spawn",
                            resp.status_code,
                            task.id,
                        )
                        result.errors.append(
                            f"claim:{task.id}: server error {resp.status_code}"
                        )
                        claim_failed = True
                        break
                except httpx.TransportError as exc:
                    logger.error(
                        "Server unreachable claiming task %s: %s — aborting spawn",
                        task.id,
                        exc,
                    )
                    result.errors.append(f"claim:{task.id}: {exc}")
                    claim_failed = True
                    break
            if claim_failed:
                continue

            # Per-batch timeout: max(estimated_minutes) * 60 * 1.5, clamped [120s, max_agent_runtime_s]
            max_estimated_s = max((t.estimated_minutes for t in batch), default=30) * 60
            batch_timeout_s = int(
                max(120, min(int(max_estimated_s * 1.5), self._config.max_agent_runtime_s))
            )

            try:
                session = self._spawner.spawn_for_tasks(batch)
                session.timeout_s = batch_timeout_s
                self._agents[session.id] = session
                self._claim_file_ownership(session.id, batch)
                alive_count += 1
                result.spawned.append(session.id)
                assigned_task_ids.update(t.id for t in batch)
                session.heartbeat_ts = time.time()
                self._spawn_failures.pop(batch_key, None)

                logger.info(
                    "Spawned %s for %d tasks: %s",
                    session.id,
                    len(batch),
                    [t.id for t in batch],
                )
                collector = get_collector()
                collector.start_agent(
                    agent_id=session.id,
                    role=session.role,
                    model=session.model_config.model,
                    provider=session.provider or "default",
                    agent_source=session.agent_source,
                )
                logger.info(
                    "Agent '%s' using prompt source: %s",
                    session.id,
                    session.agent_source,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("Spawn failed for batch %s: %s", [t.id for t in batch], exc)
                result.errors.append(f"spawn: {exc}")
                collector = get_collector()
                collector.record_error(
                    "agent_spawn_failed", "default", role=batch[0].role if batch else None
                )
                new_count = fail_count + 1
                self._spawn_failures[batch_key] = (new_count, time.time())
                if new_count >= self._MAX_SPAWN_FAILURES:
                    for task in batch:
                        try:
                            _fail_task(
                                self._client,
                                base,
                                task.id,
                                f"Spawn failed {new_count} consecutive times: {exc}",
                            )
                        except Exception as fail_exc:
                            logger.warning(
                                "Could not mark task %s as failed: %s", task.id, fail_exc
                            )
                    self._spawn_failures.pop(batch_key, None)

    def _process_completed_tasks(self, done_tasks: list[Task], result: TickResult) -> None:
        """Run janitor verification and record evolution metrics for done tasks.

        Skips tasks already processed in a prior tick. For each new done task,
        verifies completion signals, syncs the backlog file, appends the result
        to the knowledge base, and feeds the outcome to the evolution coordinator.

        Args:
            done_tasks: Tasks with status "done" fetched from the server.
            result: TickResult accumulator for verified/verification_failures lists.
        """
        for task in done_tasks:
            if task.id in self._processed_done_tasks:
                continue
            self._processed_done_tasks.add(task.id)

            janitor_passed = True
            if task.completion_signals:
                passed, failed_signals = verify_task(task, self._workdir)
                janitor_passed = passed
                if passed:
                    result.verified.append(task.id)
                else:
                    result.verification_failures.append((task.id, failed_signals))

            session = self._find_session_for_task(task.id)
            if session is not None:
                self._record_provider_health(session, success=janitor_passed)
                self._spawner.reap_completed_agent(session)
                session.status = "dead"
                logger.info(
                    "Agent %s finished task %s, process reaped", session.id, task.id
                )

            self._sync_backlog_file(task)

            if task.result_summary:
                try:
                    append_decision(
                        self._workdir, task.id, task.title, task.result_summary,
                    )
                except Exception as exc:
                    logger.warning("append_decision failed for task %s: %s", task.id, exc)

            if self._evolution is not None:
                model = session.model_config.model if session else None
                provider = session.provider if session else None
                duration = time.time() - session.spawn_ts if session and session.spawn_ts > 0 else 0.0
                try:
                    self._evolution.record_task_completion(
                        task=task,
                        duration_seconds=round(duration, 2),
                        cost_usd=0.0,
                        janitor_passed=janitor_passed,
                        model=model,
                        provider=provider,
                    )
                except Exception as exc:
                    logger.warning("Evolution record_task_completion failed: %s", exc)

    def _reap_dead_agents(self, result: TickResult, tasks_snapshot: dict[str, list[Task]]) -> None:
        """Kill agents that exceeded heartbeat or wall-clock timeout.

        Also fails any tasks owned by reaped agents.

        Args:
            result: TickResult to record reaped agent IDs into.
            tasks_snapshot: Pre-fetched tasks bucketed by status from this tick.
        """
        now = time.time()
        collector = get_collector()
        for session in list(self._agents.values()):
            if session.status == "dead":
                continue

            # Wall-clock timeout: use per-session timeout if set, else global config
            timeout_s = session.timeout_s if session.timeout_s is not None else self._config.max_agent_runtime_s
            runtime = now - session.spawn_ts
            if runtime > timeout_s:
                logger.warning(
                    "Reaping agent %s (exceeded timeout %.0fs, runtime %.0fs)",
                    session.id, timeout_s, runtime,
                )
                self._spawner.kill(session)
                result.reaped.append(session.id)
                self._release_file_ownership(session.id)
                for task_id in session.task_ids:
                    self._handle_orphaned_task(task_id, session, tasks_snapshot)
                continue

            # Heartbeat timeout
            age = now - session.heartbeat_ts
            if session.heartbeat_ts > 0 and age > self._config.heartbeat_timeout_s:
                logger.warning(
                    "Reaping stale agent %s (last heartbeat %.0fs ago)",
                    session.id,
                    age,
                )
                self._spawner.kill(session)
                result.reaped.append(session.id)
                # Release file ownership
                self._release_file_ownership(session.id)
                # Record agent end metrics
                collector.end_agent(session.id)
                # Record provider health failure for reaped agent
                self._record_provider_health(session, success=False)
                # Retry or fail their tasks
                for task_id in session.task_ids:
                    try:
                        self._retry_or_fail_task(
                            task_id,
                            f"Agent {session.id} reaped (heartbeat timeout)",
                            tasks_snapshot,
                        )
                    except httpx.HTTPError as exc:
                        logger.error("Failed to retry/fail task %s: %s", task_id, exc)

    def _sync_backlog_file(self, task: Task) -> None:
        """Move the matching .md file from backlog/open/ to backlog/closed/.

        Looks for a .md file in ``.sdd/backlog/open/`` whose filename slug
        shares significant keywords with the task title. If found, moves it to
        ``backlog/closed/`` and appends completion metadata.

        Args:
            task: The completed task to sync.
        """
        open_dir = self._workdir / ".sdd" / "backlog" / "open"
        if not open_dir.exists():
            return

        closed_dir = self._workdir / ".sdd" / "backlog" / "closed"
        closed_dir.mkdir(parents=True, exist_ok=True)

        title_words = self._backlog_words_from_title(task.title)

        best_match: str | None = None
        best_score = 0
        for md_file in open_dir.glob("*.md"):
            # Strip leading number prefix and extension to get slug words
            slug = re.sub(r"^\d+-", "", md_file.name[:-3])
            file_words = set(slug.split("-"))
            significant_file_words = {w for w in file_words if len(w) >= 4}
            overlap = title_words & significant_file_words
            if overlap and len(overlap) > best_score:
                best_score = len(overlap)
                best_match = md_file.name

        if best_match is None:
            return

        src = open_dir / best_match
        dst = closed_dir / best_match
        if not src.exists():
            return

        content = src.read_text(encoding="utf-8")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        summary = task.result_summary or ""
        content += f"\n\n---\n**completed**: {ts}\n**task_id**: {task.id}\n**result**: {summary}\n"
        dst.write_text(content, encoding="utf-8")
        src.unlink()
        logger.info("Synced backlog: %s → closed/", best_match)

    def ingest_backlog(self) -> int:
        """Scan .sdd/backlog/open/ and POST any new files to the task server.

        Each .md file in backlog/open/ that has not already been claimed is
        parsed and submitted to POST /tasks, then moved to backlog/claimed/ to
        prevent re-ingestion on subsequent ticks.

        Returns:
            Number of files ingested this call.
        """
        open_dir = self._workdir / ".sdd" / "backlog" / "open"
        if not open_dir.exists():
            return 0

        claimed_dir = self._workdir / ".sdd" / "backlog" / "claimed"

        count = 0
        for md_file in sorted(open_dir.glob("*.md")):
            # Skip if already present in claimed/ (e.g. from a prior run)
            if (claimed_dir / md_file.name).exists():
                continue

            content = md_file.read_text(encoding="utf-8")
            payload = _parse_backlog_file(md_file.name, content)

            try:
                resp = self._client.post(
                    f"{self._config.server_url}/tasks", json=payload
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("ingest_backlog: POST /tasks failed for %s: %s", md_file.name, exc)
                continue

            claimed_dir.mkdir(parents=True, exist_ok=True)
            md_file.rename(claimed_dir / md_file.name)
            count += 1
            logger.info("Ingested backlog file: %s", md_file.name)

        return count

    @staticmethod
    def _backlog_words_from_title(title: str) -> set[str]:
        """Extract significant lowercase words (≥4 chars) from a task title.

        Handles camelCase splitting so e.g. "ApprovalGate" yields
        {"approval", "gate"}.

        Args:
            title: Task title string.

        Returns:
            Set of significant word strings.
        """
        # Split camelCase
        expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", title)
        # Split on non-alphanumeric
        tokens = re.split(r"[^a-zA-Z0-9]+", expanded.lower())
        return {w for w in tokens if len(w) >= 4}

    def _generate_run_summary(
        self,
        done_tasks: list[Task],
        failed_tasks: list[Task],
    ) -> None:
        """Write a run completion summary to .sdd/runtime/summary.md.

        Called once when open_tasks == 0, active_agents == 0, and evolve_mode
        is False. Idempotent: sets _summary_written to prevent duplicate writes.

        Args:
            done_tasks: Tasks with status 'done'.
            failed_tasks: Tasks with status 'failed'.
        """
        runtime_dir = self._workdir / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        summary_path = runtime_dir / "summary.md"

        total_completed = len(done_tasks)
        total_failed = len(failed_tasks)
        wall_clock_s = time.time() - self._run_start_ts

        # Collect files-modified count and cost from metrics
        collector = get_collector(self._workdir / ".sdd" / "metrics")
        total_cost = collector.get_total_cost()
        files_modified: int = sum(
            getattr(m, "files_modified", 0)
            for m in collector._task_metrics.values()
        )

        # Build task list section
        task_lines: list[str] = []
        for task in sorted(done_tasks, key=lambda t: t.title):
            task_lines.append(f"- [x] {task.title}")
        for task in sorted(failed_tasks, key=lambda t: t.title):
            task_lines.append(f"- [ ] {task.title} *(failed)*")

        hours, rem = divmod(int(wall_clock_s), 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            duration_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes:
            duration_str = f"{minutes}m {seconds}s"
        else:
            duration_str = f"{seconds}s"

        lines = [
            "# Run Summary",
            "",
            f"**Total completed:** {total_completed}",
            f"**Total failed:** {total_failed}",
            f"**Files modified:** {files_modified}",
            f"**Estimated cost:** ${total_cost:.4f}",
            f"**Wall-clock duration:** {duration_str}",
            "",
            "## Tasks",
            "",
        ]
        lines.extend(task_lines)
        lines.append("")

        summary_path.write_text("\n".join(lines))
        self._summary_written = True
        logger.info("Run complete. Summary at .sdd/runtime/summary.md")

    def _log_summary(self, result: TickResult) -> None:
        """Write a one-line summary and agent state snapshot each tick.

        Args:
            result: TickResult from the current tick.
        """
        log_dir = self._workdir / ".sdd" / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "orchestrator.log"

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        alive = sum(1 for a in self._agents.values() if a.status != "dead")
        line = (
            f"[{ts}] open={result.open_tasks} agents={alive} "
            f"spawned={len(result.spawned)} reaped={len(result.reaped)} "
            f"verified={len(result.verified)} errors={len(result.errors)}\n"
        )
        with log_path.open("a") as f:
            f.write(line)

        # Dump agent state for the live dashboard
        agents_snapshot = [
            {
                "id": s.id,
                "role": s.role,
                "status": s.status,
                "model": s.model_config.model if s.model_config else None,
                "task_ids": s.task_ids,
                "pid": s.pid,
                "spawn_ts": s.spawn_ts,
                "runtime_s": round(time.time() - s.spawn_ts) if s.spawn_ts > 0 else 0,
            }
            for s in self._agents.values()
        ]
        state_path = log_dir / "agents.json"
        try:
            with state_path.open("w") as f:
                json.dump({"ts": time.time(), "agents": agents_snapshot}, f)
        except OSError:
            pass


class TickResult:
    """Summary of one orchestrator tick.

    Pure data container -- no logic, no side effects.
    """

    def __init__(self) -> None:
        self.open_tasks: int = 0
        self.active_agents: int = 0
        self.spawned: list[str] = []
        self.reaped: list[str] = []
        self.verified: list[str] = []
        self.verification_failures: list[tuple[str, list[str]]] = []
        self.retried: list[str] = []
        self.errors: list[str] = []
        # Populated when dry_run=True: (role, title, model, effort) tuples
        self.dry_run_planned: list[tuple[str, str, str | None, str | None]] = []

if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    from bernstein.adapters.registry import get_adapter
    from bernstein.core.seed import SeedConfig, parse_seed
    from bernstein.core.spawner import AgentSpawner

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8052)
    parser.add_argument("--adapter", type=str, default="claude")
    args = parser.parse_args()

    workdir = Path.cwd()

    # Configure logging so errors are visible in spawner.log (stdout/stderr)
    log_dir = workdir / ".sdd" / "runtime"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_dir / "orchestrator-debug.log"),
        ],
    )

    try:
        # Try to load adapter from seed if available
        adapter_name = args.adapter
        seed_path = workdir / "bernstein.yaml"
        seed: SeedConfig | None = None
        if seed_path.exists():
            try:
                seed = parse_seed(seed_path)
                adapter_name = getattr(seed, "cli", adapter_name)
            except Exception as exc:
                logger.warning("Failed to parse seed for adapter config: %s", exc)

        adapter_inst = get_adapter(adapter_name)
        if not adapter_inst:
            logger.error(f"Adapter {adapter_name} not found.")
            sys.exit(1)

        # Create TierAwareRouter from providers.yaml if available
        router: TierAwareRouter | None = None
        providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
        if providers_yaml.exists():
            router = TierAwareRouter()
            load_providers_from_yaml(providers_yaml, router)
            logger.info("Loaded TierAwareRouter from %s", providers_yaml)

        # Load MCP config from user global + project seed
        mcp_config = None
        if adapter_name == "claude":
            from bernstein.adapters.claude import load_mcp_config
            project_mcp = None
            if seed_path.exists():
                try:
                    seed_cfg = parse_seed(seed_path)
                    project_mcp = seed_cfg.mcp_servers
                except Exception as exc:
                    logger.warning("Failed to parse seed for MCP config: %s", exc)
            mcp_config = load_mcp_config(project_servers=project_mcp)
            if mcp_config:
                logger.info("Loaded MCP config with %d server(s)", len(mcp_config.get("mcpServers", {})))

        # Load agency catalog from seed config
        from bernstein.core.agency_loader import load_agency_catalog

        agency_catalog = None
        if seed and seed.agent_catalog:
            catalog_path = Path(seed.agent_catalog)
            if not catalog_path.is_absolute():
                catalog_path = workdir / catalog_path
            agency_catalog = load_agency_catalog(catalog_path)
            if agency_catalog:
                logger.info("Loaded %d agency agents from %s", len(agency_catalog), catalog_path)

        spawner = AgentSpawner(
            adapter=adapter_inst,
            templates_dir=workdir / "templates",
            workdir=workdir,
            router=router,
            mcp_config=mcp_config,
            agency_catalog=agency_catalog,
            catalog=seed.catalogs if seed else None,
        )
        budget_usd = 0.0
        dry_run = False
        run_config_path = workdir / ".sdd" / "runtime" / "run_config.json"
        if run_config_path.exists():
            try:
                run_cfg = json.loads(run_config_path.read_text())
                budget_usd = float(run_cfg.get("budget_usd", 0.0))
                dry_run = bool(run_cfg.get("dry_run", False))
            except (json.JSONDecodeError, ValueError):
                pass

        config = OrchestratorConfig(
            server_url=f"http://127.0.0.1:{args.port}",
            max_agents=6,
            budget_usd=budget_usd,
            dry_run=dry_run,
        )

        orchestrator = Orchestrator(config=config, spawner=spawner, workdir=workdir, router=router)
        orchestrator.run()
    except Exception:
        logger.exception("Orchestrator crashed")
        sys.exit(1)
