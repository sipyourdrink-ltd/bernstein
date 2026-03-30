"""Real-time circuit breaker for purpose enforcement.

Auto-terminates misbehaving agents and quarantines their changes:
- **Scope violations** — agent edited files outside the task's ``owned_files``
- **Budget violations** — agent exceeded a per-session token limit

When a violation is detected the circuit breaker:
1. Writes a structured JSON ``.kill`` signal file for the orchestrator to process.
2. Appends an entry to ``.sdd/metrics/kill_audit.jsonl`` for audit purposes.
3. Writes quarantine metadata to ``.sdd/quarantine/{session_id}.json`` so the
   agent's branch is preserved for human review.

The orchestrator picks up ``.kill`` files in its next tick via
``check_kill_signals()`` in ``agent_lifecycle``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.lifecycle import transition_agent
from bernstein.core.models import KillReason

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kill audit log
# ---------------------------------------------------------------------------


def log_kill_event(
    workdir: Path,
    session_id: str,
    reason: KillReason | str,
    detail: str,
    *,
    files: list[str] | None = None,
    requester: str = "circuit_breaker",
) -> None:
    """Append a kill event to ``.sdd/metrics/kill_audit.jsonl``.

    Args:
        workdir: Project root directory.
        session_id: The agent session that was terminated.
        reason: The :class:`KillReason` (or plain string) for termination.
        detail: Human-readable description of the violation.
        files: Files involved in any violation.
        requester: Component that requested the kill (e.g. ``"circuit_breaker"``).
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    event: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "reason": str(reason.value) if isinstance(reason, KillReason) else str(reason),
        "detail": detail,
        "requester": requester,
    }
    if files:
        event["files"] = files
    with open(metrics_dir / "kill_audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Quarantine metadata
# ---------------------------------------------------------------------------


def write_quarantine_metadata(
    workdir: Path,
    session_id: str,
    reason: KillReason | str,
    detail: str,
    *,
    files: list[str] | None = None,
    branch: str | None = None,
) -> None:
    """Write quarantine metadata for a killed agent's changes.

    The agent's git branch is preserved and can be inspected with::

        git checkout quarantine/<session_id>

    This function records the reason and context so the branch can be reviewed
    and either merged (after fixing the violation) or discarded.

    Args:
        workdir: Project root directory.
        session_id: The terminated agent's session ID.
        reason: Why the agent was killed.
        detail: Human-readable description of the violation.
        files: Files involved in the violation.
        branch: Name of the git branch where the agent's changes live.
    """
    quarantine_dir = workdir / ".sdd" / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "quarantined_at": datetime.now(UTC).isoformat(),
        "reason": str(reason.value) if isinstance(reason, KillReason) else str(reason),
        "detail": detail,
    }
    if files:
        metadata["files"] = files
    if branch:
        metadata["branch"] = branch
    out_path = quarantine_dir / f"{session_id}.json"
    out_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    logger.info("Quarantine metadata written for agent %s (branch: %s)", session_id, branch)


# ---------------------------------------------------------------------------
# Enforce kill signal
# ---------------------------------------------------------------------------


def enforce_kill_signal(
    workdir: Path,
    session_id: str,
    reason: KillReason | str,
    detail: str,
    *,
    files: list[str] | None = None,
    requester: str = "circuit_breaker",
    branch: str | None = None,
) -> None:
    """Write a structured kill signal and record it in the audit log.

    The kill file is written to ``.sdd/runtime/{session_id}.kill`` as JSON.
    The orchestrator's ``check_kill_signals()`` reads this file on the next
    tick and terminates the matching agent.

    For violation reasons (SCOPE_VIOLATION, BUDGET_EXCEEDED,
    GUARDRAIL_VIOLATION) quarantine metadata is also written so the agent's
    branch is preserved for human review.

    Args:
        workdir: Project root directory.
        session_id: The agent session to terminate.
        reason: The termination reason.
        detail: Human-readable description of why the agent is being killed.
        files: Files involved in the violation (if any).
        requester: Component requesting the kill.
        branch: Agent's git branch (for quarantine).
    """
    runtime_dir = workdir / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    reason_str = reason.value if isinstance(reason, KillReason) else str(reason)
    kill_payload: dict[str, Any] = {
        "ts": time.time(),
        "reason": reason_str,
        "detail": detail,
        "requester": requester,
    }
    if files:
        kill_payload["files"] = files

    kill_file = runtime_dir / f"{session_id}.kill"
    kill_file.write_text(json.dumps(kill_payload), encoding="utf-8")
    logger.warning(
        "Kill signal written for agent %s (reason=%s): %s",
        session_id,
        reason_str,
        detail,
    )

    # Audit log — every kill is recorded regardless of reason
    log_kill_event(workdir, session_id, reason, detail, files=files, requester=requester)

    # Quarantine metadata for violation kills so the branch can be reviewed
    _VIOLATION_REASONS = {
        KillReason.SCOPE_VIOLATION,
        KillReason.BUDGET_EXCEEDED,
        KillReason.GUARDRAIL_VIOLATION,
    }
    reason_enum: KillReason | None = None
    with contextlib.suppress(ValueError):
        reason_enum = KillReason(reason_str)
    if reason_enum in _VIOLATION_REASONS:
        write_quarantine_metadata(
            workdir,
            session_id,
            reason,
            detail,
            files=files,
            branch=branch,
        )


# ---------------------------------------------------------------------------
# Runtime scope monitoring
# ---------------------------------------------------------------------------


def _get_worktree_changed_files(worktree_path: Path) -> list[str] | None:
    """Return files modified in *worktree_path* relative to HEAD.

    Runs ``git diff --name-only HEAD`` plus ``git ls-files --others --exclude-standard``
    (untracked files) to capture both staged/unstaged edits and new files.

    Returns None if git is unavailable or the directory is not a repo.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        diff_files = [f.strip() for f in result.stdout.splitlines() if f.strip()]

        # Also include untracked files (new files not yet committed)
        result2 = subprocess.run(
            ["git", "-C", str(worktree_path), "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        untracked = [f.strip() for f in result2.stdout.splitlines() if f.strip()]
        return list(dict.fromkeys(diff_files + untracked))  # deduplicate, preserve order
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None


def _files_outside_scope(changed_files: list[str], owned_files: list[str]) -> list[str]:
    """Return files from *changed_files* that are outside *owned_files*.

    Args:
        changed_files: Files modified by the agent.
        owned_files: Allowed file/directory paths for the task.

    Returns:
        Files that do not match any entry in *owned_files*.
    """
    out_of_scope: list[str] = []
    for f in changed_files:
        if not any(f == owned or f.startswith(owned.rstrip("/") + "/") for owned in owned_files):
            out_of_scope.append(f)
    return out_of_scope


def _get_worktree_diff(worktree_path: Path) -> str | None:
    """Return the full git diff for *worktree_path* relative to HEAD.

    Used by guardrail checks that need the diff content (not just filenames).
    Returns None if git is unavailable or the directory is not a repo.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None


def check_scope_violations(orch: Any, result: Any) -> None:
    """Detect agents editing files outside their task scope and kill them.

    For each active agent with tasks that define ``owned_files`` and a git
    worktree, this function:

    1. Gets the list of currently modified files via ``git diff --name-only HEAD``.
    2. Checks each file against the task's ``owned_files`` prefixes.
    3. On violation: writes a structured kill signal and records quarantine
       metadata so the branch is preserved for review.

    Agents without a worktree are skipped — scope is checked at merge time
    by ``guardrails.run_guardrails()`` for those cases.

    Args:
        orch: Orchestrator instance (must expose ``_agents``, ``_spawner``,
            ``_workdir``, and ``_client`` attributes).
        result: Current :class:`TickResult` to record reaped agent IDs into.
    """
    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue

        # Collect owned_files from all tasks assigned to this session
        owned_files: list[str] = []
        tasks_by_id = _lookup_tasks(orch, session.task_ids)
        for task in tasks_by_id:
            owned_files.extend(task.owned_files or [])

        if not owned_files:
            continue  # No scope defined for this session's tasks

        # Need a worktree to inspect live changes
        worktree_path = orch._spawner.get_worktree_path(session.id)
        if worktree_path is None or not Path(worktree_path).is_dir():
            continue

        changed_files = _get_worktree_changed_files(Path(worktree_path))
        if changed_files is None:
            continue  # git unavailable or not a repo — skip silently

        out_of_scope = _files_outside_scope(changed_files, owned_files)
        if not out_of_scope:
            continue

        detail = (
            f"{len(out_of_scope)} file(s) modified outside task scope: "
            + ", ".join(out_of_scope[:5])
            + (" ..." if len(out_of_scope) > 5 else "")
        )
        logger.warning(
            "Scope violation by agent %s: %s",
            session.id,
            detail,
        )

        # Determine the agent's branch name for quarantine (worktree branch)
        branch: str | None = _get_worktree_branch(Path(worktree_path))

        enforce_kill_signal(
            orch._workdir,
            session.id,
            KillReason.SCOPE_VIOLATION,
            detail,
            files=out_of_scope,
            branch=branch,
        )
        # Mark session as being killed so we don't fire again next tick
        if session.status != "dead":
            transition_agent(session, "dead", actor="circuit_breaker", reason="scope violation")
        result.reaped.append(session.id)


def _lookup_tasks(orch: Any, task_ids: list[str]) -> list[Task]:
    """Fetch task objects for the given IDs from the task server.

    Returns an empty list if the server is unreachable.

    Args:
        orch: Orchestrator instance.
        task_ids: Task IDs to look up.
    """
    if not task_ids:
        return []
    tasks: list[Task] = []
    base = orch._config.server_url
    for tid in task_ids:
        with contextlib.suppress(Exception):
            resp = orch._client.get(f"{base}/tasks/{tid}")
            if resp.status_code == 200:
                from bernstein.core.tick_pipeline import _task_from_dict  # pyright: ignore[reportPrivateUsage]

                tasks.append(_task_from_dict(resp.json()))
    return tasks


def _get_worktree_branch(worktree_path: Path) -> str | None:
    """Return the current branch name in *worktree_path*.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        Branch name string, or None if it cannot be determined.
    """
    with contextlib.suppress(Exception):
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    return None


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def check_budget_violations(orch: Any, result: Any) -> None:
    """Kill agents that have exceeded their per-task token budget.

    Reads ``AgentSession.token_budget`` (0 = disabled) for each session.
    Token counts come from ``AgentSession.tokens_used``, which is updated each
    tick by ``token_monitor.check_token_growth`` (called before this function).

    The hard-kill threshold is **2x** the per-task budget: the budget hint
    embedded in the agent prompt is the soft limit; the circuit breaker fires
    at 2x to catch runaway agents that ignore the hint.

    On violation: writes a structured kill signal, logs to the kill audit, and
    writes quarantine metadata.

    Args:
        orch: Orchestrator instance (must expose ``_agents``, ``_workdir``,
            and ``_config`` attributes).
        result: Current :class:`TickResult` to record reaped agent IDs into.
    """
    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue
        budget: int = getattr(session, "token_budget", 0)
        if budget <= 0:
            continue  # No budget defined for this session (unlimited)
        kill_threshold = budget * 2  # Hard-kill at 2x the soft budget
        if session.tokens_used <= kill_threshold:
            continue

        detail = (
            f"Per-task token budget exceeded: {session.tokens_used:,} tokens used"
            f" vs {kill_threshold:,} hard limit (2x {budget:,} budget)"
        )
        logger.warning(
            "Budget violation by agent %s: %s",
            session.id,
            detail,
        )

        worktree_path = orch._spawner.get_worktree_path(session.id)
        branch: str | None = None
        if worktree_path is not None and Path(worktree_path).is_dir():
            branch = _get_worktree_branch(Path(worktree_path))

        enforce_kill_signal(
            orch._workdir,
            session.id,
            KillReason.BUDGET_EXCEEDED,
            detail,
            branch=branch,
        )
        if session.status != "dead":
            transition_agent(session, "dead", actor="circuit_breaker", reason="budget exceeded")
        result.reaped.append(session.id)


# ---------------------------------------------------------------------------
# Runtime guardrail enforcement
# ---------------------------------------------------------------------------


def check_guardrail_violations(orch: Any, result: Any) -> None:
    """Scan live worktree diffs for hard guardrail violations and kill violators.

    Currently enforces secret detection: if an agent's in-progress diff
    contains credentials, API keys, or private key material, the agent is
    immediately terminated and its changes quarantined.

    Only agents with an active git worktree are checked — agents without a
    worktree are checked at merge time by ``guardrails.run_guardrails()``.

    Args:
        orch: Orchestrator instance (must expose ``_agents``, ``_spawner``,
            ``_workdir``, and ``_client`` attributes).
        result: Current :class:`TickResult` to record reaped agent IDs into.
    """
    from bernstein.core.guardrails import check_secrets  # avoid circular import

    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue

        worktree_path = orch._spawner.get_worktree_path(session.id)
        if worktree_path is None or not Path(worktree_path).is_dir():
            continue

        diff = _get_worktree_diff(Path(worktree_path))
        if not diff:
            continue

        guardrail_results = check_secrets(diff)
        blocked = [r for r in guardrail_results if r.blocked]
        if not blocked:
            continue

        detail = "; ".join(r.detail for r in blocked)
        logger.warning(
            "Guardrail violation by agent %s: %s",
            session.id,
            detail,
        )

        branch: str | None = _get_worktree_branch(Path(worktree_path))

        enforce_kill_signal(
            orch._workdir,
            session.id,
            KillReason.GUARDRAIL_VIOLATION,
            detail,
            branch=branch,
        )
        if session.status != "dead":
            transition_agent(session, "dead", actor="circuit_breaker", reason="guardrail violation")
        result.reaped.append(session.id)
