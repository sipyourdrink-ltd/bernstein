"""Orchestrator tick loop: the core per-cycle control flow.

Extracted from orchestrator.py as part of ORCH-009 decomposition.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.agent_lifecycle import (
    check_loops_and_deadlocks,
    check_stale_agents,
    check_stalled_tasks,
    reap_dead_agents,
    recycle_idle_agents,
    refresh_agent_states,
)
from bernstein.core.context import refresh_knowledge_base
from bernstein.core.dep_validator import DependencyValidator
from bernstein.core.dependency_scan import (
    DependencyScanStatus,
    DependencyVulnerabilityFinding,
)
from bernstein.core.graph import TaskGraph
from bernstein.core.metrics import get_collector
from bernstein.core.runtime_state import (
    rotate_log_file,
)
from bernstein.core.signals import read_unresolved_pivots
from bernstein.core.slo import apply_error_budget_adjustments
from bernstein.core.task_grouping import compact_small_tasks
from bernstein.core.task_lifecycle import (
    auto_decompose_task,
    claim_and_spawn_batches,
    maybe_retry_task,
    prepare_speculative_warm_pool,
    process_completed_tasks,
    should_auto_decompose,
)
from bernstein.core.tick_pipeline import (
    fail_task,
    fetch_all_tasks,
    group_by_role,
)
from bernstein.core.token_monitor import check_token_growth
from bernstein.core.watchdog import collect_watchdog_findings

if TYPE_CHECKING:
    from bernstein.core.models import (
        AgentSession,
        Task,
    )

logger = logging.getLogger(__name__)


def tick(orch: Any) -> Any:
    """Execute one orchestrator cycle.

    Args:
        orch: The orchestrator instance.

    Returns:
        A TickResult summarizing the tick.
    """
    from bernstein.core.telemetry import start_span

    tick_start = time.monotonic()
    with start_span("orchestrator.tick", attributes={"tick": orch._tick_count + 1}):
        result = _tick_internal(orch)
    tick_duration = time.monotonic() - tick_start
    if tick_duration > 30.0:
        logger.warning("Tick took %.1fs (threshold 30s)", tick_duration)
    return result


def _tick_internal(orch: Any) -> Any:
    """Actual tick implementation (previously tick()).

    Args:
        orch: The orchestrator instance.

    Returns:
        A TickResult summarizing the tick.
    """
    from bernstein.core.orchestrator import TickResult

    result = TickResult()
    orch._tick_count += 1
    base = orch._config.server_url
    _tick_http_reads = 0  # counts GET requests this tick (should stay at 1)

    # Phase scheduling: fast ops every tick, normal every 6, slow every 30.
    # This prevents heavy operations (SLO, evolution, watchdog) from
    # blocking the fast control loop (spawn, reap, heartbeat).
    _run_normal = orch._tick_count % 6 == 0
    _run_slow = orch._tick_count % 30 == 0
    logger.debug(
        "tick #%d phases: fast%s%s",
        orch._tick_count,
        "+normal" if _run_normal else "",
        "+slow" if _run_slow else "",
    )

    # Record tick start for deterministic replay
    orch._recorder.record("tick_start", tick=orch._tick_count)
    if orch._quota_poller is not None:
        orch._quota_poller.maybe_poll()

    # WAL: record tick boundary for crash recovery and audit trail
    try:
        orch._wal_writer.write_entry(
            decision_type="tick_start",
            inputs={"tick": orch._tick_count},
            output={},
            actor="orchestrator",
        )
    except OSError:
        logger.debug("WAL write failed for tick_start %d", orch._tick_count)

    # 0-pre. Proactive server health check (every normal tick).
    # Detects server crashes early so the watchdog can restart it
    # before we waste time attempting task fetches / spawns.
    if _run_normal and not _check_server_health(orch):
        result.errors.append("server_health_check_failed")
        return result

    # 0. Ingest any new backlog files before fetching tasks.
    #    Rate-limited to 10 files/tick with title dedup to prevent
    #    server overload and duplicate task creation.
    #    Gated behind _run_normal — no need to scan 300 files every tick.
    if _run_normal:
        try:
            from bernstein.core.roadmap_runtime import emit_roadmap_wave

            emitted = emit_roadmap_wave(orch._workdir)
            if emitted:
                logger.info("Emitted %d roadmap ticket(s) into backlog/open", len(emitted))
        except (OSError, ValueError) as exc:
            logger.warning("roadmap wave emission failed: %s", exc)

        try:
            orch.ingest_backlog()
        except (OSError, ValueError) as exc:
            logger.warning("ingest_backlog failed: %s", exc)

        if orch._running:
            _run_scheduled_dependency_scan(orch)

    # 1. Fetch all tasks in a single bulk request, bucketed client-side.
    try:
        tasks_by_status = fetch_all_tasks(orch._client, base)
        _tick_http_reads += 1  # single GET /tasks (no status filter)
        orch._consecutive_server_failures = 0  # Reset on success
    except httpx.HTTPError as exc:
        orch._consecutive_server_failures = getattr(orch, "_consecutive_server_failures", 0) + 1
        if orch._consecutive_server_failures >= 12:  # 12 ticks x ~30s = ~6 min
            logger.critical(
                "Server unreachable for %d consecutive ticks — orchestrator stopping to prevent waste",
                orch._consecutive_server_failures,
            )
            orch._running = False
        elif orch._consecutive_server_failures >= 3:
            logger.warning(
                "Server unreachable for %d ticks (%s). Supervisor should restart it.",
                orch._consecutive_server_failures,
                exc,
            )
        else:
            logger.error("Failed to fetch tasks: %s", exc)
        result.errors.append(f"fetch_all: {exc}")
        # Even when the server is unreachable, refresh agent states and
        # reap zombies so dead processes don't accumulate across ticks.
        refresh_agent_states(orch, {})
        reap_dead_agents(orch, result, {})
        return result

    logger.debug(
        "tick #%d: %d HTTP read(s) this tick (open=%d claimed=%d done=%d failed=%d)",
        orch._tick_count,
        _tick_http_reads,
        len(tasks_by_status.get("open", [])),
        len(tasks_by_status.get("claimed", [])),
        len(tasks_by_status.get("done", [])),
        len(tasks_by_status.get("failed", [])),
    )

    # The server returns tasks matching the requested status; apply the
    # dependency filter here for "open" tasks.
    done_tasks = tasks_by_status["done"]
    done_ids = {t.id for t in done_tasks}
    now = time.time()
    open_tasks = [
        t
        for t in tasks_by_status["open"]
        if all(dep in done_ids for dep in t.depends_on)
        # Skip tasks with future created_at (retry backoff)
        and t.created_at <= now
    ]
    result.open_tasks = len(open_tasks)

    # 1b. Hold back tasks blocked by unresolved high-severity pivots
    ready_tasks = open_tasks
    try:
        unresolved = read_unresolved_pivots(orch._workdir)
        if unresolved:
            blocked_ids: set[str] = set()
            for pivot in unresolved:
                blocked_ids.update(pivot.affected_tickets)
            if blocked_ids:
                before = len(ready_tasks)
                ready_tasks = [t for t in ready_tasks if t.id not in blocked_ids]
                held = before - len(ready_tasks)
                if held:
                    logger.warning(
                        "Holding %d task(s) pending VP pivot review: %s",
                        held,
                        blocked_ids,
                    )
    except OSError as exc:
        logger.warning("Failed to read pivot signals: %s", exc)

    # 1b-i. Check task deadlines — warn or fail running tasks past deadline
    try:
        _check_task_deadlines(
            orch,
            tasks_by_status.get("claimed", []) + tasks_by_status.get("in_progress", []),
        )
    except Exception as exc:
        logger.warning("Deadline check failed: %s", exc)

    # 1b-i.5. Release claimed tasks stuck without a live agent (every normal tick)
    if _run_normal:
        try:
            _release_stale_claims(orch, tasks_by_status.get("claimed", []))
        except Exception as exc:
            logger.warning("Stale claim release failed: %s", exc)

    # 1b-ii. Governed workflow: filter tasks to current phase only
    if orch._workflow_executor is not None and not orch._workflow_executor.is_completed:
        before_wf = len(ready_tasks)
        ready_tasks = orch._workflow_executor.filter_tasks_for_current_phase(ready_tasks)
        held_wf = before_wf - len(ready_tasks)
        if held_wf:
            logger.info(
                "Workflow phase %r: holding %d task(s) outside current phase",
                orch._workflow_executor.current_phase_name,
                held_wf,
            )
        # Check for file-based approval grant
        _check_workflow_approval(orch)

    # 1c. Build task graph and compute optimal parallelism
    #     Graph analysis + dependency validation are expensive — gate behind
    #     _run_normal. The all_tasks list and task ID cache are always needed.
    all_tasks = [t for status_tasks in tasks_by_status.values() for t in status_tasks]
    orch._latest_tasks_by_id = {task.id: task for task in all_tasks}

    task_graph: TaskGraph | None = None
    if _run_normal:
        task_graph = TaskGraph(all_tasks)
        analysis = task_graph.analyse()
        dep_validator = DependencyValidator()
        dep_validation = dep_validator.validate(all_tasks)
        for cycle in dep_validation.cycles:
            logger.error("Dependency cycle detected: %s", " -> ".join(cycle))
        for task_id, dep_id, dep_status in dep_validation.stuck_deps:
            logger.warning(
                "Task %s depends on %s which is %s — task remains blocked",
                task_id,
                dep_id,
                dep_status,
            )
        for warning in dep_validation.warnings:
            logger.warning("Dependency validation: %s", warning)
        critical_path_ids = set(dep_validator.critical_path(all_tasks))
        # Cache for use in fast ticks
        orch._cached_critical_path_ids = critical_path_ids

        if analysis.parallel_width < orch._config.max_agents and analysis.parallel_width > 0:
            logger.debug(
                "Graph parallel width (%d) < max_agents (%d) -- dependency filter already limits concurrency",
                analysis.parallel_width,
                orch._config.max_agents,
            )

        if analysis.bottlenecks:
            logger.info(
                "Graph bottleneck(s): %s -- %d downstream tasks blocked",
                analysis.bottlenecks,
                sum(len(task_graph.dependents(b)) for b in analysis.bottlenecks),
            )

        # Persist graph snapshot for dashboard / debugging
        try:
            task_graph.save(orch._workdir / ".sdd" / "runtime")
        except OSError as exc:
            logger.debug("Failed to save task graph: %s", exc)
    else:
        # Fast tick: reuse cached critical path IDs from last normal tick
        critical_path_ids = getattr(orch, "_cached_critical_path_ids", set())

    # 3. Count alive agents, spawn if capacity (capped by graph parallel width)
    # 2b. Rate-limit recovery: restore providers whose throttle window expired.
    _recovered = orch._rate_limit_tracker.recover_expired_throttles(orch._router)
    if _recovered:
        logger.info("Rate-limit: recovered providers %s", _recovered)
    # Sync active-agent counts into the router for load-spreading scores.
    if orch._router is not None:
        orch._router.update_active_agent_counts(orch._rate_limit_tracker.get_all_active_counts())

    # 2c. Poll Provider Batch API
    if orch._batch_api is not None:
        orch._batch_api.poll(orch)

    # 2d. Detect loops and deadlocks
    check_loops_and_deadlocks(orch)

    # 2e. Recycle idle agents
    recycle_idle_agents(orch, tasks_by_status)

    # Sync failure timestamps to spawner for cooldown enforcement
    orch._spawner._agent_failure_timestamps = orch._agent_failure_timestamps

    refresh_agent_states(orch, tasks_by_status)
    alive_count = sum(1 for a in orch._agents.values() if a.status != "dead")
    result.active_agents = alive_count

    if task_graph is not None:
        prepare_speculative_warm_pool(orch, task_graph, all_tasks)

    # 3a. Build alive-per-role map for task distribution prioritization.
    # Starving roles (0 alive agents) get scheduled before well-served roles.
    _alive_per_role: dict[str, int] = {}
    for _agent in orch._agents.values():
        if _agent.status != "dead":
            _alive_per_role[_agent.role] = _alive_per_role.get(_agent.role, 0) + 1

    # 2. Group into batches with starving-role prioritization wired in
    budget_status = orch._cost_tracker.status()
    cost_estimates: dict[str, float] = {}
    if ready_tasks:
        from bernstein.core.cost_estimation import estimate_spawn_cost

        metrics_dir = orch._workdir / ".sdd" / "metrics"
        for task in ready_tasks:
            try:
                estimate = estimate_spawn_cost(task, metrics_dir=metrics_dir)
                cost_estimates[task.id] = estimate.estimated_cost_usd
            except Exception as exc:
                logger.debug("Cost estimate unavailable for task %s: %s", task.id, exc)
    priority_overrides = {task.id: max(1, task.priority - 1) for task in ready_tasks if task.id in critical_path_ids}
    # Build task creation timestamp map for fair scheduling
    task_created_at = {task.id: task.created_at for task in ready_tasks}
    batches = group_by_role(
        ready_tasks,
        orch._config.max_tasks_per_agent,
        alive_per_role=_alive_per_role,
        priority_overrides=priority_overrides,
        task_created_at=task_created_at,
        agent_affinity=orch._agent_affinity if orch._agent_affinity else None,
        cost_estimates=cost_estimates or None,
        budget_remaining_usd=budget_status.remaining_usd,
    )
    batches = compact_small_tasks(batches, orch._config.max_tasks_per_agent)

    # Track which task IDs are already assigned to active agents
    assigned_task_ids: set[str] = set()
    for agent in orch._agents.values():
        if agent.status != "dead":
            assigned_task_ids.update(agent.task_ids)

    # 3b. Adaptive parallelism: adjust effective max_agents based on
    # recent error rate and system CPU load.
    _orig_max_agents = orch._config.max_agents
    _effective_max = orch._adaptive_parallelism.effective_max_agents()
    orch._config.max_agents = _effective_max

    # Record parallelism_level metric for time-series dashboards
    from bernstein.core.metric_collector import MetricType

    _ap_status = orch._adaptive_parallelism.status()
    get_collector()._write_metric_point(
        MetricType.PARALLELISM_LEVEL,
        float(_effective_max),
        {
            "configured_max": str(_ap_status.configured_max),
            "error_rate": f"{_ap_status.error_rate:.3f}",
            "cpu_percent": f"{_ap_status.cpu_percent:.1f}",
            "reason": _ap_status.last_adjustment_reason,
        },
    )

    # 3c. Claim tasks and spawn agents for ready batches (skip if budget is exhausted)
    if orch._config.dry_run:
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
    elif orch._cost_tracker.budget_usd > 0 and orch._cost_tracker.status().should_stop:
        _bs = orch._cost_tracker.status()
        logger.warning(
            "Budget exhausted — $%.2f spent of $%.2f budget. "
            "Fix: increase budget with --budget N or wait for running tasks to complete",
            _bs.spent_usd,
            _bs.budget_usd,
        )
        orch._notify(
            "budget.exhausted",
            "Budget cap reached",
            f"Spending cap of ${_bs.budget_usd:.2f} reached. "
            f"${_bs.spent_usd:.2f} spent ({_bs.percentage_used * 100:.0f}%). "
            "Agent spawning paused.",
            budget_usd=round(_bs.budget_usd, 2),
            spent_usd=round(_bs.spent_usd, 4),
            percent_used=round(_bs.percentage_used * 100, 1),
        )
    else:
        claim_and_spawn_batches(orch, batches, alive_count, assigned_task_ids, done_ids, result)

    # Restore max_agents after adaptive-parallelism-adjusted spawning
    orch._config.max_agents = _orig_max_agents

    if orch._batch_api is not None:
        orch._batch_api.poll(orch)

    # 4. Check done tasks, run janitor, record evolution metrics
    process_completed_tasks(orch, done_tasks, result)

    # 4x. Periodic git hygiene
    # Gated behind _run_slow — git operations are IO-heavy.
    if _run_slow and len(done_tasks) > 0:
        try:
            from bernstein.core.git_hygiene import run_hygiene

            run_hygiene(orch._workdir)
        except Exception:
            pass

    # 4x-ii. Periodic worktree garbage collection
    # Gated behind _run_slow — worktree GC is IO-heavy.
    if _run_slow:
        try:
            active_ids = {s.id for s in orch._agents.values() if s.status != "dead"}
            cleaned = orch._spawner.prune_orphan_worktrees(active_ids)
            if cleaned:
                logger.info("Periodic worktree GC: cleaned %d orphan worktree(s)", cleaned)
        except Exception as exc:
            logger.debug("Periodic worktree GC failed: %s", exc)

    # 4a-wf. Governed workflow: try to advance phase after processing completions
    if orch._workflow_executor is not None and not orch._workflow_executor.is_completed:
        all_tasks = [t for status_tasks in tasks_by_status.values() for t in status_tasks]
        phase_event = orch._workflow_executor.try_advance(all_tasks)
        if phase_event is not None:
            orch._recorder.record(
                "workflow_phase_advanced",
                workflow_hash=phase_event.workflow_hash,
                from_phase=phase_event.from_phase,
                to_phase=phase_event.to_phase,
                reason=phase_event.reason,
                tasks_completed=list(phase_event.tasks_completed),
            )
            orch._post_bulletin(
                "status",
                f"Workflow phase: {phase_event.from_phase} -> {phase_event.to_phase}",
            )

    # 4b. Use cached failed tasks and maybe retry with escalation
    failed_tasks = tasks_by_status["failed"]
    for task in failed_tasks:
        if _maybe_retry_task(orch, task):
            result.retried.append(task.id)

    # 4b.5 Feed outcomes to adaptive parallelism controller
    for _task_id in result.verified:
        orch._adaptive_parallelism.record_outcome(success=True)
    for _ft in failed_tasks:
        if _ft.id not in orch._retried_task_ids:
            orch._adaptive_parallelism.record_outcome(success=False)

    # 4b.6 Track completions/failures for manager review trigger
    orch._completions_since_review += len(result.verified)
    orch._failures_since_review += len([t for t in failed_tasks if t.id not in orch._retried_task_ids])

    # Check for explicit review trigger (e.g. from `bernstein review` CLI)
    _review_flag = orch._workdir / ".sdd" / "runtime" / "review_requested"
    if _review_flag.exists():
        _review_flag.unlink(missing_ok=True)
        orch._completions_since_review = max(
            orch._completions_since_review,
            orch._MANAGER_REVIEW_COMPLETION_THRESHOLD,
        )

    # Run manager queue review when triggered (periodic correction pass)
    # Gated behind _run_slow — manager review involves an LLM call.
    if _run_slow and _should_trigger_manager_review(orch, orch._failures_since_review):
        _run_manager_queue_review(orch)

    # 4b.6 AgentOps: update SLOs, check error budget, detect incidents
    # Gated behind _run_slow — SLO/incident tracking is expensive and
    # doesn't need sub-minute granularity.
    if _run_slow:
        collector = get_collector()
        orch._slo_tracker.update_from_collector(collector)
        orch._slo_tracker.save(orch._workdir / ".sdd" / "metrics")

        # Apply error-budget-driven throttling adjustments
        adjusted_max, _ = apply_error_budget_adjustments(orch._config.max_agents, orch._slo_tracker)
        orch._adaptive_parallelism.set_slo_constraint(adjusted_max if adjusted_max != orch._config.max_agents else None)

        # Track consecutive failures for incident detection
        if result.verified:
            orch._consecutive_failures = 0
        if failed_tasks:
            orch._consecutive_failures += len([t for t in failed_tasks if t.id not in orch._retried_task_ids])

        # Check for incidents
        all_counted = orch._slo_tracker.error_budget.total_tasks
        failed_counted = orch._slo_tracker.error_budget.failed_tasks
        incident = orch._incident_manager.check_for_incidents(
            failed_task_count=failed_counted,
            total_task_count=all_counted,
            consecutive_failures=orch._consecutive_failures,
            error_budget_depleted=orch._slo_tracker.error_budget.is_depleted,
        )
        orch._incident_manager.save(orch._workdir / ".sdd" / "runtime")

        # Notify PagerDuty on SEV1/SEV2 incidents
        if incident is not None and incident.severity in ("sev1", "sev2"):
            orch._notify(
                "incident.critical",
                f"Incident [{incident.severity.value.upper()}]: {incident.title}",
                incident.description,
                incident_id=incident.id,
                severity=incident.severity.value,
                failed_tasks=str(failed_counted),
                total_tasks=str(all_counted),
                consecutive_failures=str(orch._consecutive_failures),
            )

    # 4c. Check heartbeat-based staleness; send WAKEUP/SHUTDOWN as needed
    check_stale_agents(orch)

    # 4d. Check progress-snapshot-based stalls; send WAKEUP/SHUTDOWN/kill
    check_stalled_tasks(orch)

    # 4d-ii. Token growth monitor: alert on quadratic growth, kill runaway agents
    check_token_growth(orch)

    # 4d-ii.5 Loop and deadlock detection: kill looping agents, break lock cycles
    check_loops_and_deadlocks(orch)

    # 4d-ii.6 Three-tier watchdog: mechanical checks -> AI triage -> human escalation
    # Gated behind _run_slow — watchdog sync is heavyweight.
    if _run_slow:
        orch._watchdog.sync(collect_watchdog_findings(orch))

    # 4d-iii. Cost anomaly detection: burn rate projection, stop on budget overrun
    # Gated behind _run_slow — anomaly detection doesn't need every-tick granularity.
    if _run_slow:
        for sig in orch._anomaly_detector.check_tick(list(orch._agents.values()), orch._cost_tracker):
            _handle_anomaly_signal(orch, sig)

    # 4d-iv. Real-time cost recording: update budget status from live tokens
    _record_live_costs(orch)

    # 4e. Recycle idle agents (task already resolved but process still alive,
    #     or no heartbeat for idle threshold). SHUTDOWN → 30s grace → SIGKILL.
    recycle_idle_agents(orch, tasks_by_status)

    # 5. Reap dead/stale agents and fail their tasks
    reap_dead_agents(orch, result, tasks_by_status)

    # 5b. Retry any pushes that failed in previous ticks (normal cadence)
    if _run_normal:
        try:
            retried = orch._spawner.retry_pending_pushes()
            if retried:
                logger.info("Retried %d pending push(es) successfully", retried)
        except Exception as exc:
            logger.warning("Pending push retry failed: %s", exc)

    # 6. Run evolution analysis cycle every N ticks
    # Gated behind _run_slow — evolution analysis is heavyweight.
    if _run_slow and orch._evolution is not None and orch._tick_count % orch._config.evolution_tick_interval == 0:
        from bernstein.core.orchestrator_evolve import run_evolution_cycle

        run_evolution_cycle(orch, result)

    # 6b. Refresh knowledge base every 5 evolution intervals
    # Gated behind _run_slow — knowledge base refresh is IO-heavy.
    if _run_slow and orch._tick_count % (orch._config.evolution_tick_interval * 5) == 0:
        try:
            refresh_knowledge_base(orch._workdir)
        except OSError as exc:
            logger.warning("Knowledge base refresh failed: %s", exc)

    # 7. Check evolve mode: if all tasks done and no agents alive, trigger new cycle
    from bernstein.core.orchestrator_evolve import check_evolve, replenish_backlog

    check_evolve(orch, result, tasks_by_status)

    # 8. Replenish backlog in evolve mode when tasks run out
    replenish_backlog(orch, result)

    # 8b. Generate run completion summary for non-evolve runs (reuse cached tasks)
    if (
        not orch._config.evolve_mode
        and result.open_tasks == 0
        and result.active_agents == 0
        and not orch._summary_written
    ):
        from bernstein.core.orchestrator_summary import generate_run_summary

        generate_run_summary(orch, tasks_by_status["done"], tasks_by_status["failed"])

    # 9. Log summary
    _log_summary(orch, result)

    # 11. Record replay events for deterministic replay
    _record_tick_events(orch, result, tasks_by_status)

    return result


def _check_task_deadlines(orch: Any, running_tasks: list[Task]) -> None:
    """Check deadlines on running tasks and escalate or notify.

    For tasks past their deadline with some time remaining (warning window),
    fire a ``task.deadline_warning``.  For tasks that are fully exceeded,
    fire ``task.deadline_exceeded``, append a meta message for the next agent,
    and fail the task so the retry logic kicks in with deadline-aware escalation.

    Args:
        orch: The orchestrator instance.
        running_tasks: Tasks currently in claimed or in_progress state.
    """
    now = time.time()
    warning_window = 300.0  # 5-minute warning

    for task in running_tasks:
        if task.deadline is None:
            continue

        elapsed = now - task.deadline

        # Fully exceeded: fail immediately with escalation
        if elapsed > 0:
            logger.warning(
                "Task %s ('%s') deadline exceeded (%.0fs overdue)",
                task.id,
                task.title,
                elapsed,
            )
            # Fail the task so the retry path will do deadline-aware escalation
            try:
                orch._client.post(
                    f"{orch._config.server_url}/tasks/{task.id}/fail",
                    json={"reason": f"Deadline exceeded ({elapsed:.0f}s overdue)"},
                )
            except Exception as exc:
                logger.warning("Failed to fail deadline for task %s: %s", task.id, exc)
            orch._notify(
                "task.deadline_exceeded",
                title=f"Task deadline exceeded: {task.title}",
                body=(f"Task {task.id} (role={task.role}) exceeded its deadline by {elapsed:.0f}s."),
                task_id=task.id,
                role=task.role,
            )

        # Warning window: task is about to expire soon
        elif 0 < task.deadline - now <= warning_window:
            remaining = task.deadline - now
            logger.warning(
                "Task %s ('%s') deadline approaching in %.0fs",
                task.id,
                task.title,
                remaining,
            )
            orch._notify(
                "task.deadline_warning",
                title=f"Task deadline approaching: {task.title}",
                body=(f"Task {task.id} (role={task.role}) will exceed its deadline in {remaining:.0f}s."),
                task_id=task.id,
                role=task.role,
            )


def _check_workflow_approval(orch: Any) -> None:
    """Check for file-based workflow approval grant.

    Looks for ``.sdd/runtime/workflow/approve_{phase_name}`` files.
    When found, grants approval and removes the file.

    Args:
        orch: The orchestrator instance.
    """
    if orch._workflow_executor is None or not orch._workflow_executor.approval_pending:
        return
    phase_name = orch._workflow_executor.current_phase_name
    approval_file = orch._workdir / ".sdd" / "runtime" / "workflow" / f"approve_{phase_name}"
    if approval_file.exists():
        reason = approval_file.read_text().strip() or "file-based approval"
        approval_file.unlink(missing_ok=True)
        # Also clean up the pending request file
        pending = orch._workdir / ".sdd" / "runtime" / "workflow" / f"approval_pending_{phase_name}.json"
        pending.unlink(missing_ok=True)
        orch._workflow_executor.grant_approval(reason=reason)
        orch._recorder.record(
            "workflow_approval_granted",
            phase=phase_name,
            reason=reason,
        )
        logger.info("Workflow approval granted for phase %r via file", phase_name)


def _handle_anomaly_signal(orch: Any, signal: object) -> None:
    """Dispatch an anomaly signal: log, stop spawning, or kill agent.

    Args:
        orch: The orchestrator instance.
        signal: An AnomalySignal instance.
    """
    import contextlib

    from bernstein.core.cost_anomaly import AnomalySignal

    assert isinstance(signal, AnomalySignal)
    orch._anomaly_detector.record_signal(signal)
    if signal.action == "kill_agent" and signal.agent_id:
        logger.warning("Anomaly [%s]: %s — killing agent", signal.rule, signal.message)
        session = orch._agents.get(signal.agent_id)
        if session:
            with contextlib.suppress(Exception):
                orch._spawner.kill(session)
    elif signal.action == "stop_spawning":
        logger.warning("Anomaly [%s]: %s — stopping new spawns", signal.rule, signal.message)
        orch._stop_spawning = True
    else:
        logger.info("Anomaly [%s]: %s", signal.rule, signal.message)


def _record_live_costs(orch: Any) -> None:
    """Update live cost tracker from active agent token usage.

    Args:
        orch: The orchestrator instance.
    """
    any_change = False
    for session in orch._agents.values():
        if session.status == "dead" or session.tokens_used <= 0:
            continue

        model_name = session.model_config.model if session.model_config else "sonnet"
        task_id = session.task_ids[0] if session.task_ids else f"live-{session.id}"
        delta_cost = orch._cost_tracker.record_cumulative(
            agent_id=session.id,
            task_id=task_id,
            model=model_name,
            total_input_tokens=session.tokens_used,
            total_output_tokens=0,
        )
        if delta_cost > 0:
            any_change = True

        if (
            orch._config.max_cost_per_agent > 0
            and session.id not in orch._cost_cap_killed_agents
            and orch._cost_tracker.spent_for_agent(session.id) >= orch._config.max_cost_per_agent
        ):
            _kill_agent_for_cost_cap(orch, session)
            any_change = True

    if not any_change:
        return

    try:
        orch._cost_tracker.save(orch._workdir / ".sdd")
    except OSError as exc:
        logger.warning("Failed to persist live cost tracker: %s", exc)
    status = orch._cost_tracker.status()
    orch._post_bulletin(
        "status",
        f"live_cost_update: {status.spent_usd:.4f} USD spent ({status.percentage_used * 100:.1f}%)",
    )


def _check_server_health(orch: Any) -> bool:
    """Ping the task server health endpoint with a short timeout.

    Updates ``_consecutive_server_failures`` and logs CRITICAL after 3
    consecutive failures so the external watchdog (or operator) knows
    the server needs attention.

    Args:
        orch: The orchestrator instance.

    Returns:
        True if the server responded successfully.
    """
    try:
        resp = orch._client.get(
            f"{orch._config.server_url}/status",
            timeout=5.0,
        )
        resp.raise_for_status()
        orch._consecutive_server_failures = 0
        return True
    except (httpx.HTTPError, httpx.TimeoutException):
        orch._consecutive_server_failures += 1
        if orch._consecutive_server_failures >= 3:
            logger.critical(
                "Task server health check failed %d consecutive times — "
                "server may have crashed (watchdog should restart it)",
                orch._consecutive_server_failures,
            )
        return False


def _record_provider_health(
    orch: Any,
    session: AgentSession,
    success: bool,
    latency_ms: float = 0.0,
    cost_usd: float = 0.0,
    tokens: int = 0,
) -> None:
    """Update provider health and cost in the router based on task outcome.

    No-op when no router is configured or the session has no provider.

    Args:
        orch: The orchestrator instance.
        session: Agent session whose provider to update.
        success: Whether the task completed successfully.
        latency_ms: Approximate task latency in milliseconds.
        cost_usd: Cost of the task in USD.
        tokens: Number of tokens used.
    """
    if orch._router is not None and session.provider is not None:
        orch._router.update_provider_health(session.provider, success, latency_ms)
        if cost_usd > 0 or tokens > 0:
            orch._router.record_provider_cost(session.provider, tokens, cost_usd)


def _reconcile_claimed_tasks(orch: Any) -> int:
    """Unclaim orphaned tasks from previous orchestrator runs.

    On startup the ``_task_to_session`` map is empty, so any task that
    the server still considers "claimed" is orphaned.  For each such
    task we POST ``/tasks/{id}/force-claim`` which transitions it back
    to *open* so it can be picked up again.

    Args:
        orch: The orchestrator instance.

    Returns:
        Number of tasks that were unclaimed.
    """
    try:
        resp = orch._client.get(f"{orch._config.server_url}/tasks?status=claimed")
        resp.raise_for_status()
        claimed = resp.json()
    except Exception:
        return 0

    unclaimed = 0
    for task in claimed if isinstance(claimed, list) else claimed.get("tasks", []):
        task_id = task.get("id", "")
        if task_id not in orch._task_to_session:
            try:
                orch._client.post(
                    f"{orch._config.server_url}/tasks/{task_id}/force-claim",
                )
                unclaimed += 1
                logger.info(
                    "Unclaimed orphan task %s (%s)",
                    task_id,
                    task.get("title", ""),
                )
            except Exception:
                pass

    if unclaimed:
        logger.warning(
            "Reconciled %d orphaned claimed tasks from previous run",
            unclaimed,
        )
    return unclaimed


def _release_stale_claims(orch: Any, claimed_tasks: list[Task]) -> int:
    """Fail claimed tasks that have been stuck longer than the timeout.

    When an agent dies silently (no crash signal, no heartbeat timeout),
    its claimed tasks stay in "claimed" forever.  This method detects
    tasks with no matching live agent that have exceeded the stale claim
    timeout and marks them failed so they can be retried.

    Args:
        orch: The orchestrator instance.
        claimed_tasks: Tasks with status "claimed" from the current tick.

    Returns:
        Number of tasks released.
    """
    now = time.time()
    timeout = orch._config.stale_claim_timeout_s
    released = 0
    for task in claimed_tasks:
        # Skip tasks that have a known live agent in this session
        if task.id in orch._task_to_session:
            agent_id = orch._task_to_session[task.id]
            agent = orch._agents.get(agent_id)
            if agent is not None and agent.status != "dead":
                continue

        # Use claimed_at (when available) to measure actual time in claimed
        # state.  Fall back to created_at for legacy tasks that pre-date the
        # claimed_at field — this is conservative (over-counts) but safe.
        claim_epoch = task.claimed_at if task.claimed_at is not None else task.created_at
        age_s = now - claim_epoch
        if age_s < timeout:
            continue

        try:
            fail_task(
                orch._client,
                orch._config.server_url,
                task.id,
                reason=f"Stale claim: task stuck in claimed state for {age_s / 60:.0f}m with no live agent",
            )
            released += 1
            logger.warning(
                "Released stale claimed task %s (%s) — stuck for %.0fm",
                task.id,
                task.title,
                age_s / 60,
            )
        except Exception:
            logger.debug("Failed to release stale task %s", task.id, exc_info=True)

    if released:
        logger.warning("Released %d stale claimed task(s)", released)
    return released


def _check_file_overlap(orch: Any, batch: list[Task]) -> bool:
    """Return True if any file in *batch* is currently owned by an active agent.

    Checks both the in-memory ``_file_ownership`` dict (cross-referenced
    against live agent status) and the persistent ``_lock_manager`` (for
    crash-recovery locks held across process restarts).  Dead agents do not
    block new batches even if they appear in the ownership index.

    Args:
        orch: The orchestrator instance.
        batch: List of tasks in the batch.

    Returns:
        True if a file overlap conflict was found.
    """
    all_files = [f for task in batch for f in task.owned_files]
    if not all_files:
        return False

    # In-memory ownership check — filters out dead agents explicitly.
    for fpath in all_files:
        owner_id = orch._file_ownership.get(fpath)
        if owner_id:
            session = orch._agents.get(owner_id)
            if session and session.status == "working":
                logger.debug(
                    "File %s owned by active agent %s, deferring batch",
                    fpath,
                    owner_id,
                )
                return True

    # Persistent lock check (survives crashes via FileLockManager TTL).
    conflicts = orch._lock_manager.check_conflicts(all_files)
    if conflicts:
        for fpath, lock in conflicts:
            logger.debug(
                "File %s locked by agent %s (task %s), deferring batch",
                fpath,
                lock.agent_id,
                lock.task_id,
            )
        return True
    return False


def _should_auto_decompose(orch: Any, task: Task) -> bool:
    """Check whether a task should be auto-decomposed.

    Args:
        orch: The orchestrator instance.
        task: The task to evaluate.

    Returns:
        True if the task should be decomposed.
    """
    if not orch._config.auto_decompose:
        return False
    return should_auto_decompose(
        task,
        orch._decomposed_task_ids,
        force_parallel=orch._config.force_parallel or orch._config.auto_decompose,
    )


def _auto_decompose_task(orch: Any, task: Task) -> None:
    """Decompose a task into sub-tasks.

    Args:
        orch: The orchestrator instance.
        task: The task to decompose.
    """
    auto_decompose_task(
        task,
        client=orch._client,
        server_url=orch._config.server_url,
        decomposed_task_ids=orch._decomposed_task_ids,
        workdir=orch._workdir,
    )


def _kill_agent_for_cost_cap(orch: Any, session: AgentSession) -> None:
    """Terminate an agent that exceeded the hard per-session cost cap.

    Args:
        orch: The orchestrator instance.
        session: The agent session to kill.
    """
    cap = orch._config.max_cost_per_agent
    spent = orch._cost_tracker.spent_for_agent(session.id)
    orch._cost_cap_killed_agents.add(session.id)
    logger.warning(
        "Killing agent %s: max_cost_per_agent exceeded ($%.4f >= $%.4f)",
        session.id,
        spent,
        cap,
    )
    orch._post_bulletin(
        "alert",
        f"agent {session.id[:12]} exceeded max_cost_per_agent (${spent:.2f} >= ${cap:.2f})",
    )
    orch._notify(
        "budget.warning",
        "Agent cost cap exceeded",
        f"Agent {session.id} exceeded max_cost_per_agent",
        agent_id=session.id,
        spent_usd=round(spent, 6),
        cap_usd=round(cap, 6),
    )

    with contextlib.suppress(Exception):
        orch._spawner.kill(session)

    from bernstein.core.lifecycle import transition_agent

    transition_agent(session, "dead", actor="orchestrator", reason="max_cost_per_agent exceeded")
    orch._release_file_ownership(session.id)
    orch._release_task_to_session(session.task_ids)
    orch._record_provider_health(session, success=False)

    # Import from orchestrator module (not task_lifecycle directly) so that
    # test patches on ``bernstein.core.orchestrator.retry_or_fail_task`` are
    # intercepted correctly.
    from bernstein.core.orchestrator import retry_or_fail_task as _retry_or_fail

    for task_id in list(session.task_ids):
        with contextlib.suppress(Exception):
            _retry_or_fail(
                task_id,
                f"Agent {session.id} exceeded max_cost_per_agent (${cap:.2f})",
                client=orch._client,
                server_url=orch._config.server_url,
                max_task_retries=orch._config.max_task_retries,
                retried_task_ids=orch._retried_task_ids,
            )


def _find_session_for_task(orch: Any, task_id: str) -> AgentSession | None:
    """Return the agent session that owns *task_id*, or None.

    Args:
        orch: The orchestrator instance.
        task_id: ID of the task to look up.

    Returns:
        Matching AgentSession, or None if not found.
    """
    agent_id = orch._task_to_session.get(task_id)
    if agent_id is None:
        return None
    return orch._agents.get(agent_id) or orch._batch_sessions.get(agent_id)


def _release_file_ownership(orch: Any, agent_id: str) -> None:
    """Release all files owned by the given agent.

    Args:
        orch: The orchestrator instance.
        agent_id: ID of the agent whose files should be released.
    """
    orch._lock_manager.release(agent_id)
    # Always clean the legacy dict so code that reads _file_ownership directly stays consistent
    to_remove = [fp for fp, owner in orch._file_ownership.items() if owner == agent_id]
    for fp in to_remove:
        del orch._file_ownership[fp]


def _release_task_to_session(orch: Any, task_ids: list[str]) -> None:
    """Remove reverse-index entries for the given task IDs.

    Args:
        orch: The orchestrator instance.
        task_ids: Task IDs to remove from the mapping.
    """
    for tid in task_ids:
        orch._task_to_session.pop(tid, None)


def _maybe_retry_task(orch: Any, task: Task) -> bool:
    """Delegate to task_lifecycle.maybe_retry_task.

    Args:
        orch: The orchestrator instance.
        task: The failed task to potentially retry.

    Returns:
        True if the task was retried.
    """
    session = _find_session_for_task(orch, task.id)
    return maybe_retry_task(
        task,
        retried_task_ids=orch._retried_task_ids,
        max_task_retries=orch._config.max_task_retries,
        client=orch._client,
        server_url=orch._config.server_url,
        quarantine=orch._quarantine,
        workdir=orch._workdir,
        session_id=session.id if session is not None else None,
    )


def _should_trigger_manager_review(orch: Any, failed_count: int) -> bool:
    """Return True when a manager queue review is warranted.

    Triggers on:
    - 3+ completions since last review
    - Any task failure
    - 5 minutes of no review (stall guard)

    Args:
        orch: The orchestrator instance.
        failed_count: Number of tasks failed since last review.

    Returns:
        True if the manager should review the queue.
    """
    now = time.time()
    if orch._completions_since_review >= orch._MANAGER_REVIEW_COMPLETION_THRESHOLD:
        return True
    if failed_count > 0:
        return True
    return orch._last_review_ts > 0 and (now - orch._last_review_ts) >= orch._MANAGER_REVIEW_STALL_S


def _run_manager_queue_review(orch: Any) -> None:
    """Invoke manager queue review and apply corrections.

    Fetches the task queue, calls the ManagerAgent to review it, and
    applies corrections (reassign, cancel, change_priority, add_task)
    via the task server.  All changes go through the server so the
    deterministic orchestrator remains in full control.

    Args:
        orch: The orchestrator instance.
    """
    from bernstein import get_templates_dir
    from bernstein.core.manager import ManagerAgent
    from bernstein.core.seed import parse_seed

    _BERNSTEIN_YAML = "bernstein.yaml"

    try:
        budget_pct = 1.0
        if orch._cost_tracker.budget_usd > 0:
            status = orch._cost_tracker.status()
            budget_pct = max(0.0, 1.0 - status.percentage_used)

        workdir = orch._workdir
        # Read internal LLM provider/model from seed config
        _mgr_provider = "openrouter_free"
        _mgr_model = "nvidia/nemotron-3-super-120b-a12b"
        _seed_path = workdir / _BERNSTEIN_YAML
        if _seed_path.exists():
            try:
                _seed = parse_seed(_seed_path)
                _mgr_provider = _seed.internal_llm_provider
                _mgr_model = _seed.internal_llm_model
            except Exception:
                pass
        manager = ManagerAgent(
            server_url=orch._config.server_url,
            workdir=workdir,
            templates_dir=get_templates_dir(workdir),
            model=_mgr_model,
            provider=_mgr_provider,
        )

        result = manager.review_queue_sync(
            completed_count=orch._completions_since_review,
            failed_count=orch._failures_since_review,
            budget_remaining_pct=budget_pct,
        )

        orch._last_review_ts = time.time()
        orch._completions_since_review = 0
        orch._failures_since_review = 0

        if result.skipped:
            return

        base = orch._config.server_url

        # Pre-validate corrections against actual server state so the
        # LLM cannot cancel non-existent tasks, re-route to invalid
        # roles, or operate on tasks in terminal states.
        task_states: dict[str, str] = {}
        try:
            resp = orch._client.get(f"{base}/tasks")
            resp.raise_for_status()
            for t in resp.json():
                task_states[t["id"]] = t.get("status", "unknown")
        except httpx.HTTPError as exc:
            logger.warning(
                "Manager review: failed to fetch task states for validation: %s",
                exc,
            )
            # Proceed without validation rather than silently dropping all corrections

        valid_roles: set[str] | None = None
        _cancellable_states = {"open", "claimed", "in_progress"}

        for correction in result.corrections:
            try:
                # Validate task_id exists in server state (skip add_task which has no task_id)
                if (
                    correction.action != "add_task"
                    and correction.task_id
                    and task_states
                    and correction.task_id not in task_states
                ):
                    logger.warning(
                        "Manager review: skipping %s for non-existent task %s",
                        correction.action,
                        correction.task_id,
                    )
                    continue

                if correction.action == "reassign" and correction.task_id and correction.new_role:
                    # Validate target role exists
                    if valid_roles is None:
                        from bernstein import get_templates_dir
                        from bernstein.core.context import available_roles

                        valid_roles = set(available_roles(get_templates_dir(orch._workdir) / "roles"))
                    if correction.new_role not in valid_roles:
                        logger.warning(
                            "Manager review: skipping reassign to invalid role %r (valid: %s)",
                            correction.new_role,
                            ", ".join(sorted(valid_roles)),
                        )
                        continue
                    orch._client.patch(
                        f"{base}/tasks/{correction.task_id}",
                        json={"role": correction.new_role},
                    )
                    logger.info(
                        "Manager review: reassigned %s to role=%s (%s)",
                        correction.task_id,
                        correction.new_role,
                        correction.reason,
                    )
                elif correction.action == "change_priority" and correction.task_id and correction.new_priority:
                    orch._client.patch(
                        f"{base}/tasks/{correction.task_id}",
                        json={"priority": correction.new_priority},
                    )
                    logger.info(
                        "Manager review: changed priority of %s to %d (%s)",
                        correction.task_id,
                        correction.new_priority,
                        correction.reason,
                    )
                elif correction.action == "cancel" and correction.task_id:
                    # Validate task is in a cancellable state
                    status = task_states.get(correction.task_id)
                    if status and status not in _cancellable_states:
                        logger.warning(
                            "Manager review: skipping cancel for task %s in non-cancellable state %r",
                            correction.task_id,
                            status,
                        )
                        continue
                    orch._client.post(
                        f"{base}/tasks/{correction.task_id}/cancel",
                        json={"reason": correction.reason or "manager review"},
                    )
                    logger.info(
                        "Manager review: cancelled %s (%s)",
                        correction.task_id,
                        correction.reason,
                    )
                elif correction.action == "add_task" and correction.new_task:
                    orch._client.post(
                        f"{base}/tasks",
                        json=correction.new_task,
                    )
                    logger.info(
                        "Manager review: added task %r (%s)",
                        correction.new_task.get("title"),
                        correction.reason,
                    )
            except httpx.HTTPError as exc:
                logger.warning("Manager review: correction %s failed: %s", correction.action, exc)

        if result.corrections:
            orch._post_bulletin(
                "status",
                f"Manager review applied {len(result.corrections)} correction(s): {result.reasoning}",
            )

    except Exception as exc:
        logger.warning("Manager queue review failed: %s", exc)


def _run_scheduled_dependency_scan(orch: Any) -> None:
    """Run the weekly dependency scan and enqueue remediation tasks.

    Args:
        orch: The orchestrator instance.
    """
    try:
        existing_titles = _load_existing_dependency_scan_task_titles(orch)
        result = orch._dependency_scanner.run_if_due(
            create_fix_task=lambda finding: _create_dependency_fix_task(orch, finding, existing_titles),
            audit_log=orch._audit_log,
        )
    except Exception as exc:
        logger.warning("Dependency scan failed: %s", exc)
        return

    if result is None:
        return

    log_level = logging.WARNING if result.status == DependencyScanStatus.VULNERABLE else logging.INFO
    logger.log(
        log_level,
        "Dependency scan completed: %s (%d findings)",
        result.status.value,
        len(result.findings),
    )
    orch._post_bulletin("status", f"dependency_scan: {result.summary}")


def _load_existing_dependency_scan_task_titles(orch: Any) -> set[str]:
    """Load open remediation task titles so weekly scans do not duplicate them.

    Args:
        orch: The orchestrator instance.

    Returns:
        Set of existing task titles.
    """
    try:
        response = orch._client.get(f"{orch._config.server_url}/tasks")
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return set()

    if not isinstance(payload, list):
        return set()
    return {
        str(item.get("title", ""))
        for item in payload
        if isinstance(item, dict)
        and str(item.get("status", "")) in {"open", "claimed", "in_progress", "pending_approval"}
    }


def _create_dependency_fix_task(
    orch: Any,
    finding: DependencyVulnerabilityFinding,
    existing_titles: set[str],
) -> str | None:
    """Create one remediation task per vulnerable package.

    Args:
        orch: The orchestrator instance.
        finding: The vulnerability finding.
        existing_titles: Set of existing task titles for dedup.

    Returns:
        The title of the created task, or None if skipped/failed.
    """
    title = f"Upgrade vulnerable dependency: {finding.package}"
    if title in existing_titles:
        return None

    description = (
        f"{finding.source} reported {finding.package} {finding.installed_version} as vulnerable.\n\n"
        f"Advisory: {finding.advisory_id}\n"
        f"Summary: {finding.summary or 'No summary provided.'}"
    )
    if finding.fix_versions:
        description += f"\nRecommended fix versions: {', '.join(finding.fix_versions)}"

    try:
        response = orch._client.post(
            f"{orch._config.server_url}/tasks",
            json={
                "title": title,
                "description": description,
                "role": "security",
                "priority": 2,
                "task_type": "fix",
            },
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to create dependency fix task for %s: %s", finding.package, exc)
        return None

    existing_titles.add(title)
    return title


def _log_summary(orch: Any, result: Any) -> None:
    """Write a one-line summary and agent state snapshot each tick.

    Args:
        orch: The orchestrator instance.
        result: The TickResult from the current tick.
    """
    log_dir = orch._workdir / ".sdd" / "runtime"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "orchestrator.log"

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    alive = sum(1 for a in orch._agents.values() if a.status != "dead")
    fp = orch._fast_path_stats
    fp_tag = f" fast_path={fp.tasks_bypassed} saved=${fp.estimated_cost_saved_usd:.2f}" if fp.tasks_bypassed else ""
    line = (
        f"[{ts}] open={result.open_tasks} agents={alive} "
        f"spawned={len(result.spawned)} reaped={len(result.reaped)} "
        f"verified={len(result.verified)} errors={len(result.errors)}{fp_tag}\n"
    )
    rotate_log_file(log_path)
    with log_path.open("a") as f:
        f.write(line)

    # Dump agent state for the live dashboard
    agents_snapshot = [
        {
            "id": s.id,
            "role": s.role,
            "status": s.status,
            "exit_code": s.exit_code,
            "model": s.model_config.model if s.model_config else None,
            "task_ids": s.task_ids,
            "pid": s.pid,
            "spawn_ts": s.spawn_ts,
            "runtime_s": round(time.time() - s.spawn_ts) if s.spawn_ts > 0 else 0,
            "agent_source": s.agent_source,
            "provider": s.provider,
            "cell_id": s.cell_id,
            "parent_id": s.parent_id,
            "log_path": str(getattr(s, "log_path", "")),
            "worktree_path": str(getattr(s, "worktree_path", "")),
            "tokens_used": s.tokens_used,
            "token_budget": s.token_budget,
            "context_window_tokens": s.context_window_tokens,
            "context_utilization_pct": s.context_utilization_pct,
            "context_utilization_alert": s.context_utilization_alert,
            "runtime_backend": s.runtime_backend,
            "bridge_session_key": s.bridge_session_key,
            "bridge_run_id": s.bridge_run_id,
            "transition_reason": s.transition_reason.value if s.transition_reason is not None else "",
            "abort_reason": s.abort_reason.value if s.abort_reason is not None else "",
            "abort_detail": s.abort_detail,
            "finish_reason": s.finish_reason,
        }
        for s in orch._agents.values()
    ]
    state_path = log_dir / "agents.json"
    try:
        with state_path.open("w") as f:
            json.dump({"ts": time.time(), "agents": agents_snapshot}, f)
    except Exception:
        pass


def _record_tick_events(orch: Any, result: Any, tasks_by_status: dict[str, list[Task]]) -> None:
    """Record replay events from a completed tick for deterministic replay.

    Args:
        orch: The orchestrator instance.
        result: The TickResult from the current tick.
        tasks_by_status: Pre-fetched task snapshot keyed by status string.
    """
    # Record spawned agents
    for session_id in result.spawned:
        session = orch._agents.get(session_id)
        if session is not None:
            orch._recorder.record(
                "agent_spawned",
                agent_id=session.id,
                role=session.role,
                model=session.model_config.model if session.model_config else None,
                provider=session.provider,
                task_ids=session.task_ids,
                agent_source=session.agent_source,
            )
            for tid in session.task_ids:
                orch._recorder.record(
                    "task_claimed",
                    task_id=tid,
                    agent_id=session.id,
                    model=session.model_config.model if session.model_config else None,
                )

    # Record verified (completed) tasks
    for task_id in result.verified:
        session = _find_session_for_task(orch, task_id)
        cost = 0.0
        if session is not None:
            cost = orch._cost_tracker.status().spent_usd
        orch._recorder.record(
            "task_completed",
            task_id=task_id,
            agent_id=session.id if session else None,
            cost_usd=round(cost, 4),
        )

    # Record verification failures
    for task_id, failed_signals in result.verification_failures:
        orch._recorder.record(
            "task_verification_failed",
            task_id=task_id,
            failed_signals=failed_signals,
        )

    # Record reaped agents
    for agent_id in result.reaped:
        orch._recorder.record("agent_reaped", agent_id=agent_id)

    # Record retried tasks
    for task_id in result.retried:
        orch._recorder.record("task_retried", task_id=task_id)
