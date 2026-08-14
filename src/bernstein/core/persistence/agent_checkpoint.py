"""Agent work-in-progress checkpoints for crash recovery.

On each heartbeat, the agent's current state (files modified, last output,
step count) is saved to disk. After a crash, the orchestrator can detect
recoverable tasks (worktree has uncommitted changes) and spawn a new agent
with checkpoint context so work continues instead of restarting.

Issue #3649 — grant-bound recovery
-----------------------------------
A checkpoint is now also an authority record.  At suspend time the checkpoint
captures a hash of the grant that was live: role name, resolved
``allowed_paths`` / ``denied_paths`` (from :func:`get_permissions_for_role`),
``task_id``, ``parent_run_id``, and the chain head at that moment.

At resume time :func:`is_checkpoint_recoverable` re-derives the current grant
from the same inputs and compares hashes **before** any side effect is taken.
A narrowed role, reassigned task, or cancelled parent causes an explicit refusal
naming the bindings the grant hash covers.

A successful resume appends a :class:`ContinuationEntry` to the run journal
binding ``(checkpoint_hash, grant_hash, chain_head_at_suspend,
chain_head_at_resume)`` so a verifier can chain suspend → resume with no
filesystem access.  Absence of the entry means the resume never completed; the
verifier treats absence as a new run, never as evidence of continuity.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bernstein.core.persistence.atomic_write import write_atomic_json
from bernstein.core.security.permissions import AgentPermissions, get_permissions_for_role

_CHECKPOINT_FILENAME = "checkpoint.json"


# ---------------------------------------------------------------------------
# Grant hashing
# ---------------------------------------------------------------------------


def compute_grant_hash(
    role: str,
    permissions: AgentPermissions,
    task_id: str,
    parent_run_id: str,
    chain_head: str,
) -> str:
    """Stable SHA-256 of the grant that was live at suspend time.

    The hash binds: role name, resolved ``allowed_paths`` / ``denied_paths``
    (sorted for stability), ``task_id``, ``parent_run_id``, and the Merkle
    chain head at the moment of suspension.

    Args:
        role: Agent role name (e.g. ``"backend"``).
        permissions: Resolved :class:`AgentPermissions` for the role.
        task_id: Task the agent was executing.
        parent_run_id: Run that owns the task.
        chain_head: Journal Merkle-head hash at suspend time.

    Returns:
        Lowercase hex SHA-256 string.
    """
    payload = json.dumps(
        {
            "role": role,
            "allowed_paths": sorted(permissions.allowed_paths),
            "denied_paths": sorted(permissions.denied_paths),
            "task_id": task_id,
            "parent_run_id": parent_run_id,
            "chain_head_at_suspend": chain_head,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def checkpoint_hash(checkpoint: AgentCheckpoint) -> str:
    """Stable SHA-256 fingerprint of an :class:`AgentCheckpoint`.

    Excludes timing fields (``checkpointed_at``, ``elapsed_seconds``) so
    two logically identical checkpoints written at different wall-clock times
    produce the same digest.  Used by the continuation entry.
    """
    stable = {k: v for k, v in asdict(checkpoint).items() if k not in {"checkpointed_at", "elapsed_seconds"}}
    payload = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# ContinuationEntry — appended to the journal on a successful resume
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContinuationEntry:
    """Authenticated chain link written to the journal on successful resume.

    A verifier that finds this entry can reconstruct the suspend → resume
    arc with no filesystem access: it re-derives the grant hash from current
    configuration and confirms it matches ``grant_hash``.

    Absence of this entry for a given checkpoint means the resumed run never
    completed its first side-effect-free authority check; the verifier treats
    the run as a *new* run, never as a continuation.

    Attributes:
        checkpoint_hash: SHA-256 of the :class:`AgentCheckpoint` at suspend.
        grant_hash: SHA-256 of the grant that was live at suspend time.
        chain_head_at_suspend: Journal Merkle-head at the moment of suspension.
        chain_head_at_resume: Journal Merkle-head at the moment of resumption.
        resumed_at: Unix timestamp of resumption.
    """

    checkpoint_hash: str
    grant_hash: str
    chain_head_at_suspend: str
    chain_head_at_resume: str
    resumed_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# AgentCheckpoint dataclass
# ---------------------------------------------------------------------------


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
        role: Agent role name at suspend time (e.g. ``"backend"``).
        grant_hash: SHA-256 of the live grant at suspend time.
        parent_run_id: Run that owns the task.
        chain_head_at_suspend: Journal Merkle-head at the moment of suspension.
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
    # --- grant fields (issue #3649) ---
    role: str = ""
    grant_hash: str = ""
    parent_run_id: str = ""
    chain_head_at_suspend: str = ""


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


def find_checkpoint_for_task(task_id: str, runtime_dir: Path) -> AgentCheckpoint | None:
    """Find the checkpoint for ``task_id`` across all agent directories.

    Checkpoints are stored per **agent** (``agents/{agent_id}/checkpoint.json``)
    while resume is driven by **task**, so callers resolving a task must scan
    rather than key by id — looking up ``agents/{task_id}/`` would silently
    miss every real checkpoint. Returns the newest match (``checkpointed_at``)
    when several agents checkpointed the same task; ``None`` when none did.
    """
    agents_dir = runtime_dir / "agents"
    if not agents_dir.is_dir():
        return None
    best: AgentCheckpoint | None = None
    for path in agents_dir.glob(f"*/{_CHECKPOINT_FILENAME}"):
        try:
            candidate = AgentCheckpoint(**json.loads(path.read_text()))
        except (OSError, TypeError, ValueError):
            # A single unreadable checkpoint must not block resume of
            # unrelated tasks; corrupt files surface via scan tooling.
            continue
        if candidate.task_id != task_id:
            continue
        if best is None or candidate.checkpointed_at > best.checkpointed_at:
            best = candidate
    return best


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
    """Return True if ``pid`` refers to a running process.

    Uses the standard POSIX ``os.kill(pid, 0)`` probe. On Windows this can
    raise ``OSError(WinError 87) "The parameter is incorrect"`` for PIDs
    that do not correspond to a live process (instead of the expected
    ``ProcessLookupError``), so we treat any ``OSError`` from ``os.kill``
    on Windows as "not alive". ``PermissionError`` always means the PID
    exists but is not ours; that still counts as alive.
    """
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists, owned by another user - still a live process.
        return True
    except OSError:
        # On Windows, os.kill raises OSError(WinError 87) for dead/invalid
        # PIDs instead of ProcessLookupError. Treat any OSError on Windows
        # as "not alive"; on POSIX, re-raise so genuine bugs surface.
        if sys.platform == "win32":
            return False
        raise
    return True


def is_checkpoint_recoverable(
    checkpoint: AgentCheckpoint,
    *,
    role_overrides: dict[str, AgentPermissions] | None = None,
) -> tuple[bool, str]:
    """Check if a checkpoint can be recovered.

    Performs **both** a liveness check (worktree exists, has uncommitted
    changes) and an **authority check** (issue #3649): the grant that was
    live at suspend time is re-derived from current configuration and its
    hash compared against the stored :attr:`~AgentCheckpoint.grant_hash`.

    The authority check runs **before** the liveness checks so that a
    narrowed or revoked grant is detected before any filesystem side effect
    is taken.

    Args:
        checkpoint: The checkpoint to inspect.
        role_overrides: Optional per-project permission overrides forwarded
            to :func:`get_permissions_for_role`.

    Returns:
        ``(recoverable, reason)``. Recoverable if the grant still holds and
        the worktree has uncommitted changes that can be resumed.
    """
    # --- Authority check (must happen before any side effect) ---
    if checkpoint.grant_hash:
        current_perms = get_permissions_for_role(checkpoint.role, role_overrides)
        expected_hash = compute_grant_hash(
            role=checkpoint.role,
            permissions=current_perms,
            task_id=checkpoint.task_id,
            parent_run_id=checkpoint.parent_run_id,
            chain_head=checkpoint.chain_head_at_suspend,
        )
        if expected_hash != checkpoint.grant_hash:
            # Only the hash of the suspend-time grant is stored, so the
            # refusal cannot prove which single input moved; it names the
            # bindings the hash covers so the operator knows where to look.
            reason = (
                "grant mismatch — resume refused before first side effect; "
                f"role '{checkpoint.role}' permissions narrowed or changed, "
                f"or a grant-bound field no longer matches (task "
                f"'{checkpoint.task_id}', parent run '{checkpoint.parent_run_id}', "
                "chain head at suspend)"
            )
            return False, reason

    # --- Liveness checks (only reached when grant is valid or absent) ---
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


def build_continuation_entry(
    checkpoint: AgentCheckpoint,
    *,
    chain_head_at_resume: str = "",
) -> ContinuationEntry:
    """Build the authenticated journal entry for a successful resume.

    Call this **after** :func:`is_checkpoint_recoverable` returns ``True``
    and **before** dispatching the first agent side effect.  Persist the
    returned entry to the run journal so a verifier can chain suspend →
    resume with no filesystem access.

    Absence of a continuation entry for a checkpoint is always read as
    *new run*, never as evidence of continuity.

    Args:
        checkpoint: The checkpoint that was verified and is now resuming.
        chain_head_at_resume: The journal Merkle-head at resumption time.

    Returns:
        A :class:`ContinuationEntry` ready to append to the journal.
    """
    return ContinuationEntry(
        checkpoint_hash=checkpoint_hash(checkpoint),
        grant_hash=checkpoint.grant_hash,
        chain_head_at_suspend=checkpoint.chain_head_at_suspend,
        chain_head_at_resume=chain_head_at_resume,
    )


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
        f"scratch - build on the existing work.\n\n"
        f"Last output from previous agent:\n"
        f"```\n{checkpoint.last_output[:2000]}\n```\n"
    )
