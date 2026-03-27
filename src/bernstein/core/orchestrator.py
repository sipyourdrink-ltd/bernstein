"""Orchestrator loop: watch tasks, spawn agents, verify completion, repeat.

The orchestrator is DETERMINISTIC CODE, not an LLM. It matches tasks to agents
via the spawner and verifies completion via the janitor. See ADR-001.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

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
    )


def _fetch_tasks(client: httpx.Client, base_url: str, status: str) -> list[Task]:
    """GET /tasks?status=<status> and parse into Task objects.

    Args:
        client: httpx client.
        base_url: Server base URL.
        status: Task status filter (e.g. "open", "done").

    Returns:
        List of tasks matching the status filter.
    """
    resp = client.get(f"{base_url}/tasks", params={"status": status})
    resp.raise_for_status()
    return [_task_from_dict(t) for t in resp.json()]


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
        self._client = client or httpx.Client(timeout=10.0)
        self._agents: dict[str, AgentSession] = {}
        self._file_ownership: dict[str, str] = {}  # filepath → agent_id
        self._processed_done_tasks: set[str] = set()  # avoid re-processing done tasks
        self._running = False
        self._tick_count = 0

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
            1. Fetch open tasks from server.
            2. Group into role-based batches.
            3. Spawn agents if capacity allows.
            4. Check done tasks and run janitor.
            5. Reap dead/stale agents and fail their tasks.

        Returns:
            Summary of what happened this tick.
        """
        result = TickResult()
        self._tick_count += 1
        base = self._config.server_url

        # 1. Fetch open tasks
        try:
            open_tasks = _fetch_tasks(self._client, base, "open")
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch open tasks: %s", exc)
            result.errors.append(f"fetch_open: {exc}")
            return result

        result.open_tasks = len(open_tasks)

        # 2. Group into batches
        batches = group_by_role(open_tasks, self._config.max_tasks_per_agent)

        # 3. Count alive agents, spawn if capacity
        self._refresh_agent_states()
        alive_count = sum(1 for a in self._agents.values() if a.status != "dead")
        result.active_agents = alive_count

        # Track which task IDs are already assigned to active agents
        assigned_task_ids = set()
        for agent in self._agents.values():
            if agent.status != "dead":
                assigned_task_ids.update(agent.task_ids)

        for batch in batches:
            if alive_count >= self._config.max_agents:
                break

            # Skip batches where any task is already assigned to an active agent
            if any(t.id in assigned_task_ids for t in batch):
                continue

            # Feature 2: skip if any owned files overlap with active agents
            if self._check_file_overlap(batch):
                continue

            try:
                session = self._spawner.spawn_for_tasks(batch)
                self._agents[session.id] = session
                # Claim file ownership for the spawned agent
                self._claim_file_ownership(session.id, batch)
                alive_count += 1
                result.spawned.append(session.id)
                assigned_task_ids.update(t.id for t in batch)

                # Claim specific tasks on the server so they're not re-spawned
                for task in batch:
                    try:
                        self._client.post(f"{base}/tasks/{task.id}/claim")
                    except httpx.HTTPError:
                        pass  # Best effort

                # Set heartbeat_ts so stale reaper can timeout hung agents
                session.heartbeat_ts = time.time()

                logger.info(
                    "Spawned %s for %d tasks: %s",
                    session.id,
                    len(batch),
                    [t.id for t in batch],
                )
                # Record agent spawn metrics
                collector = get_collector()
                collector.start_agent(
                    agent_id=session.id,
                    role=session.role,
                    model=session.model_config.model,
                    provider=session.provider or "default",
                )
            except Exception as exc:
                logger.error("Spawn failed for batch %s: %s", [t.id for t in batch], exc)
                result.errors.append(f"spawn: {exc}")
                # Record spawn failure
                collector = get_collector()
                collector.record_error("agent_spawn_failed", "default", role=batch[0].role if batch else None)

        # 4. Check done tasks, run janitor, record evolution metrics
        try:
            done_tasks = _fetch_tasks(self._client, base, "done")
            for task in done_tasks:
                # Skip tasks we already processed
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
                        result.verification_failures.append(
                            (task.id, failed_signals)
                        )

                # Record provider health feedback for done tasks
                session = self._find_session_for_task(task.id)
                if session is not None:
                    self._record_provider_health(session, success=janitor_passed)

                # Sync backlog: move matching .md file from open/ to closed/
                self._sync_backlog_file(task)

                # Record task completion in evolution coordinator with real data
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
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch done tasks: %s", exc)
            result.errors.append(f"fetch_done: {exc}")

        # 5. Reap stale agents
        self._reap_stale(result)

        # 6. Run evolution analysis cycle every N ticks
        if self._evolution is not None and self._tick_count % self._config.evolution_tick_interval == 0:
            self._run_evolution_cycle(result)

        # 7. Log summary
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
            time.sleep(self._config.poll_interval_s)
        logger.info("Orchestrator stopped")

    def stop(self) -> None:
        """Signal the run loop to exit after the current tick."""
        self._running = False

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

    def _refresh_agent_states(self) -> None:
        """Update alive/dead status for all tracked agents.

        When an agent process dies, handles orphaned tasks via the agent
        completion protocol: checks task status on the server, runs janitor
        verification if completion signals exist, and completes or fails
        accordingly. Also releases file ownership and emits metrics.
        """
        base = self._config.server_url
        for session in list(self._agents.values()):
            if session.status == "dead":
                continue
            if not self._spawner.check_alive(session):
                session.status = "dead"
                # Release file ownership for this agent
                self._release_file_ownership(session.id)
                # Handle orphaned tasks
                for task_id in session.task_ids:
                    self._handle_orphaned_task(task_id, session)

    def _handle_orphaned_task(self, task_id: str, session: AgentSession) -> None:
        """Handle a task left behind by a dead agent process.

        Checks task status on the server, runs janitor verification if
        the task has completion signals, and marks it complete or failed.
        Emits a MetricsRecord afterward.

        Args:
            task_id: ID of the orphaned task.
            session: The dead agent's session.
        """
        base = self._config.server_url
        start_ts = session.heartbeat_ts if session.heartbeat_ts > 0 else time.time()
        success = False
        error_type: str | None = None

        try:
            resp = self._client.get(f"{base}/tasks/{task_id}")
            resp.raise_for_status()
            task_data = resp.json()
            task = _task_from_dict(task_data)
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
                    _fail_task(
                        self._client, base, task_id,
                        f"Agent {session.id} died; janitor failed: {failed_signals}",
                    )
                    logger.info(
                        "Orphaned task %s failed (janitor failed: %s) after agent %s died",
                        task_id, failed_signals, session.id,
                    )
                except httpx.HTTPError as exc:
                    logger.error("Failed to fail orphaned task %s: %s", task_id, exc)
                error_type = "janitor_failed"
        else:
            # No completion signals -- fail the task
            try:
                _fail_task(
                    self._client, base, task_id,
                    f"Agent {session.id} died; no completion signals to verify",
                )
                logger.info(
                    "Orphaned task %s failed (no signals) after agent %s died",
                    task_id, session.id,
                )
            except httpx.HTTPError as exc:
                logger.error("Failed to fail orphaned task %s: %s", task_id, exc)
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
            timestamp=datetime.now(timezone.utc).isoformat(),
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
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

    def _reap_stale(self, result: TickResult) -> None:
        """Kill agents that exceeded heartbeat or wall-clock timeout.

        Also fails any tasks owned by reaped agents.

        Args:
            result: TickResult to record reaped agent IDs into.
        """
        now = time.time()
        collector = get_collector()
        for session in list(self._agents.values()):
            if session.status == "dead":
                continue

            # Wall-clock timeout: kill agents running longer than max_agent_runtime_s
            runtime = now - session.spawn_ts
            if runtime > self._config.max_agent_runtime_s:
                logger.warning(
                    "Reaping agent %s (exceeded max runtime %.0fs)",
                    session.id, runtime,
                )
                self._spawner.kill(session)
                result.reaped.append(session.id)
                self._release_file_ownership(session.id)
                for task_id in session.task_ids:
                    self._handle_orphaned_task(task_id, session)
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
                # Fail their tasks
                for task_id in session.task_ids:
                    try:
                        _fail_task(
                            self._client,
                            self._config.server_url,
                            task_id,
                            f"Agent {session.id} reaped (heartbeat timeout)",
                        )
                    except httpx.HTTPError as exc:
                        logger.error("Failed to fail task %s: %s", task_id, exc)

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
        self.errors: list[str] = []

if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    from bernstein.adapters.registry import get_adapter
    from bernstein.core.seed import parse_seed
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
        if seed_path.exists():
            try:
                seed = parse_seed(seed_path)
                adapter_name = getattr(seed, "cli", adapter_name)
            except Exception:
                pass

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

        spawner = AgentSpawner(
            adapter=adapter_inst,
            templates_dir=workdir / "templates",
            workdir=workdir,
            router=router,
        )
        config = OrchestratorConfig(server_url=f"http://127.0.0.1:{args.port}", max_agents=6)

        orchestrator = Orchestrator(config=config, spawner=spawner, workdir=workdir, router=router)
        orchestrator.run()
    except Exception:
        logger.exception("Orchestrator crashed")
        sys.exit(1)
