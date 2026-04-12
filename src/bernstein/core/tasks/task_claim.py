# pyright: reportPrivateUsage=false
"""Task claiming and batch spawn bridge.

Extracted from task_lifecycle.py — contains claim_and_spawn_batches,
file ownership helpers, speculative warm pool, and file overlap detection.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.defaults import TASK
from bernstein.core.fast_path import (
    TaskLevel,
    classify_task,
    get_l1_model_config,
    try_fast_path_batch,
)
from bernstein.core.metrics import get_collector
from bernstein.core.router import RouterError
from bernstein.core.spawn_analyzer import SpawnAnalyzer, SpawnFailureAnalysis
from bernstein.core.tasks.models import (
    AgentSession,
    Task,
    TaskStatus,
)
from bernstein.core.tasks.task_completion import _get_changed_files_in_worktree
from bernstein.core.tasks.task_spawn_bridge import auto_decompose_task, should_auto_decompose
from bernstein.core.team_state import TeamStateStore
from bernstein.core.tick_pipeline import (
    complete_task,
    fail_task,
)

if TYPE_CHECKING:
    from bernstein.core.wal import WALWriter

logger = logging.getLogger(__name__)

_XL_ROLES = frozenset({"architect", "security", "manager"})


# ---------------------------------------------------------------------------
# File ownership helpers
# ---------------------------------------------------------------------------


def infer_affected_paths(task: Task) -> set[str]:
    """Infer file paths a task is likely to edit from its title and description.

    Scans the combined title + description text for explicit path references
    (e.g. ``src/bernstein/core/foo.py``) and bare module names (e.g. ``foo.py``).
    Bare module names are resolved against the ``src/bernstein`` tree; only the
    first match is kept to avoid false positives.

    Args:
        task: Task whose content to scan.

    Returns:
        Set of relative file paths the task is expected to touch.
    """
    from pathlib import Path as _Path

    text = f"{task.title} {task.description}"

    # Match explicit paths like src/bernstein/core/foo.py or tests/unit/test_bar.py
    paths: set[str] = set(re.findall(r"(?:src/bernstein|tests/unit|tests/integration)/\S+\.py", text))

    # Match bare module names like "orchestrator.py" and resolve to real paths
    for match in re.findall(r"\b(\w+\.py)\b", text):
        # Skip if we already have a fully qualified path ending with this name
        if any(p.endswith(match) for p in paths):
            continue
        candidates = list(_Path("src/bernstein").rglob(match))
        if candidates:
            paths.add(str(candidates[0]))

    return paths


def _get_active_agent_files(orch: Any) -> set[str]:
    """Return the set of files currently being edited by active agents.

    Inspects the git diff in each active agent's worktree to discover which
    files have uncommitted changes.  Falls back to ``_file_ownership`` entries
    for agents whose worktree cannot be inspected.

    Args:
        orch: Orchestrator instance.

    Returns:
        Set of file paths (relative to repo root) being edited by active agents.
    """
    active_files: set[str] = set()
    spawner = getattr(orch, "_spawner", None)

    for agent_id, session in orch._agents.items():
        if session.status == "dead":
            continue
        # Try to get real changed files from the worktree git diff
        worktree_path = None
        if spawner is not None:
            _get_wt = getattr(spawner, "get_worktree_path", None)
            worktree_path = _get_wt(agent_id) if _get_wt is not None else None
        if worktree_path is not None:
            changed = _get_changed_files_in_worktree(worktree_path)
            active_files.update(changed)
        # Also include statically declared owned_files from file_ownership
        for fpath, owner in orch._file_ownership.items():
            if owner == agent_id:
                active_files.add(fpath)

    return active_files


def check_file_overlap(
    batch: list[Task],
    file_ownership: dict[str, str],
    agents: dict[str, AgentSession],
) -> bool:
    """Check if any file in the batch is owned by an active agent.

    Checks both explicitly declared ``owned_files`` and paths inferred from the
    task title/description via :func:`infer_affected_paths`.

    Args:
        batch: Tasks to check for file conflicts.
        file_ownership: Mapping of filepath -> agent_id.
        agents: Agent sessions dict.

    Returns:
        True if there is a conflict, False if safe to spawn.
    """
    for task in batch:
        # Check both explicit owned_files and inferred paths
        all_paths = set(task.owned_files) | infer_affected_paths(task)
        for fpath in all_paths:
            if fpath in file_ownership:
                owner = file_ownership[fpath]
                # Only conflict if the owning agent is still alive
                owner_session = agents.get(owner)
                if owner_session and owner_session.status != "dead":
                    logger.debug(
                        "File %s owned by active agent %s, skipping batch",
                        fpath,
                        owner,
                    )
                    return True
    return False


def prepare_speculative_warm_pool(orch: Any, task_graph: Any, tasks: list[Task]) -> None:
    """Pre-create warm-pool capacity for tasks that are one dependency away.

    This keeps AGENT-022 aligned with Bernstein's short-lived-agent invariant:
    only worktrees/adapter capacity are prepared ahead of time. No task is
    claimed and no sleeping agent process is created.

    Args:
        orch: Orchestrator instance.
        task_graph: TaskGraph for the current tick.
        tasks: Current task snapshot across statuses.
    """
    warm_pool = getattr(getattr(orch, "_spawner", None), "_warm_pool", None)
    if warm_pool is None or getattr(orch, "is_shutting_down", lambda: False)():
        return

    candidates = _speculative_warm_pool_candidates(orch, task_graph, tasks)
    if not candidates:
        return

    desired_idle = min(warm_pool.config.max_slots, len({task.role for task in candidates}))
    current_ready = warm_pool.stats().get("ready", 0)
    if desired_idle <= 0 or current_ready >= desired_idle:
        return

    from bernstein.core.warm_pool import PoolSlot

    created = 0
    try:
        for candidate in candidates[: desired_idle - current_ready]:
            warm_pool.add_slot(
                PoolSlot(
                    slot_id=f"spec-{candidate.id}",
                    role=candidate.role,
                    worktree_path="",
                    created_at=0.0,
                )
            )
            created += 1
    except RuntimeError as exc:
        logger.debug("Speculative warm-pool preparation skipped: %s", exc)
        return

    if created > 0:
        logger.info(
            "Speculative warm-pool prep: created %d idle worktree(s) for near-ready roles %s",
            created,
            sorted({task.role for task in candidates}),
        )


def _speculative_warm_pool_candidates(orch: Any, task_graph: Any, tasks: list[Task]) -> list[Task]:
    """Return blocked tasks worth pre-warming for near-future execution."""
    tasks_by_id = {task.id: task for task in tasks}
    active_files = _get_active_agent_files(orch)
    candidates: list[Task] = []

    for task in tasks:
        if task.status != TaskStatus.OPEN:
            continue
        blocking_edges = [
            edge for edge in task_graph.edges_to(task.id) if edge.semantic_type.value in {"blocks", "validates"}
        ]
        if not blocking_edges:
            continue
        unresolved = [
            edge.source
            for edge in blocking_edges
            if tasks_by_id.get(edge.source) is not None and tasks_by_id[edge.source].status != TaskStatus.DONE
        ]
        if len(unresolved) != 1:
            continue
        if set(task.owned_files) & active_files:
            continue
        candidates.append(task)

    candidates.sort(key=lambda task: (task.priority, -task.estimated_minutes, task.id))
    return candidates


def _batch_timeout_seconds(batch: list[Task]) -> int:
    """Return the spawn timeout bucket for a task batch.

    The timeout contract is intentionally coarse-grained so operators can reason
    about behavior without reconstructing adaptive multipliers:
    small=15m, medium=30m, large=60m, xl=120m.
    """
    bucket_seconds = max(TASK.scope_timeout_s.get(task.scope.value, 30 * 60) for task in batch)
    xl_batch = any(task.role in _XL_ROLES for task in batch) or any(
        task.scope.value == "large" and task.complexity.value == "high" for task in batch
    )
    return TASK.xl_timeout_s if xl_batch else bucket_seconds


def _claim_file_ownership(orch: Any, agent_id: str, tasks: list[Task]) -> None:
    """Register file ownership for files in the given tasks.

    Uses :class:`~bernstein.core.file_locks.FileLockManager` when available,
    falling back to the legacy ``_file_ownership`` dict for compatibility.

    Also claims ownership for paths inferred from the task title/description
    (CRITICAL-007) so that subsequent ``check_file_overlap`` calls detect
    conflicts even when tasks lack explicit ``owned_files``.

    Args:
        orch: Orchestrator instance.
        agent_id: The agent claiming ownership.
        tasks: Tasks whose owned_files to claim.
    """
    lock_manager = getattr(orch, "_lock_manager", None)
    for task in tasks:
        explicit_files = task.owned_files
        inferred_files = infer_affected_paths(task)
        all_files = list(set(explicit_files) | inferred_files)
        if not all_files:
            continue
        if lock_manager is not None:
            lock_manager.acquire(
                all_files,
                agent_id=agent_id,
                task_id=task.id,
                task_title=task.title,
            )
        # Keep legacy dict in sync so existing code that reads _file_ownership still works
        for fpath in all_files:
            orch._file_ownership[fpath] = agent_id


# ---------------------------------------------------------------------------
# Claim and spawn
# ---------------------------------------------------------------------------


def claim_and_spawn_batches(
    orch: Any,  # Orchestrator instance (avoids circular import)
    batches: list[list[Task]],
    alive_count: int,
    assigned_task_ids: set[str],
    done_ids: set[str],
    result: Any,  # TickResult
) -> None:
    """Claim tasks and spawn agents for each ready batch.

    Iterates over role-grouped batches, enforces capacity/overlap/backoff
    guards, claims tasks on the server, spawns an agent, and records metrics.
    Batches that fail to spawn are tracked for backoff and eventually failed.

    Args:
        orch: Orchestrator instance.
        batches: Role-grouped task batches from group_by_role.
        alive_count: Current number of alive agents (used to enforce max_agents cap).
        assigned_task_ids: Task IDs already owned by active agents (mutated in-place).
        done_ids: IDs of already-completed tasks (reserved for future guard use).
        result: TickResult accumulator for spawned/error lists.
    """
    if getattr(orch, "is_shutting_down", lambda: False)():
        logger.debug("Skipping claim/spawn: orchestrator is shutting down")
        return

    # Pre-spawn rate-limit check: avoid wasting worktree/process resources
    # when the provider is known to be throttling requests (CRITICAL-003).
    _adapter = getattr(getattr(orch, "_spawner", None), "_adapter", None)
    if _adapter is not None and _adapter.is_rate_limited():
        logger.warning("Provider rate-limited — skipping all spawns this tick")
        return

    # Convergence guard: block entire spawn wave if system is overloaded.
    _cg = getattr(orch, "_convergence_guard", None)
    if _cg is not None:
        _merge_queue = getattr(orch, "_merge_queue", None)
        _pending_merges = len(_merge_queue) if _merge_queue is not None else 0
        _error_rate = _cg.current_error_rate()
        _spawn_rate = _cg.current_spawn_rate()
        _cg_status = _cg.is_converged(
            pending_merges=_pending_merges,
            active_agents=alive_count,
            error_rate=_error_rate if _error_rate >= 0 else None,
            spawn_rate=_spawn_rate,
        )
        if not _cg_status.ready:
            logger.warning(
                "Convergence guard blocking spawn wave: %s",
                "; ".join(_cg_status.reasons),
            )
            return

    base = orch._config.server_url
    spawn_analyzer = SpawnAnalyzer()
    if not hasattr(orch, "_spawn_failure_history"):
        orch._spawn_failure_history = {}
    raw_spawn_failure_history = getattr(orch, "_spawn_failure_history", {})
    if not isinstance(raw_spawn_failure_history, dict):
        raw_spawn_failure_history = {}
        orch._spawn_failure_history = raw_spawn_failure_history
    spawn_failure_history = cast(
        "dict[frozenset[str], list[SpawnFailureAnalysis]]",
        raw_spawn_failure_history,
    )

    # Compute fair per-role caps: ceil(max_agents * role_tasks / total_tasks).
    # Prevents any single role from consuming all agent slots while other roles starve.
    _all_task_count = sum(len(b) for b in batches)
    _tasks_per_role: dict[str, int] = defaultdict(int)
    # Count open task batches per role — direct cap prevents spawning more agents
    # than there are work items for a role (idle-agent accumulation guard).
    _batches_per_role: dict[str, int] = defaultdict(int)
    for _b in batches:
        if _b:
            _tasks_per_role[_b[0].role] += len(_b)
            _batches_per_role[_b[0].role] += 1

    # Count currently alive agents per role (baseline before this tick's spawns)
    # Exclude idle agents (those sent SHUTDOWN signal) from count since they are
    # exiting and won't accept new work. This ensures spawn prevention doesn't
    # prevent spawning when a role's last agent is idle and waiting to exit.
    _alive_per_role: dict[str, int] = defaultdict(int)
    for _agent in orch._agents.values():
        if _agent.status != "dead" and _agent.id not in orch._idle_shutdown_ts:
            _alive_per_role[_agent.role] += 1

    # Starvation prevention: promote batches for roles with 0 alive agents to the
    # front of the spawn queue. Guarantees a starving role gets at least one agent
    # before over-represented roles receive additional agents. Within each tier
    # (starving / non-starving), stable sort preserves round-robin ordering from
    # group_by_role so no role is permanently delayed.
    _starving_roles: set[str] = {b[0].role for b in batches if b and _alive_per_role[b[0].role] == 0}
    if _starving_roles:
        batches = sorted(batches, key=lambda b: 0 if (b and b[0].role in _starving_roles) else 1)
        logger.debug(
            "Starvation prevention: %d role(s) with 0 agents promoted to front: %s",
            len(_starving_roles),
            sorted(_starving_roles),
        )

    # Track agents spawned this tick per role (avoids stale alive_per_role during loop)
    _spawned_per_role: dict[str, int] = defaultdict(int)

    # Track titles claimed this tick to prevent duplicate agent assignments.
    # Strips [RETRY N] prefixes so retries don't bypass the dedup check.
    def _base_title(title: str) -> str:
        t = title
        while t.startswith("[RETRY"):
            t = t.split("] ", 1)[-1] if "] " in t else t
        return t.strip()

    _claimed_titles: set[str] = set()
    for agent in orch._agents.values():
        if agent.status != "dead":
            for tid in agent.task_ids:
                _claimed_titles.add(tid)

    for batch in batches:
        if getattr(orch, "is_shutting_down", lambda: False)():
            logger.debug("Stopping claim/spawn loop: orchestrator is shutting down")
            break
        if alive_count >= orch._config.max_agents:
            break

        # Skip batches where any task is already assigned to an active agent
        if any(t.id in assigned_task_ids for t in batch):
            continue

        # Enforce per-role cap: no role gets more than ceil(max_agents * role_tasks / total_tasks)
        # agents. This prevents a role with many tasks from occupying all slots while other roles
        # have tasks but zero agents (starvation).
        # Also capped at the number of open task batches for the role: never spawn more agents
        # than there are work items. Prevents idle accumulation when a role's queue shrinks.
        if _all_task_count > 0 and batch:
            _role = batch[0].role
            _role_cap = math.ceil(orch._config.max_agents * _tasks_per_role[_role] / _all_task_count)
            # Cap at open batches count: role can have at most one agent per available task batch
            _effective_role_cap = min(_role_cap, _batches_per_role[_role])
            _current_role_agents = _alive_per_role[_role] + _spawned_per_role[_role]
            if _current_role_agents >= _effective_role_cap:
                logger.debug(
                    "Skipping batch for role %r: at cap (%d/%d agents for %d batches)",
                    _role,
                    _current_role_agents,
                    _effective_role_cap,
                    _batches_per_role[_role],
                )
                continue

        # Dedup: skip if a task with the same base title is already active
        batch_base_titles = {_base_title(t.title) for t in batch}
        if batch_base_titles & _claimed_titles:
            logger.debug(
                "Skipping batch -- duplicate title already active: %s",
                batch_base_titles & _claimed_titles,
            )
            continue

        # Response cache: skip spawning if an identical task was already completed.
        # Check the semantic cache for a verified result — if found, complete the
        # task immediately (zero tokens, instant result).
        _response_cache: Any = getattr(orch, "_response_cache", None)
        if _response_cache is not None and len(batch) == 1:
            _task = batch[0]
            try:
                from bernstein.core.semantic_cache import ResponseCacheManager

                _cache_key = ResponseCacheManager.task_key(_task.role, _task.title, _task.description)
                _cached_entry, _sim = _response_cache.lookup_entry(_cache_key)
                if _cached_entry is not None and _cached_entry.verified:
                    logger.info(
                        "Cache hit for task '%s' (sim=%.2f) — skipping agent spawn",
                        _task.title,
                        _sim,
                    )
                    complete_task(orch._client, orch._config.server_url, _task.id, _cached_entry.response)
                    result.verified.append(_task.id)
                    continue
            except Exception as exc:
                logger.debug("Response cache lookup failed for %s: %s", _task.id, exc)

        # Skip if any owned files overlap with active agents
        _batch_sessions = getattr(orch, "_batch_sessions", {})
        _ownership_sessions = {**orch._agents, **(_batch_sessions if isinstance(_batch_sessions, dict) else {})}
        if check_file_overlap(batch, orch._file_ownership, _ownership_sessions):
            continue

        # Skip if inferred paths overlap with files actively being edited
        # in other agents' worktrees (hot-file detection — CRITICAL-007).
        _active_files = _get_active_agent_files(orch)
        if _active_files:
            _batch_inferred: set[str] = set()
            for _t in batch:
                _batch_inferred |= infer_affected_paths(_t)
            _overlap = _batch_inferred & _active_files
            if _overlap:
                logger.info(
                    "Skipping batch — file overlap with active agent worktree: %s",
                    _overlap,
                )
                continue

        # Check spawn backoff: skip batches that recently failed
        batch_key = frozenset(t.id for t in batch)
        fail_count, last_fail_ts = orch._spawn_failures.get(batch_key, (0, 0.0))
        failure_history = spawn_failure_history.get(batch_key, [])
        # Exponential backoff: base * 2^(failures-1), capped at max
        backoff_s = (
            min(
                orch._SPAWN_BACKOFF_BASE_S * (2 ** max(fail_count - 1, 0)),
                orch._SPAWN_BACKOFF_MAX_S,
            )
            if fail_count > 0
            else 0.0
        )
        if failure_history:
            should_retry, analyzed_delay = spawn_analyzer.should_retry(
                failure_history,
                max_retries=orch._MAX_SPAWN_FAILURES,
            )
            backoff_s = max(backoff_s, analyzed_delay)
            if not should_retry:
                logger.error(
                    "Skipping batch %s permanently after analyzed spawn failures",
                    [t.id for t in batch],
                )
                for task in batch:
                    with contextlib.suppress(Exception):
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            "Spawn failed permanently after classified failures",
                        )
                orch._spawn_failures.pop(batch_key, None)
                spawn_failure_history.pop(batch_key, None)
                continue
        if fail_count > 0 and (time.time() - last_fail_ts) < backoff_s:
            logger.warning(
                "Skipping batch %s: in backoff after %d consecutive spawn failure(s)",
                [t.id for t in batch],
                fail_count,
            )
            continue

        # Cross-run quarantine: skip tasks that have repeatedly failed across runs.
        # action="skip" -> skip entirely; action="decompose" -> auto-decompose first.
        quarantined_tasks = [t for t in batch if orch._quarantine.is_quarantined(t.title)]
        if quarantined_tasks:
            for task in quarantined_tasks:
                entry = orch._quarantine.get_entry(task.title)
                action = entry.action if entry else "skip"
                logger.warning(
                    "Skipping quarantined task %s (title=%r, fail_count=%d, action=%s)",
                    task.id,
                    task.title,
                    entry.fail_count if entry else 0,
                    action,
                )
                if action == "decompose" and len(batch) == 1 and getattr(orch._config, "auto_decompose", False):
                    auto_decompose_task(
                        task,
                        client=orch._client,
                        server_url=base,
                        decomposed_task_ids=orch._decomposed_task_ids,
                        workdir=orch._workdir,
                    )
            continue

        # Pre-flight: auto-decompose large tasks before claiming.
        # Creates a lightweight manager task that breaks the large task into
        # 3-5 atomic subtasks; the original stays open until subtasks complete.
        # Respects auto_decompose config — disabled by default.
        if (
            getattr(orch._config, "auto_decompose", False)
            and len(batch) == 1
            and should_auto_decompose(
                batch[0],
                orch._decomposed_task_ids,
                workdir=orch._workdir,
                force_parallel=orch._config.force_parallel,
            )
        ):
            auto_decompose_task(
                batch[0],
                client=orch._client,
                server_url=base,
                decomposed_task_ids=orch._decomposed_task_ids,
                workdir=orch._workdir,
            )
            continue

        # Claim tasks BEFORE spawning to prevent duplicate agents.
        # Pass expected_version for CAS (compare-and-swap) to prevent two
        # distributed nodes from claiming the same task simultaneously.
        # Abort on server errors (5xx), CAS conflicts (409), or transport failures.
        claim_failed = False
        _orch_session_id: str | None = getattr(orch, "session_id", None)
        for task in batch:
            try:
                _claim_params: dict[str, Any] = {"expected_version": task.version}
                if _orch_session_id is not None:
                    _claim_params["claimed_by_session"] = _orch_session_id
                resp = orch._client.post(
                    f"{base}/tasks/{task.id}/claim",
                    params=_claim_params,
                )
                if resp.status_code == 409:
                    logger.info(
                        "CAS conflict claiming task %s (version %d) -- another node claimed it",
                        task.id,
                        task.version,
                    )
                    result.errors.append(f"claim:{task.id}: CAS conflict (version {task.version})")
                    claim_failed = True
                    break
                if resp.status_code >= 500:
                    logger.error(
                        "Server error %d claiming task %s -- aborting spawn",
                        resp.status_code,
                        task.id,
                    )
                    result.errors.append(f"claim:{task.id}: server error {resp.status_code}")
                    claim_failed = True
                    break
            except httpx.TransportError as exc:
                logger.error(
                    "Server unreachable claiming task %s: %s -- aborting spawn",
                    task.id,
                    exc,
                )
                result.errors.append(f"claim:{task.id}: {exc}")
                claim_failed = True
                break
        if claim_failed:
            continue

        # WAL: record pre-execution intent (committed=False).
        # The matching committed=True entry is written after successful spawn.
        # On crash recovery, uncommitted entries indicate tasks that were
        # claimed on the server but whose agent was never spawned.
        _wal: WALWriter | None = getattr(orch, "_wal_writer", None)
        if _wal is not None:
            for task in batch:
                try:
                    _wal.write_entry(
                        decision_type="task_claimed",
                        inputs={"task_id": task.id, "role": task.role, "title": task.title},
                        output={"batch_size": len(batch)},
                        actor="task_lifecycle",
                        committed=False,
                    )
                except OSError:
                    logger.debug("WAL write failed for task_claimed %s", task.id)

        # Response cache: if a functionally identical task was already completed,
        # return the cached result without spawning an agent (20-40% savings target).
        # Only applied to single-task batches — multi-task batches have complex
        # inter-task dependencies that make result reuse unsafe.
        if len(batch) == 1:
            _rc = getattr(orch, "_response_cache", None)
            if _rc is not None:
                _rc_task = batch[0]
                _rc_key = _rc.task_key(_rc_task.role, _rc_task.title, _rc_task.description)
                _cached_entry, _rc_sim = _rc.lookup_entry(_rc_key)
                if _cached_entry is not None and _cached_entry.verified:
                    _rc_completed = False
                    try:
                        complete_task(orch._client, base, _rc_task.id, _cached_entry.response)
                        # Move backlog file on cache hit
                        from bernstein.core.tasks.task_completion import _move_backlog_ticket

                        _move_backlog_ticket(orch._workdir, _rc_task)

                        assigned_task_ids.add(_rc_task.id)
                        _claimed_titles.add(_base_title(_rc_task.title))
                        result.spawned.append(f"response-cache:{_rc_task.id}")
                        logger.info(
                            "Verified response cache hit (similarity=%.3f) for task %s (%r) -- skipping spawn",
                            _rc_sim,
                            _rc_task.id,
                            _rc_task.title,
                        )
                        _rc.save()
                        _rc_completed = True
                    except Exception as _rc_exc:
                        logger.warning(
                            "Response cache complete_task failed for %s: %s -- falling through to spawn",
                            _rc_task.id,
                            _rc_exc,
                        )
                    if _rc_completed:
                        continue
                elif _cached_entry is not None:
                    logger.info(
                        "Ignoring unverified response cache hit for task %s (%r)",
                        _rc_task.id,
                        _rc_task.title,
                    )

        # Fast-path: try deterministic execution for trivial (L0) tasks.
        # Runs inline, marks task complete on server, skips spawner entirely.
        if try_fast_path_batch(
            batch,
            orch._workdir,
            orch._client,
            base,
            orch._fast_path_stats,
        ):
            assigned_task_ids.update(t.id for t in batch)
            result.spawned.append(f"fast-path:{batch[0].id}")
            continue

        # L1 downgrade: classify single-task batches and override to cheapest model
        if len(batch) == 1:
            l1_check = classify_task(batch[0])
            if l1_check.level == TaskLevel.L1 and not batch[0].model:
                l1_cfg = get_l1_model_config()
                batch[0].model = l1_cfg.model
                batch[0].effort = l1_cfg.effort
                logger.info(
                    "L1 downgrade for task %s -> %s/%s (%s)",
                    batch[0].id,
                    l1_cfg.model,
                    l1_cfg.effort,
                    l1_check.reason,
                )

        # Provider batch: submit eligible low-risk single-task work to
        # OpenAI/Anthropic batch APIs instead of spawning a local CLI agent.
        if len(batch) == 1:
            _batch_api = getattr(orch, "_batch_api", None)
            if _batch_api is not None:
                _batch_result = _batch_api.try_submit(orch, batch[0])
                if _batch_result.handled:
                    if _batch_result.submitted:
                        assigned_task_ids.add(batch[0].id)
                        _claimed_titles.add(_base_title(batch[0].title))
                        result.spawned.append(_batch_result.session_id or f"provider-batch:{batch[0].id}")
                    elif _batch_result.reason:
                        result.errors.append(f"batch:{batch[0].id}: {_batch_result.reason}")
                    continue

        batch_timeout_s = _batch_timeout_seconds(batch)
        _shadow_bandit_decision: Any | None = None
        _routing_bandit: Any = getattr(orch, "_bandit_router", None)
        _bandit_mode = str(getattr(orch, "_bandit_routing_mode", "static"))
        if len(batch) == 1 and _routing_bandit is not None:
            _bandit_task = batch[0]
            if not _bandit_task.model and not _bandit_task.effort:
                try:
                    _bandit_decision = _routing_bandit.select(_bandit_task)
                    if _bandit_mode == "bandit":
                        _bandit_task.model = _bandit_decision.model
                        _bandit_task.effort = _bandit_decision.effort
                        logger.info(
                            "Bandit routing selected %s/%s for task %s: %s",
                            _bandit_decision.model,
                            _bandit_decision.effort,
                            _bandit_task.id,
                            _bandit_decision.reason,
                        )
                    elif _bandit_mode == "bandit-shadow":
                        _shadow_bandit_decision = _bandit_decision
                        logger.info(
                            "Bandit shadow routing would select %s/%s for task %s: %s",
                            _bandit_decision.model,
                            _bandit_decision.effort,
                            _bandit_task.id,
                            _bandit_decision.reason,
                        )
                except Exception as _bandit_exc:
                    logger.warning(
                        "Bandit routing failed for task %s; using static routing: %s",
                        _bandit_task.id,
                        _bandit_exc,
                    )
        elif len(batch) > 1 and _routing_bandit is not None:
            logger.debug(
                "Bandit routing skipped for multi-task batch %s; static batch escalation keeps attribution clear",
                [task.id for task in batch],
            )

        try:
            # Check if any task in this batch has a preserved worktree for resume
            resume_worktree = next(
                (orch._preserved_worktrees[t.id] for t in batch if t.id in orch._preserved_worktrees),
                None,
            )
            if resume_worktree is not None:
                changed_files = _get_changed_files_in_worktree(resume_worktree)
                session = orch._spawner.spawn_for_resume(
                    batch,
                    worktree_path=resume_worktree,
                    changed_files=changed_files,
                )
                for _t in batch:
                    orch._preserved_worktrees.pop(_t.id, None)
                logger.info(
                    "Resumed %s in preserved worktree %s for tasks: %s",
                    session.id,
                    resume_worktree,
                    [t.id for t in batch],
                )
            else:
                session = orch._spawner.spawn_for_tasks(batch)

            if _shadow_bandit_decision is not None and _routing_bandit is not None:
                _session_config = session.model_config
                _routing_bandit.record_shadow_decision(
                    task=batch[0],
                    decision=_shadow_bandit_decision,
                    executed_model=_session_config.model,
                    executed_effort=_session_config.effort,
                )

            # --- A/B Testing ---
            # When A/B test mode is enabled, deterministically route each task to one
            # of two models using a 50/50 hash split so results can be compared later.
            # Only single-task batches are eligible (multi-task batches are excluded
            # because cost and quality attribution is ambiguous across tasks).
            if getattr(orch._config, "ab_test", False) and len(batch) == 1:
                from bernstein.core.ab_test_results import model_for_task

                ab_task = batch[0]
                primary_model = session.model_config.model
                # Derive the alt model: sonnet ↔ opus; gpt: o3 ↔ gpt-5.4
                if "gpt" in primary_model or "o3" in primary_model:
                    alt_model = "gpt-5.4" if "o3" in primary_model else "o3"
                else:
                    alt_model = "opus" if "sonnet" in primary_model.lower() else "sonnet"

                # 50/50 deterministic split: some tasks go to primary, others to alt
                routed_model = model_for_task(ab_task.id, primary_model, alt_model)
                if routed_model != primary_model:
                    # Re-spawn this task with the alt model (the primary session is
                    # discarded — spawn a new one with the correct model override).
                    try:
                        logger.info(
                            "A/B TEST: routing task %s to model %s (hash split)",
                            ab_task.id,
                            routed_model,
                        )
                        # Record the A/B assignment so reports can track the split
                        _ab_split_tracker = getattr(orch, "_ab_split_tracker", None)
                        if isinstance(_ab_split_tracker, dict):
                            _ab_split_tracker[ab_task.id] = routed_model
                        alt_session = orch._spawner.spawn_for_tasks(batch, model_override=routed_model)
                        alt_session.timeout_s = batch_timeout_s
                        # Replace the primary session with the routed alt session
                        del orch._agents[session.id]
                        session = alt_session
                    except Exception as ab_exc:
                        logger.warning("A/B TEST: alt-model spawn failed, keeping primary: %s", ab_exc)
                else:
                    # This task is assigned to the primary model — record it
                    _ab_split_tracker = getattr(orch, "_ab_split_tracker", None)
                    if isinstance(_ab_split_tracker, dict):
                        _ab_split_tracker[ab_task.id] = primary_model
                    logger.info(
                        "A/B TEST: routing task %s to model %s (hash split)",
                        ab_task.id,
                        primary_model,
                    )

            session.timeout_s = batch_timeout_s
            orch._agents[session.id] = session
            for _t in batch:
                orch._task_to_session[_t.id] = session.id
            _claim_file_ownership(orch, session.id, batch)
            alive_count += 1
            result.spawned.append(session.id)
            assigned_task_ids.update(t.id for t in batch)
            _claimed_titles.update(_base_title(t.title) for t in batch)
            session.heartbeat_ts = time.time()
            orch._spawn_failures.pop(batch_key, None)
            spawn_failure_history.pop(batch_key, None)
            _spawned_per_role[batch[0].role] += 1
            # Track spawn rate in convergence guard
            _convergence = getattr(orch, "_convergence_guard", None)
            if _convergence is not None:
                _convergence.record_spawn()
            # Track active-agent count for rate-limit load spreading
            _rl_tracker = getattr(orch, "_rate_limit_tracker", None)
            if _rl_tracker is not None and session.provider:
                _rl_tracker.increment_active(session.provider)

            logger.info(
                "Spawned %s for %d tasks: %s",
                session.id,
                len(batch),
                [t.id for t in batch],
            )
            # WAL: commit the claim — agent was successfully spawned.
            # This pairs with the committed=False entry written before spawn.
            if _wal is not None:
                for _t in batch:
                    try:
                        _wal.write_entry(
                            decision_type="task_spawn_confirmed",
                            inputs={"task_id": _t.id, "agent_id": session.id},
                            output={"role": session.role},
                            actor="task_lifecycle",
                            committed=True,
                        )
                    except OSError:
                        logger.debug("WAL write failed for task_spawn_confirmed %s", _t.id)
            try:
                rec_engine = RecommendationEngine(orch._workdir)
                rec_engine.build()
                recommendations = rec_engine.for_role(session.role)
                rec_engine.record_hits(session.role, recommendations)
            except Exception as exc:
                logger.debug("Recommendation hit tracking failed: %s", exc)
            try:
                TeamStateStore(orch._workdir / ".sdd").on_spawn(
                    session.id,
                    session.role,
                    model=session.model_config.model,
                    task_ids=[t.id for t in batch],
                    provider=session.provider or "",
                )
            except Exception as _ts_exc:
                logger.debug("Team state on_spawn failed: %s", _ts_exc)

            collector = get_collector(orch._workdir / ".sdd" / "metrics")
            collector.start_agent(
                agent_id=session.id,
                role=session.role,
                model=session.model_config.model,
                provider=session.provider or "default",
                agent_source=session.agent_source,
                tenant_id=batch[0].tenant_id,
            )
            for _task in batch:
                collector.start_task(
                    task_id=_task.id,
                    role=session.role,
                    model=session.model_config.model,
                    provider=session.provider or "default",
                    tenant_id=_task.tenant_id,
                )
            logger.info(
                "Agent '%s' using prompt source: %s",
                session.id,
                session.agent_source,
            )
        except (OSError, RuntimeError, ValueError, RouterError) as exc:
            logger.error("Spawn failed for batch %s: %s", [t.id for t in batch], exc)
            result.errors.append(f"spawn: {exc}")
            analysis = spawn_analyzer.analyze(exc, batch[0])
            batch_history = spawn_failure_history.setdefault(batch_key, [])
            batch_history.append(analysis)
            collector = get_collector(orch._workdir / ".sdd" / "metrics")
            collector.record_error(
                f"agent_spawn_failed:{analysis.error_type}",
                "default",
                role=batch[0].role if batch else None,
                tenant_id=batch[0].tenant_id if batch else "default",
            )
            if not analysis.is_transient:
                for task in batch:
                    try:
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            f"Spawn failed permanently ({analysis.error_type}): {analysis.detail}",
                        )
                    except Exception as fail_exc:
                        logger.warning("Could not mark task %s as failed: %s", task.id, fail_exc)
                orch._spawn_failures.pop(batch_key, None)
                spawn_failure_history.pop(batch_key, None)
                continue
            new_count = fail_count + 1
            orch._spawn_failures[batch_key] = (new_count, time.time())
            should_retry, _ = spawn_analyzer.should_retry(batch_history, max_retries=orch._MAX_SPAWN_FAILURES)
            if new_count >= orch._MAX_SPAWN_FAILURES or not should_retry:
                for task in batch:
                    try:
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            f"Spawn failed {new_count} consecutive times ({analysis.error_type}): {analysis.detail}",
                        )
                    except Exception as fail_exc:
                        logger.warning("Could not mark task %s as failed: %s", task.id, fail_exc)
                orch._spawn_failures.pop(batch_key, None)
                spawn_failure_history.pop(batch_key, None)
            else:
                # Transient failure — release claimed tasks immediately so they
                # don't stay stuck in "claimed" status for the 15-min timeout.
                for task in batch:
                    try:
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            f"Spawn failed (transient, attempt {new_count}): {analysis.detail}",
                        )
                    except Exception as fail_exc:
                        logger.warning(
                            "Could not release task %s after transient spawn failure: %s",
                            task.id,
                            fail_exc,
                        )
