"""Agent work-in-progress checkpoints for crash recovery.

On each heartbeat, the agent's current state (files modified, last output,
step count) is saved to disk. After a crash, the orchestrator can detect
recoverable tasks (worktree has uncommitted changes) and spawn a new agent
with checkpoint context so work continues instead of restarting.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bernstein.core.persistence.atomic_write import write_atomic_json

_CHECKPOINT_FILENAME = "checkpoint.json"


@dataclass
class AgentCheckpoint:
    """Snapshot of an agent's in-progress work for crash recovery.

    Attributes:
        agent_id: Unique identifier of the agent process.
        task_id: Identifier of the task the agent is working on.
        worktree_path: Filesystem path to the agent's git worktree.
        files_modified: Paths of files the agent has modified so far.
        last_output: Trailing output buffer from the agent.
        step_count: Number of discrete steps the agent has performed.
        elapsed_seconds: Wall-clock seconds the agent has been running.
        checkpointed_at: Unix timestamp when the checkpoint was written.
        crash_recoverable: Whether this checkpoint is eligible for recovery.
    """

    agent_id: str
    task_id: str
    worktree_path: str
    files_modified: list[str] = field(default_factory=list)
    last_output: str = ""
    step_count: int = 0
    elapsed_seconds: float = 0.0
    checkpointed_at: float = field(default_factory=time.time)
    crash_recoverable: bool = True


def save_checkpoint(checkpoint: AgentCheckpoint, runtime_dir: Path) -> Path:
    """Persist checkpoint to ``.sdd/runtime/agents/{agent_id}/checkpoint.json``.

    Args:
        checkpoint: The checkpoint to persist.
        runtime_dir: Root runtime directory (typically ``.sdd/runtime``).

    Returns:
        Path to the written checkpoint file.
    """
    agent_dir = runtime_dir / "agents" / checkpoint.agent_id
    path = agent_dir / _CHECKPOINT_FILENAME
    write_atomic_json(path, asdict(checkpoint), indent=None, sort_keys=True)
    return path


def load_checkpoint(agent_id: str, runtime_dir: Path) -> AgentCheckpoint | None:
    """Load a checkpoint for ``agent_id``; returns None if missing."""
    path = runtime_dir / "agents" / agent_id / _CHECKPOINT_FILENAME
    if not path.exists():
        return None
    return AgentCheckpoint(**json.loads(path.read_text()))


def scan_orphaned_checkpoints(runtime_dir: Path) -> list[AgentCheckpoint]:
    """Find all checkpoints whose owning process is no longer alive.

    Args:
        runtime_dir: Root runtime directory to scan.

    Returns:
        The list of orphaned checkpoints that may be recovered.
    """
    agents_dir = runtime_dir / "agents"
    if not agents_dir.exists():
        return []
    orphans: list[AgentCheckpoint] = []
    for agent_dir in agents_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        checkpoint_path = agent_dir / _CHECKPOINT_FILENAME
        pid_path = agent_dir / "pid"
        if not checkpoint_path.exists():
            continue
        # Check if pid is alive
        pid = _read_pid(pid_path)
        if pid is not None and _pid_alive(pid):
            continue  # still running, not orphaned
        try:
            orphans.append(AgentCheckpoint(**json.loads(checkpoint_path.read_text())))
        except (json.JSONDecodeError, TypeError):
            continue
    return orphans


def _read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        import os

        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def is_checkpoint_recoverable(checkpoint: AgentCheckpoint) -> tuple[bool, str]:
    """Check if a checkpoint can be recovered.

    Args:
        checkpoint: The checkpoint to inspect.

    Returns:
        ``(recoverable, reason)``. Recoverable if the worktree exists and has
        uncommitted changes that can be resumed.
    """
    worktree = Path(checkpoint.worktree_path)
    if not worktree.exists():
        return False, "worktree missing"
    if not (worktree / ".git").exists() and not _is_git_worktree(worktree):
        return False, "not a git worktree"
    # Check for uncommitted changes
    status = _git_status(worktree)
    if status is None:
        return False, "git status failed"
    if not status.strip():
        return False, "worktree is clean"
    return True, "has uncommitted changes"


def _is_git_worktree(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.SubprocessError, OSError):
        return False


def _git_status(worktree: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.SubprocessError, OSError):
        return None


def build_resume_prompt(checkpoint: AgentCheckpoint, original_goal: str) -> str:
    """Build a system-prompt addendum for an agent resuming from checkpoint.

    Args:
        checkpoint: The checkpoint describing prior progress.
        original_goal: The task goal as originally given to the agent.

    Returns:
        A markdown-formatted prompt fragment that instructs the new agent to
        continue from the prior work rather than restart.
    """
    files_summary = ", ".join(checkpoint.files_modified[:10]) or "(none yet)"
    return (
        f"## Resuming from checkpoint\n\n"
        f"You were previously working on this task. Here's what the previous "
        f"agent accomplished before being interrupted:\n\n"
        f"- **Original goal**: {original_goal}\n"
        f"- **Steps taken**: {checkpoint.step_count}\n"
        f"- **Elapsed time**: {checkpoint.elapsed_seconds:.0f}s\n"
        f"- **Files modified**: {files_summary}\n\n"
        f"The files above are already in the worktree. Review them first, then "
        f"continue from where the previous agent left off. Do NOT restart from "
        f"scratch — build on the existing work.\n\n"
        f"Last output from previous agent:\n"
        f"```\n{checkpoint.last_output[:2000]}\n```\n"
    )
