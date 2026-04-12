"""Merge, push, trace finalization, and reap helpers for spawner.

Free functions that encapsulate merge/push/trace operations.  AgentSpawner
delegates to these from its own methods.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.git_ops import MergeResult, merge_with_conflict_detection
from bernstein.core.models import AgentBackend, AgentSession
from bernstein.core.prometheus import merge_duration
from bernstein.core.traces import AgentTrace, TraceStore, finalize_trace
from bernstein.plugins.manager import get_plugin_manager

if TYPE_CHECKING:
    import subprocess

    from bernstein.core.container import ContainerManager
    from bernstein.core.in_process_agent import InProcessAgent
    from bernstein.core.warm_pool import PoolSlot, WarmPool
    from bernstein.core.worktree import WorktreeManager

logger = logging.getLogger(__name__)


def _sanitise_for_log(value: str) -> str:
    """Strip CR/LF from ``value`` so attacker-controlled input cannot
    inject fake log lines.

    Used at every log site that touches data read out of the pending
    pushes file or subprocess stderr (CodeQL/Sonar py/log-injection
    S5145). Keep this function cheap and side-effect-free -- it is
    called inside the spawner hot path.
    """
    return value.replace("\r", "").replace("\n", "") if value else value


# ---------------------------------------------------------------------------
# Merge and worktree branch merge
# ---------------------------------------------------------------------------


def merge_and_cleanup_worktree(
    session: AgentSession,
    skip_merge: bool,
    *,
    defer_cleanup: bool = False,
    worktree_paths: dict[str, Path],
    worktree_roots: dict[str, Path],
    worktree_managers: dict[Path, WorktreeManager],
    merge_locks: dict[Path, threading.Lock],
    warm_pool_entries: dict[str, PoolSlot],
    warm_pool: WarmPool | None,
    workdir: Path,
    merge_worktree_branch_fn: Any,
) -> MergeResult | None:
    """Merge worktree branch back and optionally clean up.

    Args:
        session: The agent session whose worktree to process.
        skip_merge: When True, skip the merge step.
        defer_cleanup: When True, skip worktree cleanup so the caller
            can inspect the merge result and clean up later via
            ``cleanup_worktree``.  Used by task_lifecycle to ensure
            the worktree survives until after PR creation and merge
            verification (BUG-4 fix).
        worktree_paths: Mutable map of session_id -> worktree path.
        worktree_roots: Mutable map of session_id -> repo root.
        worktree_managers: Map of repo root -> WorktreeManager.
        merge_locks: Mutable map of repo root -> Lock.
        warm_pool_entries: Mutable map of session_id -> PoolSlot.
        warm_pool: Optional warm pool.
        workdir: Project working directory.
        merge_worktree_branch_fn: Callable(session_id, repo_root) -> MergeResult.

    Returns:
        MergeResult when worktrees are enabled and skip_merge is False
        (None otherwise).
    """
    if defer_cleanup:
        worktree_path = worktree_paths.get(session.id)
        worktree_root = worktree_roots.get(session.id, workdir.resolve())
    else:
        worktree_path = worktree_paths.pop(session.id, None)
        worktree_root = worktree_roots.pop(session.id, workdir.resolve())
    worktree_mgr = worktree_managers.get(worktree_root)
    merge_result: MergeResult | None = None

    if worktree_path is not None and worktree_mgr is not None:
        if not skip_merge:
            merge_lock = merge_locks.setdefault(worktree_root, threading.Lock())
            with merge_lock:
                merge_start = time.perf_counter()
                merge_result = merge_worktree_branch_fn(session.id, repo_root=worktree_root)
                merge_duration.observe(time.perf_counter() - merge_start)

                from bernstein.core.metric_collector import get_collector

                merge_ok = merge_result is not None and merge_result.success
                for task_id in session.task_ids:
                    get_collector().record_merge_result(task_id, success=merge_ok)

                if merge_result and merge_result.success:
                    from bernstein.core.git_ops import safe_push

                    push_result = safe_push(worktree_root, "main")
                    if push_result.ok:
                        logger.info("Pushed merged work from %s to origin/main", session.id)
                    else:
                        logger.warning("Push failed after merge for %s: %s", session.id, push_result.stderr)
        if not defer_cleanup:
            warm_entry = warm_pool_entries.pop(session.id, None)
            if warm_entry is not None and warm_pool is not None:
                warm_pool.release_slot(warm_entry.slot_id)
            else:
                worktree_mgr.cleanup(session.id)

    return merge_result


# ---------------------------------------------------------------------------
# Pending push retry queue
# ---------------------------------------------------------------------------


def pending_pushes_path(workdir: Path) -> Path:
    """Return the path to the pending-pushes JSONL file."""
    return workdir / ".sdd" / "runtime" / "pending_pushes.jsonl"


def record_pending_push(
    workdir: Path,
    session_id: str,
    branch: str,
    repo_root: Path,
) -> None:
    """Append a failed push to the retry queue on disk."""
    path = pending_pushes_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": session_id,
        "branch": branch,
        "repo_root": str(repo_root),
        "ts": time.time(),
    }
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        logger.info("Queued pending push for %s (%s)", session_id, repo_root)
    except OSError as exc:
        logger.error("Failed to write pending push for %s: %s", session_id, exc)


def validate_pending_push_entry(
    line: str,
    safe_base: Path,
) -> tuple[Path, str, str] | None:
    """Parse and validate a single pending-push entry line.

    Returns:
        ``(repo_root, branch, session_id)`` on success, or ``None``
        if the entry is invalid or should be skipped.
    """
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(entry, dict):
        return None

    raw_repo_root = entry.get("repo_root")
    if not isinstance(raw_repo_root, str):
        return None
    try:
        candidate_root = Path(raw_repo_root).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        relative_root = candidate_root.relative_to(safe_base)
    except ValueError:
        logger.warning(
            "Skipping pending push entry: repo_root %r escapes workspace",
            _sanitise_for_log(raw_repo_root),
        )
        return None

    repo_root = (safe_base / relative_root).resolve()
    if not (repo_root / ".git").exists():
        return None

    branch = entry.get("branch", "main")
    if not isinstance(branch, str):
        branch = "main"
    session_id = entry.get("session_id", "unknown")
    if not isinstance(session_id, str):
        session_id = "unknown"
    return repo_root, branch, session_id


def retry_pending_pushes(workdir: Path) -> int:
    """Retry any pushes recorded in the pending-pushes file.

    Successfully pushed entries are removed from the file.  Entries
    that still fail are kept for the next tick.

    Returns:
        Number of pushes successfully retried.
    """
    path = pending_pushes_path(workdir)
    if not path.exists():
        return 0

    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0

    if not lines:
        return 0

    from bernstein.core.git_ops import safe_push

    remaining: list[str] = []
    retried = 0

    safe_base = pending_pushes_path(workdir).resolve().parent.parent.parent
    for line in lines:
        validated = validate_pending_push_entry(line, safe_base)
        if validated is None:
            continue
        repo_root, branch, session_id = validated

        safe_session_id = _sanitise_for_log(session_id)
        safe_repo_root = _sanitise_for_log(str(repo_root))
        push_result = safe_push(repo_root, branch)
        if push_result.ok:
            logger.info(
                "Retry push succeeded for %s (%s)",
                safe_session_id,
                safe_repo_root,
            )
            retried += 1
        else:
            logger.warning(
                "Retry push still failing for %s: %s",
                safe_session_id,
                _sanitise_for_log(push_result.stderr),
            )
            remaining.append(line)

    # Rewrite file with only the entries that still failed
    try:
        if remaining:
            path.write_text("\n".join(remaining) + "\n")
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to update pending pushes file: %s", exc)

    return retried


# ---------------------------------------------------------------------------
# Trace finalization
# ---------------------------------------------------------------------------


def finalize_agent_trace(
    session: AgentSession,
    traces: dict[str, AgentTrace],
    trace_store: TraceStore,
) -> None:
    """Write the finalized trace for a reaped session."""
    trace = traces.pop(session.id, None)
    if trace is not None:
        outcome = "success" if session.status != "dead" else "unknown"
        finalize_trace(trace, outcome)
        try:
            trace_store.write(trace)
        except Exception as exc:
            logger.warning("Failed to write finalized trace for %s: %s", session.id, exc)


def update_trace_outcome(
    session_id: str,
    outcome: str,
    traces: dict[str, AgentTrace],
    trace_store: TraceStore,
) -> None:
    """Update the stored trace outcome for a session.

    Called by the orchestrator when it learns a task succeeded or failed
    via the task server (before the process is reaped).

    Args:
        session_id: The session whose trace should be updated.
        outcome: "success" or "failed".
        traces: Mutable traces dict.
        trace_store: TraceStore for persistence.
    """
    trace = traces.get(session_id)
    if trace is None:
        return
    if outcome in ("success", "failed", "unknown"):
        trace.outcome = outcome  # type: ignore[assignment]
        try:
            trace_store.write(trace)
        except Exception as exc:
            logger.warning("Failed to update trace outcome for %s: %s", session_id, exc)


# ---------------------------------------------------------------------------
# Merge worktree branch
# ---------------------------------------------------------------------------


def merge_worktree_branch(
    session_id: str,
    workdir: Path,
    repo_root: Path | None = None,
) -> MergeResult:
    """Merge the agent's worktree branch with conflict detection.

    Uses ``merge_with_conflict_detection`` for a safe, abort-on-conflict
    merge.  On success the branch is merged; on conflict the merge is
    aborted and the caller receives the list of conflicting files.

    Args:
        session_id: The session whose branch should be merged.
        workdir: Project working directory (fallback merge root).
        repo_root: Optional explicit repo root for the merge.

    Returns:
        MergeResult with success status and any conflicting files.
    """
    branch_name = f"agent/{session_id}"
    merge_root = (repo_root or workdir).resolve()
    try:
        result = merge_with_conflict_detection(
            merge_root,
            branch_name,
            message=f"Merge {branch_name}",
        )
        if result.success:
            logger.info("Merged worktree branch %s into current branch", branch_name)
        elif result.conflicting_files:
            logger.warning(
                "Merge conflicts for %s in files: %s",
                session_id,
                ", ".join(result.conflicting_files),
            )
        else:
            logger.warning("Merge failed for %s: %s", session_id, result.error)
        return result
    except Exception as exc:
        logger.warning("Merge failed for %s: %s", session_id, exc)
        return MergeResult(success=False, conflicting_files=[], error=str(exc))


# ---------------------------------------------------------------------------
# Reap helpers
# ---------------------------------------------------------------------------


def reap_openclaw(
    session: AgentSession,
    runtime_bridge: Any,
    run_bridge_call_fn: Any,
) -> None:
    """Sync logs from the remote bridge for an OpenClaw session."""
    from bernstein.bridges.base import BridgeError

    if runtime_bridge is not None:
        try:
            run_bridge_call_fn(runtime_bridge.logs(session.id))
        except BridgeError as exc:
            logger.warning("OpenClaw log sync failed for %s: %s", session.id, exc)
    logger.info("Agent %s remote bridge run finalized", session.id)


def reap_container(
    session: AgentSession,
    container_mgr: ContainerManager | None,
    sandbox_managers: dict[str, ContainerManager],
) -> None:
    """Destroy the container for a containerized agent session."""
    mgr: ContainerManager | None = sandbox_managers.get(session.id, container_mgr)
    if session.container_id and mgr is not None:
        handle = mgr.get_handle(session.id)
        if handle is not None:
            mgr.destroy(handle)
        sandbox_managers.pop(session.id, None)
        logger.info("Agent %s container destroyed", session.id)


def reap_in_process(
    session: AgentSession,
    in_process: InProcessAgent | None,
    backend: AgentBackend,
) -> bool:
    """Wait on and clean up an in-process agent. Returns True if reaped."""
    if in_process is None or backend != AgentBackend.IN_PROCESS:
        return False
    exit_code_val = in_process.wait(session.id, timeout=5.0)
    if exit_code_val is not None:
        session.exit_code = exit_code_val
    in_process.cleanup(session.id)
    logger.info("Agent %s in-process agent cleaned up", session.id)
    return True


def reap_subprocess(
    session: AgentSession,
    procs: dict[str, subprocess.Popen[bytes] | None],
) -> None:
    """Terminate and wait on the OS subprocess."""
    proc = procs.pop(session.id, None)
    if proc is not None:
        try:
            proc.terminate()
        except Exception as exc:
            logger.warning("reap_completed_agent: terminate failed for %s: %s", session.id, exc)
        try:
            session.exit_code = proc.wait(timeout=5)
        except Exception as exc:
            logger.warning("reap_completed_agent: wait failed for %s: %s", session.id, exc)
    logger.info("Agent %s process reaped", session.id)


def reap_completed_agent(
    session: AgentSession,
    *,
    skip_merge: bool = False,
    defer_cleanup: bool = False,
    # --- Dependencies (from spawner state) ---
    runtime_bridge: Any,
    run_bridge_call_fn: Any,
    container_mgr: ContainerManager | None,
    sandbox_managers: dict[str, ContainerManager],
    in_process: InProcessAgent | None,
    backend: AgentBackend,
    procs: dict[str, subprocess.Popen[bytes] | None],
    worktree_paths: dict[str, Path],
    worktree_roots: dict[str, Path],
    worktree_managers: dict[Path, WorktreeManager],
    merge_locks: dict[Path, threading.Lock],
    warm_pool_entries: dict[str, PoolSlot],
    warm_pool: WarmPool | None,
    workdir: Path,
    merge_worktree_branch_fn: Any,
    traces: dict[str, AgentTrace],
    trace_store: TraceStore,
) -> MergeResult | None:
    """Terminate and wait on the subprocess for a completed agent.

    Calls proc.terminate() then proc.wait(timeout=5) to reap the OS
    process.  Handles bridge, container, in-process, and subprocess agents.

    Args:
        session: The AgentSession whose underlying process should be reaped.
        skip_merge: When True, skip the worktree merge.
        defer_cleanup: When True, keep the worktree alive after merge.
        (remaining args are spawner state passed through)

    Returns:
        MergeResult when worktrees are enabled and skip_merge is False
        (None otherwise).
    """
    from bernstein.core.agent_ipc import unregister_stdin_pipe

    unregister_stdin_pipe(session.id)

    if session.runtime_backend == "openclaw":
        reap_openclaw(session, runtime_bridge, run_bridge_call_fn)
    else:
        reap_container(session, container_mgr, sandbox_managers)

        if reap_in_process(session, in_process, backend):
            worktree_paths.pop(session.id, None)
            worktree_roots.pop(session.id, None)
        else:
            reap_subprocess(session, procs)
            merge_result = merge_and_cleanup_worktree(
                session,
                skip_merge,
                defer_cleanup=defer_cleanup,
                worktree_paths=worktree_paths,
                worktree_roots=worktree_roots,
                worktree_managers=worktree_managers,
                merge_locks=merge_locks,
                warm_pool_entries=warm_pool_entries,
                warm_pool=warm_pool,
                workdir=workdir,
                merge_worktree_branch_fn=merge_worktree_branch_fn,
            )
            outcome = "completed" if session.status != "dead" else "timed_out"
            get_plugin_manager().fire_agent_reaped(session_id=session.id, role=session.role, outcome=outcome)
            return merge_result

    finalize_agent_trace(session, traces, trace_store)
    merge_result = merge_and_cleanup_worktree(
        session,
        skip_merge,
        defer_cleanup=defer_cleanup,
        worktree_paths=worktree_paths,
        worktree_roots=worktree_roots,
        worktree_managers=worktree_managers,
        merge_locks=merge_locks,
        warm_pool_entries=warm_pool_entries,
        warm_pool=warm_pool,
        workdir=workdir,
        merge_worktree_branch_fn=merge_worktree_branch_fn,
    )
    outcome = "completed" if session.status != "dead" else "timed_out"
    get_plugin_manager().fire_agent_reaped(session_id=session.id, role=session.role, outcome=outcome)
    return merge_result
