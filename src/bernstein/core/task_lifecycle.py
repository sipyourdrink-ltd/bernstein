"""Task lifecycle: claim, spawn, complete, retry, decompose.

Methods extracted from the Orchestrator class to reduce orchestrator.py size.
These are free functions that accept the orchestrator instance (or its fields)
as explicit arguments so the Orchestrator methods can delegate to them.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.context import append_decision
from bernstein.core.cross_model_verifier import (
    CrossModelVerifierConfig,
    run_cross_model_verification_sync,
)
from bernstein.core.fast_path import (
    TaskLevel,
    classify_task,
    get_l1_model_config,
    try_fast_path_batch,
)
from bernstein.core.janitor import verify_task
from bernstein.core.metrics import get_collector
from bernstein.core.models import (
    AgentSession,
    Task,
)
from bernstein.core.quality_gates import run_quality_gates
from bernstein.core.router import RouterError
from bernstein.core.rule_enforcer import RulesConfig, load_rules_config, run_rule_enforcement
from bernstein.core.tick_pipeline import (
    CompletionData,
    fail_task,
)

if TYPE_CHECKING:
    import concurrent.futures
    from pathlib import Path

    from bernstein.core.git_ops import MergeResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Completion data extraction
# ---------------------------------------------------------------------------


def collect_completion_data(workdir: Path, session: AgentSession) -> CompletionData:
    """Read agent log file and extract structured completion data.

    Parses the agent's runtime log for files_modified and test_results.

    Args:
        workdir: Project working directory.
        session: Agent session whose log to parse.

    Returns:
        Dict with files_modified and test_results keys.
    """
    data: CompletionData = {"files_modified": [], "test_results": {}}
    log_path = workdir / ".sdd" / "runtime" / f"{session.id}.log"
    if not log_path.exists():
        return data

    try:
        log_content = log_path.read_text(encoding="utf-8", errors="replace")
        lines = log_content.splitlines()
        # Extract file modifications (lines like "Modified: path/to/file")
        files_modified: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("Modified: ") or stripped.startswith("Created: "):
                fpath = stripped.split(": ", 1)[1].strip()
                if fpath and fpath not in files_modified:
                    files_modified.append(fpath)
        data["files_modified"] = files_modified

        # Extract test results (look for pytest-style summary)
        for line in reversed(lines):
            stripped = line.strip()
            if "passed" in stripped or "failed" in stripped:
                data["test_results"] = {"summary": stripped}
                break
    except OSError as exc:
        logger.debug("Could not read agent log %s: %s", log_path, exc)

    return data


# ---------------------------------------------------------------------------
# File ownership helpers
# ---------------------------------------------------------------------------


def check_file_overlap(
    batch: list[Task],
    file_ownership: dict[str, str],
    agents: dict[str, AgentSession],
) -> bool:
    """Check if any file in the batch is owned by an active agent.

    Args:
        batch: Tasks to check for file conflicts.
        file_ownership: Mapping of filepath -> agent_id.
        agents: Agent sessions dict.

    Returns:
        True if there is a conflict, False if safe to spawn.
    """
    for task in batch:
        for fpath in task.owned_files:
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


# ---------------------------------------------------------------------------
# Task retry / fail
# ---------------------------------------------------------------------------


def maybe_retry_task(
    task: Task,
    *,
    retried_task_ids: set[str],
    max_task_retries: int,
    client: httpx.Client,
    server_url: str,
    quarantine: Any,
) -> bool:
    """Queue a retry for a failed task with model/effort escalation.

    First retry bumps effort one level (low->medium->high->max), keeps model.
    Second retry escalates model (haiku->sonnet->opus) and resets effort to high.

    Args:
        task: The failed task to potentially retry.
        retried_task_ids: Set of task IDs already retried (mutated in-place).
        max_task_retries: Maximum retries allowed.
        client: httpx client.
        server_url: Task server base URL.
        quarantine: QuarantineStore instance.

    Returns:
        True if a retry task was created, False otherwise.
    """
    if task.id in retried_task_ids:
        return False

    # Determine current retry count from title prefix [RETRY N]
    retry_count = 0
    m = re.match(r"^\[RETRY (\d+)\] ", task.title)
    if m:
        retry_count = int(m.group(1))

    if retry_count >= max_task_retries:
        base_title = re.sub(r"^\[RETRY \d+\] ", "", task.title)
        quarantine.record_failure(base_title, "Max retries exhausted")
        logger.warning(
            "Task %r exhausted %d retries -- recorded cross-run failure in quarantine",
            base_title,
            max_task_retries,
        )
        return False

    next_retry = retry_count + 1

    current_model = task.model or "sonnet"
    current_effort = task.effort or "high"

    effort_ladder = ["low", "medium", "high", "max"]
    model_ladder = ["haiku", "sonnet", "opus"]

    from bernstein.core.models import Scope as _Scope

    # High-stakes roles/scopes always get opus/max on any retry
    _high_stakes_roles = ("architect", "security")
    if task.scope == _Scope.LARGE or task.role in _high_stakes_roles:
        new_model = "opus"
        new_effort = "max"
    elif next_retry == 1:
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

    # Progressive timeout: each retry multiplies estimated_minutes by (retry_count + 2)
    progressive_minutes = task.estimated_minutes * (retry_count + 2)

    payload: dict[str, Any] = {
        "title": new_title,
        "description": new_description,
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": progressive_minutes,
        "model": new_model,
        "effort": new_effort,
    }

    try:
        resp = client.post(f"{server_url}/tasks", json=payload)
        resp.raise_for_status()
        new_task_id = resp.json().get("id", "?")
        retried_task_ids.add(task.id)
        logger.info(
            "Retry %d queued for failed task %s -> %s (model=%s effort=%s)",
            next_retry,
            task.id,
            new_task_id,
            new_model,
            new_effort,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to queue retry for task %s: %s", task.id, exc)
        return False


def retry_or_fail_task(
    task_id: str,
    reason: str,
    *,
    client: httpx.Client,
    server_url: str,
    max_task_retries: int,
    retried_task_ids: set[str],
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
        client: httpx client.
        server_url: Task server base URL.
        max_task_retries: Maximum number of retries allowed.
        retried_task_ids: Set of already-retried task IDs (mutated in-place).
        tasks_snapshot: Optional pre-fetched tasks snapshot to avoid an
            extra HTTP round-trip when the task is already in cache.
    """
    base = server_url
    max_retries = max_task_retries

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
            logger.debug("retry_or_fail_task %s: resolved from tick snapshot", task_id)

    if task is None:
        try:
            resp = client.get(f"{base}/tasks/{task_id}")
            resp.raise_for_status()
            task = Task.from_dict(resp.json())
        except httpx.HTTPError as exc:
            logger.error("retry_or_fail_task: could not fetch task %s: %s", task_id, exc)
            return

    # Dedup: prevent retry fan-out (same task retried multiple times)
    if task_id in retried_task_ids:
        logger.debug("Skipping duplicate retry for task %s", task_id)
        return
    retried_task_ids.add(task_id)

    # Extract current retry count from description marker
    marker_re = re.compile(r"^\[retry:(\d+)\]\s*")
    m = marker_re.match(task.description)
    retry_count = int(m.group(1)) if m else 0
    base_description = marker_re.sub("", task.description)

    if retry_count < max_retries:
        new_description = f"[retry:{retry_count + 1}] {base_description}"
        # Escalate model on retry: large/architect/security always opus/max;
        # other roles: sonnet->opus on 2nd retry, effort->high on 1st retry.
        from bernstein.core.models import Scope as _Scope

        _high_stakes_roles = ("architect", "security")
        if task.scope == _Scope.LARGE or task.role in _high_stakes_roles:
            retry_model = "opus"
            retry_effort = "max"
        elif retry_count >= 1:
            retry_model = "opus"
            retry_effort = "high"
        else:
            retry_model = task.model or "sonnet"
            retry_effort = task.effort or "high"
        # Progressive timeout: each retry multiplies estimated_minutes by (retry_count + 2)
        # so retry 1 doubles the time, retry 2 triples it, giving agents more runway.
        progressive_minutes = task.estimated_minutes * (retry_count + 2)
        task_body: dict[str, Any] = {
            "title": f"[RETRY {retry_count + 1}] {task.title}",
            "description": new_description,
            "role": task.role,
            "priority": task.priority,
            "scope": task.scope.value,
            "complexity": task.complexity.value,
            "estimated_minutes": progressive_minutes,
            "depends_on": task.depends_on,
            "owned_files": task.owned_files,
            "task_type": task.task_type.value,
            "model": retry_model,
            "effort": retry_effort,
        }
        # Preserve completion signals on retry
        if task.completion_signals:
            task_body["completion_signals"] = [{"type": s.type, "value": s.value} for s in task.completion_signals]
        try:
            client.post(f"{base}/tasks", json=task_body).raise_for_status()
            logger.info(
                "Retrying task %s (attempt %d/%d): %s",
                task_id,
                retry_count + 1,
                max_retries,
                reason,
            )
        except httpx.HTTPError as exc:
            logger.error("Failed to re-create task %s for retry: %s", task_id, exc)
            # Fall through to permanent fail
            fail_task(client, base, task_id, f"Max retries exceeded: {reason}")
            return
        # Fail the old task silently (it has been replaced)
        with contextlib.suppress(httpx.HTTPError):
            fail_task(client, base, task_id, f"Retried: {reason}")
    else:
        fail_task(client, base, task_id, f"Max retries exceeded: {reason}")


# ---------------------------------------------------------------------------
# Auto-decomposition
# ---------------------------------------------------------------------------


def should_auto_decompose(
    task: Task,
    decomposed_task_ids: set[str],
    workdir: Path | None = None,
    force_parallel: bool = False,
) -> bool:
    """Return True if a large task should be decomposed into subtasks.

    Decomposition is triggered for scope=LARGE tasks that haven't been
    queued for decomposition yet in this orchestrator session.

    Before decomposing, a :class:`~bernstein.core.complexity_advisor.ComplexityAdvisor`
    check is performed.  If the advisor recommends single-agent mode (few,
    tightly-coupled files), decomposition is skipped — a single agent is
    faster and avoids coordination overhead.  Pass ``force_parallel=True``
    (or set ``OrchestratorConfig.force_parallel``) to bypass this gate.

    Args:
        task: The task to check.
        decomposed_task_ids: Set of already-decomposed task IDs.
        workdir: Repository root for coupling analysis (None = skip analysis).
        force_parallel: If True, bypass the complexity advisor.

    Returns:
        True if the task should be auto-decomposed.
    """
    from bernstein.core.models import Scope

    # Already queued for decomposition in this session
    if task.id in decomposed_task_ids:
        return False
    # Manager-created decompose tasks should never be re-decomposed
    if task.title.startswith("[DECOMPOSE]"):
        return False
    # Tasks that have failed 2+ times should be decomposed regardless of scope --
    # they've proven too large for a single agent session.
    retry_match = re.match(r"^\[RETRY (\d+)\]", task.title)
    if retry_match:
        return int(retry_match.group(1)) >= 2
    # Fresh tasks: only decompose scope=LARGE
    if task.scope != Scope.LARGE:
        return False
    # Complexity advisor gate: skip decomposition if single-agent is recommended.
    if workdir is not None and not force_parallel:
        try:
            from bernstein.core.complexity_advisor import ComplexityAdvisor, ComplexityMode

            advice = ComplexityAdvisor().advise(task, workdir=workdir, force_parallel=False)
            if advice.mode == ComplexityMode.SINGLE_AGENT:
                logger.info(
                    "Complexity advisor: single-agent mode for task %s (%s) — skipping decomposition",
                    task.id,
                    advice.reason,
                )
                return False
        except Exception as exc:
            logger.debug("Complexity advisor failed, proceeding with decomposition: %s", exc)
    return True


def create_conflict_resolution_task(
    conflicting_task: Task,
    conflicting_files: list[str],
    *,
    client: httpx.Client,
    server_url: str,
    session_id: str,
) -> str | None:
    """Create a resolver task when a merge conflict is detected.

    Called by the orchestrator immediately after a failed merge so a
    dedicated ``resolver`` agent can resolve conflicts and commit.

    Args:
        conflicting_task: The original task whose agent branch conflicted.
        conflicting_files: File paths with merge conflicts.
        client: httpx client for task server requests.
        server_url: Task server base URL.
        session_id: Agent session whose branch conflicted (for context).

    Returns:
        The new resolver task ID, or None if creation failed.
    """
    files_list = "\n".join(f"- {f}" for f in conflicting_files)
    description = (
        f"A merge conflict was detected when merging the work of agent session "
        f"`{session_id}` (task: {conflicting_task.id} — {conflicting_task.title!r}).\n\n"
        f"## Conflicting files\n{files_list}\n\n"
        f"## Your job\n"
        f"1. For each conflicting file, read the conflict markers and understand both sides\n"
        f"2. Resolve each conflict — preserve intent from both sides where possible\n"
        f"3. After resolving all conflicts, run tests to verify correctness\n"
        f"4. Stage all resolved files and commit with a message explaining what was kept\n\n"
        f"Original task description:\n{conflicting_task.description}\n"
    )

    resolver_task_body: dict[str, Any] = {
        "title": f"[CONFLICT] {conflicting_task.title[:80]}",
        "description": description,
        "role": "resolver",
        "priority": max(1, conflicting_task.priority - 1),  # Higher priority
        "scope": "small",
        "complexity": "medium",
        "owned_files": conflicting_files,
    }

    try:
        resp = client.post(f"{server_url}/tasks", json=resolver_task_body)
        resp.raise_for_status()
        resolver_id: str = resp.json().get("id", "?")
        logger.info(
            "Conflict resolution task %s created for session %s (%d files: %s)",
            resolver_id,
            session_id,
            len(conflicting_files),
            ", ".join(conflicting_files),
        )
        return resolver_id
    except httpx.HTTPError as exc:
        logger.warning(
            "Failed to create conflict resolution task for session %s: %s",
            session_id,
            exc,
        )
        return None


def auto_decompose_task(
    task: Task,
    *,
    client: httpx.Client,
    server_url: str,
    decomposed_task_ids: set[str],
) -> None:
    """Queue a large task for decomposition by spawning a planner manager.

    Creates a lightweight manager task (haiku/high) that reads the original
    task and creates 3-5 atomic subtasks. The original large task stays open
    until the subtasks are done.

    Args:
        task: The large task to decompose.
        client: httpx client.
        server_url: Task server base URL.
        decomposed_task_ids: Set of decomposed task IDs (mutated in-place).
    """
    base = server_url

    manager_description = (
        f"A large task needs to be decomposed into 3-5 smaller, atomic subtasks.\n\n"
        f"## Original large task (id={task.id})\n"
        f"**Title:** {task.title}\n"
        f"**Role:** {task.role}\n"
        f"**Description:**\n{task.description}\n\n"
        f"## Your job\n"
        f"1. Read the task description carefully\n"
        f"2. Identify 3-5 specific, atomic subtasks (each completable in one agent session, < 30 min)\n"
        f"3. Each subtask should target specific files and have clear completion criteria\n"
        f"4. Create each subtask via the task server:\n"
        f"```bash\n"
        f"curl -s -X POST {base}/tasks -H 'Content-Type: application/json' \\\n"
        f'  -d \'{{"title": "...", "description": "... [subtask of {task.id}]", '
        f'"role": "{task.role}", "priority": {task.priority}, '
        f'"scope": "small", "complexity": "medium"}}\'\n'
        f"```\n"
        f"5. After creating all subtasks, exit.\n\n"
        f"IMPORTANT: Each subtask description MUST include '[subtask of {task.id}]' "
        f"so it can be tracked back to the original task."
    )

    planner_task_body: dict[str, Any] = {
        "title": f"[DECOMPOSE] {task.title[:80]}",
        "description": manager_description,
        "role": "manager",
        "priority": max(1, task.priority - 1),  # Higher priority than original
        "scope": "small",
        "complexity": "medium",
        "model": "haiku",
        "effort": "high",
    }

    try:
        resp = client.post(f"{base}/tasks", json=planner_task_body)
        resp.raise_for_status()
        planner_id = resp.json().get("id", "?")
        decomposed_task_ids.add(task.id)
        logger.info(
            "Auto-decompose: created planner task %s for large task %s ('%s')",
            planner_id,
            task.id,
            task.title,
        )
    except httpx.HTTPError as exc:
        logger.warning("Auto-decompose: failed to create planner task for %s: %s", task.id, exc)


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
    base = orch._config.server_url

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

        # Skip if any owned files overlap with active agents
        if check_file_overlap(batch, orch._file_ownership, orch._agents):
            continue

        # Check spawn backoff: skip batches that recently failed
        batch_key = frozenset(t.id for t in batch)
        fail_count, last_fail_ts = orch._spawn_failures.get(batch_key, (0, 0.0))
        # Exponential backoff: base * 2^(failures-1), capped at max
        backoff_s = (
            min(
                orch._SPAWN_BACKOFF_BASE_S * (2 ** max(fail_count - 1, 0)),
                orch._SPAWN_BACKOFF_MAX_S,
            )
            if fail_count > 0
            else 0.0
        )
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
                if action == "decompose" and len(batch) == 1:
                    auto_decompose_task(
                        task,
                        client=orch._client,
                        server_url=base,
                        decomposed_task_ids=orch._decomposed_task_ids,
                    )
            continue

        # Pre-flight: auto-decompose large tasks before claiming.
        # Creates a lightweight manager task that breaks the large task into
        # 3-5 atomic subtasks; the original stays open until subtasks complete.
        if len(batch) == 1 and should_auto_decompose(
            batch[0],
            orch._decomposed_task_ids,
            workdir=orch._workdir,
            force_parallel=orch._config.force_parallel,
        ):
            auto_decompose_task(
                batch[0],
                client=orch._client,
                server_url=base,
                decomposed_task_ids=orch._decomposed_task_ids,
            )
            continue

        # Claim tasks BEFORE spawning to prevent duplicate agents.
        # Pass expected_version for CAS (compare-and-swap) to prevent two
        # distributed nodes from claiming the same task simultaneously.
        # Abort on server errors (5xx), CAS conflicts (409), or transport failures.
        claim_failed = False
        for task in batch:
            try:
                resp = orch._client.post(
                    f"{base}/tasks/{task.id}/claim",
                    params={"expected_version": task.version},
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

        # Adaptive timeout: scale by task complexity/role
        # Architect/security tasks need 3x base, large tasks 2x, etc.
        _SCOPE_MULTIPLIER = {"small": 1.0, "medium": 1.5, "large": 2.5}
        _ROLE_MULTIPLIER = {"architect": 3.0, "security": 2.5, "manager": 2.0}
        max_estimated_s = max((t.estimated_minutes for t in batch), default=30) * 60
        scope_mult = max(_SCOPE_MULTIPLIER.get(t.scope.value, 1.0) for t in batch)
        role_mult = max(_ROLE_MULTIPLIER.get(t.role, 1.0) for t in batch)
        complexity_mult = max(scope_mult, role_mult)
        max_runtime = orch._config.max_agent_runtime_s * complexity_mult
        batch_timeout_s = int(max(120, min(int(max_estimated_s * complexity_mult), max_runtime)))

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
            _spawned_per_role[batch[0].role] += 1
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
            collector = get_collector(orch._workdir / ".sdd" / "metrics")
            collector.start_agent(
                agent_id=session.id,
                role=session.role,
                model=session.model_config.model,
                provider=session.provider or "default",
                agent_source=session.agent_source,
            )
            for _task in batch:
                collector.start_task(
                    task_id=_task.id,
                    role=session.role,
                    model=session.model_config.model,
                    provider=session.provider or "default",
                )
            logger.info(
                "Agent '%s' using prompt source: %s",
                session.id,
                session.agent_source,
            )
        except (OSError, RuntimeError, ValueError, RouterError) as exc:
            logger.error("Spawn failed for batch %s: %s", [t.id for t in batch], exc)
            result.errors.append(f"spawn: {exc}")
            collector = get_collector(orch._workdir / ".sdd" / "metrics")
            collector.record_error("agent_spawn_failed", "default", role=batch[0].role if batch else None)
            new_count = fail_count + 1
            orch._spawn_failures[batch_key] = (new_count, time.time())
            if new_count >= orch._MAX_SPAWN_FAILURES:
                for task in batch:
                    try:
                        fail_task(
                            orch._client,
                            base,
                            task.id,
                            f"Spawn failed {new_count} consecutive times: {exc}",
                        )
                    except Exception as fail_exc:
                        logger.warning("Could not mark task %s as failed: %s", task.id, fail_exc)
                orch._spawn_failures.pop(batch_key, None)


def process_completed_tasks(
    orch: Any,  # Orchestrator instance
    done_tasks: list[Task],
    result: Any,  # TickResult
) -> None:
    """Run janitor verification and record evolution metrics for done tasks.

    Skips tasks already processed in a prior tick. For each new done task,
    submits verify_task() calls in parallel via orch._executor, then
    processes post-verification steps (sync backlog, append decision,
    record evolution) after all verifications complete.

    Args:
        orch: Orchestrator instance.
        done_tasks: Tasks with status "done" fetched from the server.
        result: TickResult accumulator for verified/verification_failures lists.
    """
    # Filter to only new tasks and mark them all processed upfront.
    new_tasks: list[Task] = []
    for task in done_tasks:
        if task.id in orch._processed_done_tasks:
            continue
        orch._processed_done_tasks.add(task.id)
        new_tasks.append(task)

    if not new_tasks:
        return

    # Fan-out: submit all verify_task() calls in parallel.
    verify_futures: dict[str, concurrent.futures.Future[tuple[bool, list[str]]]] = {}
    for task in new_tasks:
        if task.completion_signals:
            verify_futures[task.id] = orch._executor.submit(verify_task, task, orch._workdir)

    # Fan-in: collect results then run sequential post-verification steps.
    for task in new_tasks:
        if task.id in verify_futures:
            passed, failed_signals = verify_futures[task.id].result()
            janitor_passed = passed
            if passed:
                result.verified.append(task.id)
            else:
                result.verification_failures.append((task.id, failed_signals))
        else:
            janitor_passed = True

        session = orch._find_session_for_task(task.id)
        # Track whether this is the first time we're reaping this session so
        # agent-lifetime metrics are recorded exactly once per agent even when
        # an agent owns multiple tasks that all complete in the same tick.
        _agent_just_reaped = session is not None and session.status != "dead"
        if session is not None:
            # Quality gates: lint/type/test checks run after janitor, before approval.
            _qg_config = getattr(orch, "_quality_gate_config", None)
            if janitor_passed and _qg_config is not None:
                _worktree_for_gates = orch._spawner.get_worktree_path(session.id)
                _gate_run_dir = _worktree_for_gates if _worktree_for_gates is not None else orch._workdir
                _qg_result = run_quality_gates(task, _gate_run_dir, orch._workdir, _qg_config)
                if not _qg_result.passed:
                    janitor_passed = False
                    _qg_failed = [
                        f"quality_gate:{r.gate}" for r in _qg_result.gate_results if r.blocked and not r.passed
                    ]
                    with contextlib.suppress(ValueError):
                        result.verified.remove(task.id)
                    result.verification_failures.append((task.id, _qg_failed))
                    logger.info(
                        "Quality gates blocked merge for task %s: %s",
                        task.id,
                        ", ".join(_qg_failed),
                    )

            # Organizational rule enforcement: .bernstein/rules.yaml checks.
            # Runs after quality gates, before cross-model verification.
            if janitor_passed:
                _rules_config: RulesConfig | None = load_rules_config(orch._workdir)
                if _rules_config is not None:
                    _re_worktree = orch._spawner.get_worktree_path(session.id)
                    _re_run_dir = _re_worktree if _re_worktree is not None else orch._workdir
                    _re_result = run_rule_enforcement(task, _re_run_dir, orch._workdir, _rules_config)
                    if not _re_result.passed:
                        janitor_passed = False
                        _re_failed = [f"rule:{v.rule_id}: {v.fix_hint}" for v in _re_result.violations if v.blocked]
                        with contextlib.suppress(ValueError):
                            result.verified.remove(task.id)
                        result.verification_failures.append((task.id, _re_failed))
                        logger.info(
                            "Rule enforcement blocked merge for task %s: %s",
                            task.id,
                            ", ".join(_re_failed),
                        )

            # Cross-model verification: route diff to a different model for review.
            # Runs after quality gates, before the approval gate.
            # None (the default) means disabled; pass CrossModelVerifierConfig() to enable.
            _cmv_raw = getattr(orch._config, "cross_model_verify", None)
            _cmv_config: CrossModelVerifierConfig = (
                _cmv_raw if isinstance(_cmv_raw, CrossModelVerifierConfig) else CrossModelVerifierConfig(enabled=False)
            )
            if janitor_passed and _cmv_config.enabled:
                _cmv_worktree = orch._spawner.get_worktree_path(session.id)
                _cmv_path = _cmv_worktree if _cmv_worktree is not None else orch._workdir
                _cmv_writer = session.model_config.model
                _cmv_verdict = run_cross_model_verification_sync(task, _cmv_path, _cmv_writer, _cmv_config)
                if _cmv_verdict.verdict == "request_changes" and _cmv_config.block_on_issues:
                    janitor_passed = False
                    _cmv_issues_str = "; ".join(_cmv_verdict.issues) if _cmv_verdict.issues else _cmv_verdict.feedback
                    with contextlib.suppress(ValueError):
                        result.verified.remove(task.id)
                    result.verification_failures.append((task.id, [f"cross_model_review:{_cmv_issues_str}"]))
                    logger.info(
                        "Cross-model review blocked merge for task %s (reviewer=%s): %s",
                        task.id,
                        _cmv_verdict.reviewer_model,
                        _cmv_verdict.feedback,
                    )
                    # Queue a fix task so the issues get addressed.
                    _cmv_fix_description = (
                        f"Cross-model review flagged issues in task {task.id} "
                        f"({task.title!r}).\n\n"
                        f"**Reviewer:** {_cmv_verdict.reviewer_model}\n"
                        f"**Feedback:** {_cmv_verdict.feedback}\n\n"
                        f"**Issues to fix:**\n"
                        + "\n".join(f"- {i}" for i in _cmv_verdict.issues)
                        + f"\n\nOriginal task description:\n{task.description}\n"
                    )
                    _cmv_fix_body: dict[str, Any] = {
                        "title": f"[REVIEW-FIX] {task.title[:80]}",
                        "description": _cmv_fix_description,
                        "role": task.role,
                        "priority": max(1, task.priority - 1),
                        "scope": "small",
                        "complexity": "medium",
                        "owned_files": task.owned_files,
                    }
                    try:
                        orch._client.post(f"{orch._config.server_url}/tasks", json=_cmv_fix_body).raise_for_status()
                    except httpx.HTTPError as _cmv_exc:
                        logger.warning(
                            "cross_model_verifier: failed to create fix task for %s: %s",
                            task.id,
                            _cmv_exc,
                        )
                else:
                    logger.info(
                        "Cross-model review approved task %s (reviewer=%s)",
                        task.id,
                        _cmv_verdict.reviewer_model,
                    )

            orch._record_provider_health(session, success=janitor_passed)
            _skip_merge = False
            if janitor_passed and orch._approval_gate is not None:
                _approval_result = orch._approval_gate.evaluate(
                    task,
                    session_id=session.id,
                )
                if _approval_result.rejected:
                    _skip_merge = True
                    logger.warning(
                        "Approval gate: task %s rejected -- skipping merge for agent %s",
                        task.id,
                        session.id,
                    )
                elif not _approval_result.approved:
                    # PR mode -- create PR then skip local merge
                    _skip_merge = True
                    _worktree_path = orch._spawner.get_worktree_path(session.id)
                    if _worktree_path is not None:
                        # Gather metadata for the PR body
                        _pr_collector = get_collector(orch._workdir / ".sdd" / "metrics")
                        _pr_task_m = _pr_collector.task_metrics.get(task.id)
                        _pr_cost_usd = _pr_task_m.cost_usd if _pr_task_m else 0.0
                        _pr_completion = collect_completion_data(orch._workdir, session)
                        _pr_test_summary = _pr_completion.get("test_results", {}).get("summary", "")
                        _pr_url = orch._approval_gate.create_pr(
                            task,
                            worktree_path=_worktree_path,
                            session_id=session.id,
                            labels=orch._config.pr_labels,
                            role=session.role,
                            model=session.model_config.model,
                            cost_usd=_pr_cost_usd,
                            test_summary=_pr_test_summary,
                        )
                        if _pr_url:
                            logger.info(
                                "Approval gate: PR created for task %s: %s",
                                task.id,
                                _pr_url,
                            )
                    else:
                        logger.warning(
                            "Approval gate PR mode: no worktree for agent %s -- cannot create PR",
                            session.id,
                        )
            _merge_result: MergeResult | None = orch._spawner.reap_completed_agent(session, skip_merge=_skip_merge)
            session.status = "dead"
            logger.info("Agent %s finished task %s, process reaped", session.id, task.id)

            # Route merge conflicts to a dedicated resolver agent.
            if (
                _merge_result is not None
                and not _merge_result.success
                and _merge_result.conflicting_files
                and not _skip_merge
            ):
                create_conflict_resolution_task(
                    task,
                    _merge_result.conflicting_files,
                    client=orch._client,
                    server_url=orch._config.server_url,
                    session_id=session.id,
                )
                orch._post_bulletin(
                    "alert",
                    f"merge conflict in {len(_merge_result.conflicting_files)} files — "
                    f"resolver task created (task {task.id})",
                )

        # Record task completion in the operational metrics collector so
        # run summaries and evolution analysis see real duration/success data.
        _collector = get_collector(orch._workdir / ".sdd" / "metrics")
        _task_m = _collector.task_metrics.get(task.id)
        _cost_usd = _task_m.cost_usd if _task_m else 0.0

        # Record cost in the per-run budget tracker and persist to disk.
        _agent_id = session.id if session else "unknown"
        _model = session.model_config.model if session else "unknown"
        _tokens_in = _task_m.tokens_prompt if _task_m else 0
        _tokens_out = _task_m.tokens_completion if _task_m else 0
        orch._cost_tracker.record(
            agent_id=_agent_id,
            task_id=task.id,
            model=_model,
            input_tokens=_tokens_in,
            output_tokens=_tokens_out,
            cost_usd=_cost_usd if _cost_usd > 0 else None,
        )
        try:
            orch._cost_tracker.save(orch._workdir / ".sdd")
        except OSError as exc:
            logger.warning("Failed to persist cost tracker: %s", exc)

        _collector.complete_task(task.id, success=janitor_passed, janitor_passed=janitor_passed, cost_usd=_cost_usd)
        if session is not None:
            # complete_agent_task must be called before end_agent so that
            # end_agent() has non-zero task counts and writes the AGENT_SUCCESS
            # metric to the JSONL file.
            _collector.complete_agent_task(session.id, success=janitor_passed)
            _collector.end_agent(session.id)
            # Record agent lifetime to evolution collector (once per agent).
            if orch._evolution is not None and _agent_just_reaped:
                try:
                    _agent_m = _collector.agent_metrics.get(session.id)
                    _lifetime = round(
                        (time.time() - session.spawn_ts) if session.spawn_ts > 0 else 0.0,
                        2,
                    )
                    _tasks_done = _agent_m.tasks_completed if _agent_m else 0
                    orch._evolution.record_agent_lifetime(
                        agent_id=session.id,
                        role=session.role,
                        lifetime_seconds=_lifetime,
                        tasks_completed=_tasks_done,
                        model=session.model_config.model,
                    )
                except Exception as exc:
                    logger.warning("Evolution record_agent_lifetime failed: %s", exc)

        # Post bulletin: task completed or failed (with janitor result)
        if janitor_passed:
            orch._post_bulletin(
                "status",
                f"task completed: {task.title} ({task.id})",
            )
            orch._notify(
                "task.completed",
                f"Task completed: {task.title}",
                task.result_summary or "",
                task_id=task.id,
                role=task.role,
            )
        else:
            orch._post_bulletin(
                "alert",
                f"task failed janitor: {task.title} ({task.id})",
            )
            orch._notify(
                "task.failed",
                f"Task failed: {task.title}",
                task.result_summary or "Janitor verification did not pass.",
                task_id=task.id,
                role=task.role,
            )

        orch._sync_backlog_file(task)

        if task.result_summary:
            try:
                append_decision(
                    orch._workdir,
                    task.id,
                    task.title,
                    task.result_summary,
                )
            except Exception as exc:
                logger.warning("append_decision failed for task %s: %s", task.id, exc)

        if orch._evolution is not None:
            model = session.model_config.model if session else None
            provider = session.provider if session else None
            duration = (
                (_task_m.end_time - _task_m.start_time)
                if _task_m and _task_m.end_time
                else (time.time() - session.spawn_ts if session and session.spawn_ts > 0 else 0.0)
            )
            try:
                orch._evolution.record_task_completion(
                    task=task,
                    duration_seconds=round(duration, 2),
                    cost_usd=_cost_usd,
                    janitor_passed=janitor_passed,
                    model=model,
                    provider=provider,
                )
            except Exception as exc:
                logger.warning("Evolution record_task_completion failed: %s", exc)


# ---------------------------------------------------------------------------
# Private helpers shared with claim_and_spawn_batches
# ---------------------------------------------------------------------------


def _get_changed_files_in_worktree(worktree_path: Path) -> list[str]:
    """Return the list of files changed in a worktree relative to HEAD.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        List of changed file paths, or empty list on any error.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.splitlines() if f.strip()]
    except Exception as exc:
        logger.debug("_get_changed_files_in_worktree failed for %s: %s", worktree_path, exc)
    return []


def _claim_file_ownership(orch: Any, agent_id: str, tasks: list[Task]) -> None:
    """Register file ownership for files in the given tasks.

    Uses :class:`~bernstein.core.file_locks.FileLockManager` when available,
    falling back to the legacy ``_file_ownership`` dict for compatibility.

    Args:
        orch: Orchestrator instance.
        agent_id: The agent claiming ownership.
        tasks: Tasks whose owned_files to claim.
    """
    lock_manager = getattr(orch, "_lock_manager", None)
    for task in tasks:
        files = task.owned_files
        if not files:
            continue
        if lock_manager is not None:
            lock_manager.acquire(
                files,
                agent_id=agent_id,
                task_id=task.id,
                task_title=task.title,
            )
        # Keep legacy dict in sync so existing code that reads _file_ownership still works
        for fpath in files:
            orch._file_ownership[fpath] = agent_id
