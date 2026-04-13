"""Graceful drain coordinator — 6-phase shutdown with work preservation.

Executes a controlled shutdown sequence: freeze task assignment, signal agents,
wait for clean exits, auto-commit unsaved work, merge completed branches via
an Opus agent, and clean up worktrees/branches/tickets.

Usage::

    from bernstein.core.orchestration.drain import DrainCoordinator, DrainConfig

    coordinator = DrainCoordinator(workdir=Path("."), config=DrainConfig())
    report = await coordinator.run(callback=my_ui_callback)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.orchestration.process_utils import is_process_alive
from bernstein.core.platform_compat import kill_process, kill_process_group
from bernstein.core.runtime_state import read_supervisor_state

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DrainConfig:
    """Configuration for the drain coordinator.

    Attributes:
        wait_timeout_s: Maximum seconds to wait for agents in phase 3.
        merge_timeout_s: Timeout for the Opus merge agent in phase 5.
        merge_model: Model to use for the merge agent.
        merge_effort: Effort level for the merge agent.
        auto_commit: Whether to auto-commit dirty worktrees in phase 4.
        auto_merge: Whether to run the Opus merge agent in phase 5.
    """

    wait_timeout_s: int = 120
    merge_timeout_s: int = 120
    merge_model: str = "opus"
    merge_effort: str = "max"
    auto_commit: bool = True
    auto_merge: bool = True


@dataclass
class DrainPhase:
    """Status of a single drain phase.

    Attributes:
        number: Phase number (1-6).
        name: Machine name (freeze, signal, wait, commit, merge, cleanup).
        status: Current status (pending, running, done, skipped, failed).
        detail: Human-readable progress text.
        started_at: Monotonic timestamp when the phase started.
        finished_at: Monotonic timestamp when the phase finished.
    """

    number: int
    name: str
    status: str
    detail: str
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class AgentDrainStatus:
    """Drain-time status of a single agent.

    Attributes:
        session_id: Agent session identifier.
        role: Agent role (backend, qa, security, etc.).
        pid: OS process ID.
        status: Current status (running, committing, exited, killed).
        committed_files: Number of files committed during drain.
        worktree_path: Filesystem path to the agent's worktree.
    """

    session_id: str
    role: str
    pid: int
    status: str
    committed_files: int = 0
    worktree_path: str = ""


@dataclass(frozen=True)
class MergeResult:
    """Result of merging a single branch.

    Attributes:
        branch: Branch name.
        action: What happened (merged, skipped).
        files_changed: Number of files affected.
        reason: Human-readable explanation.
    """

    branch: str
    action: str
    files_changed: int
    reason: str


@dataclass
class DrainReport:
    """Final report produced after the drain completes.

    Attributes:
        phases: Status of each drain phase.
        agents: Final status of each agent.
        merges: Merge result per branch.
        tasks_done: Number of tasks that completed successfully.
        tasks_partial: Number of tasks with partial progress.
        tasks_failed: Number of tasks that failed.
        worktrees_removed: Number of worktrees cleaned up.
        branches_deleted: Number of branches deleted.
        total_duration_s: Wall-clock duration of the entire drain.
        cost_usd: Estimated cost of the drain (merge agent tokens, etc.).
    """

    phases: list[DrainPhase] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    agents: list[AgentDrainStatus] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    merges: list[MergeResult] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    tasks_done: int = 0
    tasks_partial: int = 0
    tasks_failed: int = 0
    worktrees_removed: int = 0
    branches_deleted: int = 0
    total_duration_s: float = 0.0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POLL_INTERVAL_S = 2
_SIGTERM_GRACE_S = 5


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command, returning the CompletedProcess."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _rmtree_windows_safe(path: Path, max_attempts: int = 3) -> bool:
    """Remove a directory tree with Windows file-lock handling.

    On Windows, files may be locked by processes that haven't fully exited,
    antivirus scanning, or editor file watchers. This function retries with
    delays and uses a permission-override handler as a last resort.

    Args:
        path: Directory to remove.
        max_attempts: Number of retry attempts on Windows (default 3).

    Returns:
        True if the directory was removed, False otherwise.
    """
    if not path.exists():
        return True

    def _onerror(func: Callable[[str], object], fpath: str, exc_info: object) -> None:
        """Handle permission errors by making file writable and retrying."""
        try:
            # Intentional: clear read-only flag on internal worktree files
            # during cleanup so shutil.rmtree can delete them (Windows).
            os.chmod(fpath, stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            func(fpath)
        except OSError:
            pass  # Give up on this file

    attempts = max_attempts if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onerror=_onerror)
            return True
        except OSError as exc:
            if attempt < attempts - 1:
                # Wait for file locks to release (antivirus, processes exiting)
                time.sleep(1.0)
                logger.debug("Retry %d/%d removing %s: %s", attempt + 1, attempts, path, exc)
            else:
                logger.warning("Failed to remove %s after %d attempts: %s", path, attempts, exc)
                return False
    return False


def _is_process_alive(pid: int) -> bool:
    """Return True if *pid* is still running."""
    return is_process_alive(pid)


def _send_signal(pid: int, sig: int) -> None:
    """Send *sig* to *pid*, ignoring errors if the process is already gone."""
    kill_process(pid, sig)


# ---------------------------------------------------------------------------
# DrainCoordinator
# ---------------------------------------------------------------------------


class DrainCoordinator:
    """Orchestrates a 6-phase graceful shutdown.

    Args:
        workdir: Project root directory (contains ``.sdd/``).
        server_url: Base URL of the Bernstein task server.
        config: Drain configuration; uses defaults when ``None``.
    """

    def __init__(
        self,
        workdir: Path,
        server_url: str = "http://127.0.0.1:8052",
        config: DrainConfig | None = None,
    ) -> None:
        self._workdir = workdir
        self._server_url = server_url
        self._config = config or DrainConfig()

        self._phases = self._build_phases()
        self._agents: list[AgentDrainStatus] = []
        self._merges: list[MergeResult] = []
        self._branches_ahead: list[str] = []
        self._cancelled = False
        self._current_phase = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def _execute_phase(
        self,
        phase: DrainPhase,
        method: Callable[[], Coroutine[None, None, None]],
        callback: Callable[[DrainPhase, list[AgentDrainStatus]], None] | None,
    ) -> None:
        """Execute a single drain phase with status tracking and error handling."""
        self._current_phase = phase.number
        phase.status = "running"
        phase.started_at = time.monotonic()
        if callback is not None:
            callback(phase, self._agents)
        try:
            await method()
            if phase.status == "running":
                phase.status = "done"
        except Exception:
            phase.status = "failed"
            logger.exception("Phase %d (%s) failed", phase.number, phase.name)
        finally:
            phase.finished_at = time.monotonic()
            if callback is not None:
                callback(phase, self._agents)

    async def run(
        self,
        callback: Callable[[DrainPhase, list[AgentDrainStatus]], None] | None = None,
    ) -> DrainReport:
        """Execute all drain phases sequentially.

        Args:
            callback: Called on state changes so the UI can update.

        Returns:
            A ``DrainReport`` summarising everything that happened.
        """
        start = time.monotonic()
        self._callback = callback
        report = DrainReport(phases=self._phases, agents=self._agents)

        phase_methods: list[Callable[[], Coroutine[None, None, None]]] = [
            self._phase_freeze,
            self._phase_signal,
            self._phase_wait,
            self._phase_commit,
            self._phase_merge,
            self._phase_cleanup,
        ]

        for idx, method in enumerate(phase_methods):
            if self._cancelled:
                break
            await self._execute_phase(self._phases[idx], method, callback)

        report.agents = self._agents
        report.merges = self._merges
        report.total_duration_s = time.monotonic() - start

        # Tally task outcomes.
        for agent in self._agents:
            if agent.status == "exited" and agent.committed_files > 0:
                report.tasks_done += 1
            elif agent.status in ("exited", "killed") and agent.committed_files == 0:
                report.tasks_partial += 1

        return report

    async def cancel(self) -> None:
        """Cancel the drain.

        Only effective during phases 1-2 (freeze/signal).  Later phases
        cannot be cancelled — use Ctrl+C to force-quit instead.
        """
        if not self.cancellable:
            logger.warning("Cannot cancel drain during phase %d", self._current_phase)
            return
        self._cancelled = True
        logger.info("Drain cancelled during phase %d", self._current_phase)

        # Undo freeze: tell server to resume.
        async with httpx.AsyncClient(timeout=5) as client:
            with contextlib.suppress(httpx.HTTPError):
                await client.post(f"{self._server_url}/drain/cancel")

        # Remove SHUTDOWN signals we may have written.
        signals_dir = self._workdir / ".sdd" / "runtime" / "signals"
        if signals_dir.exists():
            for child in signals_dir.iterdir():
                shutdown_file = child / "SHUTDOWN"
                if shutdown_file.exists():
                    shutdown_file.unlink(missing_ok=True)

        # Remove draining flag file.
        draining_flag = self._workdir / ".sdd" / "runtime" / "draining"
        draining_flag.unlink(missing_ok=True)

    @property
    def cancellable(self) -> bool:
        """True if the current phase allows cancellation."""
        return self._current_phase <= 2

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    async def _phase_freeze(self) -> None:
        """Phase 1: Freeze — disable new task assignment."""
        phase = self._phases[0]
        phase.detail = "Disabling new task assignment"

        async with httpx.AsyncClient(timeout=5) as client:
            try:
                resp = await client.post(f"{self._server_url}/drain")
                resp.raise_for_status()
                logger.info("Task server set to draining mode")
            except httpx.HTTPError:
                # Server unreachable — fall back to flag file.
                flag = self._workdir / ".sdd" / "runtime" / "draining"
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text("draining", encoding="utf-8")
                logger.info("Task server already stopped; wrote draining flag file")

        phase.detail = "New task spawning disabled"

    async def _phase_signal(self) -> None:
        """Phase 2: Signal — write SHUTDOWN to all live agents.

        Discovers agents from multiple sources (HTTP API, PID files, worktrees)
        since agents.json may not exist.
        """
        phase = self._phases[1]

        # Source 1: HTTP API — most reliable when server is running.
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._server_url}/status")
                if resp.status_code == 200:
                    status_data = resp.json()
                    claimed = int(status_data.get("claimed", 0))
                    logger.info("Server reports %d claimed tasks", claimed)
        except Exception:
            pass

        # Source 2: PID files in .sdd/runtime/pids/.
        pids_dir = self._workdir / ".sdd" / "runtime" / "pids"
        if pids_dir.is_dir():
            for pid_file in pids_dir.glob("*.json"):
                try:
                    meta = json.loads(pid_file.read_text(encoding="utf-8"))
                    session_id = str(meta.get("session_id", pid_file.stem))
                    role = str(meta.get("role", "unknown"))
                    pid = int(meta.get("pid", 0))
                    if pid and _is_process_alive(pid):
                        already = any(a.session_id == session_id for a in self._agents)
                        if not already:
                            wt = self._workdir / ".sdd" / "worktrees" / session_id
                            self._agents.append(
                                AgentDrainStatus(
                                    session_id=session_id,
                                    role=role,
                                    pid=pid,
                                    status="running",
                                    worktree_path=str(wt) if wt.exists() else "",
                                )
                            )
                except (OSError, ValueError):
                    continue

        # Source 3: agents.json (legacy fallback).
        agents_file = self._workdir / ".sdd" / "runtime" / "agents.json"
        if agents_file.exists():
            try:
                raw = json.loads(agents_file.read_text(encoding="utf-8"))
                agents_data = cast("list[dict[str, object]]", raw) if isinstance(raw, list) else []
                for entry in agents_data:
                    session_id = str(entry.get("session_id", entry.get("id", "")))
                    if any(a.session_id == session_id for a in self._agents):
                        continue
                    role = str(entry.get("role", "unknown"))
                    raw_pid = entry.get("pid", 0)
                    pid = int(raw_pid) if isinstance(raw_pid, (int, str, float)) else 0
                    worktree = str(entry.get("worktree_path", ""))
                    if session_id and pid:
                        self._agents.append(
                            AgentDrainStatus(
                                session_id=session_id,
                                role=role,
                                pid=pid,
                                status="running",
                                worktree_path=worktree,
                            )
                        )
            except (json.JSONDecodeError, OSError):
                pass

        # Source 4: scan running processes for orphan agents working in this repo.
        # This catches agents that lost their PID file or were spawned externally.
        my_pid = os.getpid()
        workdir_str = str(self._workdir)
        known_pids = {a.pid for a in self._agents}
        try:
            ps_proc = await asyncio.create_subprocess_exec(
                "ps",
                "-ax",
                "-o",
                "pid=,command=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            ps_stdout, _ = await asyncio.wait_for(ps_proc.communicate(), timeout=5)
            if ps_proc.returncode == 0:
                for line in ps_stdout.decode().splitlines():
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) != 2:
                        continue
                    try:
                        pid = int(parts[0])
                    except ValueError:
                        continue
                    if pid == my_pid or pid in known_pids:
                        continue
                    cmd = parts[1]
                    # Match agents working in this repo's worktrees
                    if workdir_str in cmd and (
                        ".sdd/worktrees" in cmd or ".claude/worktrees" in cmd or ".sdd/runtime/heartbeats" in cmd
                    ):
                        self._agents.append(
                            AgentDrainStatus(
                                session_id=f"orphan-{pid}",
                                role="unknown",
                                pid=pid,
                                status="running",
                                worktree_path="",
                            )
                        )
        except OSError:
            pass

        # Write SHUTDOWN signals for discovered agents.
        for agent in self._agents:
            signal_dir = self._workdir / ".sdd" / "runtime" / "signals" / agent.session_id
            signal_dir.mkdir(parents=True, exist_ok=True)
            (signal_dir / "SHUTDOWN").write_text(
                "DRAIN: Save all work, commit changes, and exit cleanly",
                encoding="utf-8",
            )

        count = len(self._agents)
        phase.detail = f"SHUTDOWN sent to {count} agent{'s' if count != 1 else ''}"
        logger.info("Sent SHUTDOWN signal to %d agents", count)

    async def _phase_wait(self) -> None:
        """Phase 3: Wait — poll agents until they exit or timeout."""
        phase = self._phases[2]

        if not self._agents:
            phase.detail = "No agents to wait for"
            return

        deadline = time.monotonic() + self._config.wait_timeout_s
        remaining = [a for a in self._agents if a.status == "running"]

        while remaining and time.monotonic() < deadline:
            for agent in remaining:
                if not _is_process_alive(agent.pid):
                    agent.status = "exited"
                    logger.info("Agent %s (pid %d) exited cleanly", agent.session_id, agent.pid)
                    continue

                # Check worktree for recent commits.
                if agent.worktree_path:
                    wt = Path(agent.worktree_path)
                    if wt.exists():
                        result = _run_git(["status", "--porcelain"], cwd=wt)
                        if result.returncode == 0 and not result.stdout.strip():
                            agent.status = "committing"

            remaining = [a for a in self._agents if a.status == "running"]
            exited = [a for a in self._agents if a.status == "exited"]
            elapsed = self._config.wait_timeout_s - (deadline - time.monotonic())
            phase.detail = (
                f"{len(exited)} exited, {len(remaining)} waiting ({int(elapsed)}s/{self._config.wait_timeout_s}s)"
            )
            # Update UI each poll so the user sees live progress
            if self._callback is not None:
                self._callback(phase, self._agents)
            if remaining:
                await asyncio.sleep(_POLL_INTERVAL_S)

        # Timeout: escalate remaining agents.
        still_alive = [a for a in self._agents if a.status in ("running", "committing")]
        if still_alive:
            logger.warning("Timeout: sending SIGTERM to %d remaining agents", len(still_alive))
            for agent in still_alive:
                _send_signal(agent.pid, signal.SIGTERM)

            await asyncio.sleep(_SIGTERM_GRACE_S)

            for agent in still_alive:
                if _is_process_alive(agent.pid):
                    _send_signal(agent.pid, signal.SIGKILL)
                    agent.status = "killed"
                    logger.warning(
                        "Agent %s (pid %d) killed after timeout",
                        agent.session_id,
                        agent.pid,
                    )
                else:
                    agent.status = "exited"

        exited = sum(1 for a in self._agents if a.status == "exited")
        killed = sum(1 for a in self._agents if a.status == "killed")
        phase.detail = f"{exited} exited, {killed} killed"

    @staticmethod
    def _try_commit_worktree(agent: AgentDrainStatus) -> bool:
        """Attempt to stage and commit dirty files in an agent worktree.

        Returns True if a commit was made, False otherwise.
        """
        if not agent.worktree_path:
            return False
        wt = Path(agent.worktree_path)
        if not wt.exists():
            return False

        status_result = _run_git(["status", "--porcelain"], cwd=wt)
        if status_result.returncode != 0 or not status_result.stdout.strip():
            return False

        add_result = _run_git(["add", "-A"], cwd=wt)
        if add_result.returncode != 0:
            logger.warning("git add failed in %s: %s", wt, add_result.stderr.strip())
            return False

        commit_result = _run_git(["commit", "-m", "chore(drain): auto-save during drain"], cwd=wt)
        if commit_result.returncode != 0:
            logger.warning("git commit failed in %s: %s", wt, commit_result.stderr.strip())
            return False

        diff_result = _run_git(["diff", "--name-only", "HEAD~1..HEAD"], cwd=wt)
        file_count = len(diff_result.stdout.strip().splitlines()) if diff_result.returncode == 0 else 0
        agent.committed_files = file_count
        logger.info("Auto-committed %d files in %s", file_count, wt)
        return True

    def _detect_branches_ahead(self) -> None:
        """Detect agent worktree branches that are ahead of main."""
        for agent in self._agents:
            if not agent.worktree_path:
                continue
            wt = Path(agent.worktree_path)
            if not wt.exists():
                continue
            branch_result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
            if branch_result.returncode != 0:
                continue
            branch = branch_result.stdout.strip()
            if not branch or branch == "main":
                continue
            log_result = _run_git(["log", f"main..{branch}", "--oneline"], cwd=self._workdir)
            if log_result.returncode == 0 and log_result.stdout.strip():
                self._branches_ahead.append(branch)

    async def _phase_commit(self) -> None:
        """Phase 4: Commit — auto-save dirty worktrees."""
        await asyncio.sleep(0)  # Async interface requirement
        phase = self._phases[3]

        if not self._config.auto_commit:
            phase.status = "skipped"
            phase.detail = "Auto-commit disabled"
            return

        committed = sum(1 for agent in self._agents if self._try_commit_worktree(agent))
        self._detect_branches_ahead()

        phase.detail = (
            f"{committed} worktree{'s' if committed != 1 else ''} committed, "
            f"{len(self._branches_ahead)} branch"
            f"{'es' if len(self._branches_ahead) != 1 else ''} ahead of main"
        )

    async def _phase_merge(self) -> None:
        """Phase 5: Merge — spawn an Opus agent to cherry-pick branches."""
        phase = self._phases[4]

        if not self._branches_ahead:
            phase.status = "skipped"
            phase.detail = "No branches ahead of main"
            return

        if not self._config.auto_merge:
            phase.status = "skipped"
            phase.detail = "Auto-merge disabled"
            return

        phase.detail = f"Merging {len(self._branches_ahead)} branches via {self._config.merge_model}"

        try:
            from bernstein.core.orchestration.drain_merge import run_merge_agent  # type: ignore[import-not-found]

            raw_results: list[Any] = await run_merge_agent(
                workdir=self._workdir,
                branches=self._branches_ahead,
                model=self._config.merge_model,
                effort=self._config.merge_effort,
                timeout_s=self._config.merge_timeout_s,
            )
            # Convert to local MergeResult (drain_merge may use its own type).
            for r in raw_results:
                self._merges.append(
                    MergeResult(
                        branch=getattr(r, "branch", ""),
                        action=getattr(r, "action", "skipped"),
                        files_changed=int(getattr(r, "files_changed", 0)),
                        reason=getattr(r, "reason", ""),
                    )
                )
            merged = sum(1 for m in self._merges if m.action == "merged")
            skipped = sum(1 for m in self._merges if m.action == "skipped")
            phase.detail = f"{merged} merged, {skipped} skipped"
        except ImportError:
            phase.status = "skipped"
            phase.detail = "drain_merge module not available"
            logger.warning("drain_merge module not found; skipping merge phase")
        except Exception:
            phase.status = "failed"
            phase.detail = "Merge agent failed"
            logger.exception("Merge agent failed")

    async def _phase_cleanup(self) -> None:
        """Phase 6: Cleanup — remove worktrees, branches, update tickets."""
        phase = self._phases[5]
        worktrees_removed = 0
        branches_deleted = 0

        # Stop long-lived infrastructure first so the watchdog cannot
        # respawn the server/spawner while we are tearing runtime state down.
        await self._stop_infrastructure()

        # Remove ALL agent worktrees from .sdd/worktrees/ and .claude/worktrees/
        # — not just those in self._agents (which may be empty after a restart).
        wt_dirs = [
            self._workdir / ".sdd" / "worktrees",
            self._workdir / ".claude" / "worktrees",
        ]
        for wt_dir in wt_dirs:
            if not wt_dir.is_dir():
                continue
            for entry in sorted(wt_dir.iterdir()):
                if not entry.is_dir():
                    continue
                # On Windows, retry git worktree remove with delays for file locks
                max_git_attempts = 3 if sys.platform == "win32" else 1
                removed = False
                for attempt in range(max_git_attempts):
                    result = _run_git(
                        ["worktree", "remove", "--force", str(entry)],
                        cwd=self._workdir,
                    )
                    if result.returncode == 0:
                        worktrees_removed += 1
                        removed = True
                        break
                    if attempt < max_git_attempts - 1:
                        await asyncio.sleep(1.0)  # Wait for file locks to release

                if not removed:  # noqa: SIM102
                    # Fallback: rm -rf with Windows file-lock handling
                    if _rmtree_windows_safe(entry):
                        worktrees_removed += 1

        # Prune worktree registry BEFORE deleting branches — this
        # unregisters removed worktrees so branch -D succeeds.
        _run_git(["worktree", "prune"], cwd=self._workdir)

        # Delete agent/* branches.
        branch_result = _run_git(["branch", "--list", "agent/*"], cwd=self._workdir)
        if branch_result.returncode == 0:
            for line in branch_result.stdout.strip().splitlines():
                branch_name = line.strip().lstrip("*+ ")
                if not branch_name:
                    continue
                del_result = _run_git(["branch", "-D", branch_name], cwd=self._workdir)
                if del_result.returncode == 0:
                    branches_deleted += 1
                elif "not found" not in del_result.stderr:
                    logger.warning(
                        "Failed to delete branch %s: %s",
                        branch_name,
                        del_result.stderr.strip(),
                    )

        # Move ticket files.
        self._move_tickets()

        # Clean runtime state.
        self._clean_runtime()

        # Cancel drain mode on the server so claims work again on next run.
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{self._server_url}/drain/cancel")
                logger.info("Drain mode cancelled on server")
        except Exception:
            # Server may already be down — that's fine, draining is
            # an in-memory flag that resets on server restart.
            logger.debug("Could not cancel drain on server (may be already stopped)")

        phase.detail = (
            f"{worktrees_removed} worktree{'s' if worktrees_removed != 1 else ''} removed, "
            f"{branches_deleted} branch{'es' if branches_deleted != 1 else ''} deleted"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_phases() -> list[DrainPhase]:
        """Create the initial list of pending phases."""
        names = ["freeze", "signal", "wait", "commit", "merge", "cleanup"]
        return [DrainPhase(number=i + 1, name=name, status="pending", detail="") for i, name in enumerate(names)]

    def _move_tickets(self) -> None:
        """Move ticket YAML files between backlog directories."""
        claimed_dir = self._workdir / ".sdd" / "backlog" / "claimed"
        done_dir = self._workdir / ".sdd" / "backlog" / "done"
        open_dir = self._workdir / ".sdd" / "backlog" / "open"

        if not claimed_dir.exists():
            return

        done_dir.mkdir(parents=True, exist_ok=True)
        open_dir.mkdir(parents=True, exist_ok=True)

        completed_sessions = {a.session_id for a in self._agents if a.status == "exited" and a.committed_files > 0}

        for ticket_path in list(claimed_dir.iterdir()):
            if ticket_path.suffix not in (".yaml", ".yml"):
                continue
            if not ticket_path.exists():
                continue

            try:
                content = ticket_path.read_text(encoding="utf-8")
            except OSError:
                continue

            # Check if any completed agent handled this ticket.
            is_done = any(sid in content for sid in completed_sessions)

            dest = (done_dir if is_done else open_dir) / ticket_path.name

            try:
                shutil.move(str(ticket_path), str(dest))
                logger.info("Moved ticket %s → %s", ticket_path.name, dest.parent.name)
            except FileNotFoundError:
                pass  # already moved by another cleanup path
            except OSError as exc:
                logger.warning("Failed to move ticket %s: %s", ticket_path.name, exc)

    async def _stop_infrastructure(self) -> None:
        """Terminate the watchdog, spawner, and server for this run."""
        await self._terminate_runtime_pid("watchdog.pid", "watchdog")
        await self._terminate_runtime_pid("spawner.pid", "spawner")

        server_pid = self._read_runtime_pid("server.pid")
        if server_pid is None:
            snapshot = read_supervisor_state(self._workdir / ".sdd")
            if snapshot is not None and snapshot.current_pid > 0:
                server_pid = snapshot.current_pid
        # Fallback: find server by port if PID file missing
        if server_pid is None:
            server_pid = self._find_pid_by_port(8052)
            if server_pid:
                logger.debug("Found task server by port scan: PID %d", server_pid)
        await self._terminate_process(server_pid, "task server")

    def _find_pid_by_port(self, port: int) -> int | None:
        """Find PID of process listening on a port (Windows/Unix)."""
        import sys

        try:
            if sys.platform == "win32":
                return self._find_pid_by_port_windows(port)
            return self._find_pid_by_port_unix(port)
        except Exception as exc:
            logger.debug("Port scan for %d failed: %s", port, exc)
        return None

    @staticmethod
    def _find_pid_by_port_windows(port: int) -> int | None:
        """Find PID listening on *port* using Windows netstat."""
        import subprocess

        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" not in line or "LISTENING" not in line:
                continue
            parts = line.split()
            if not parts:
                continue
            try:
                return int(parts[-1])
            except ValueError:
                pass
        return None

    @staticmethod
    def _find_pid_by_port_unix(port: int) -> int | None:
        """Find PID listening on *port* using lsof."""
        import subprocess

        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().split()[0])
        return None

    def _read_runtime_pid(self, filename: str) -> int | None:
        """Read a runtime PID file, returning None for missing or invalid data."""
        pid_path = self._workdir / ".sdd" / "runtime" / filename
        if not pid_path.exists():
            return None
        try:
            return int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None

    async def _terminate_runtime_pid(self, filename: str, label: str) -> None:
        """Terminate a runtime-managed process named by *filename*."""
        await self._terminate_process(self._read_runtime_pid(filename), label)

    async def _terminate_process(self, pid: int | None, label: str) -> None:
        """Terminate *pid* gracefully, escalating to SIGKILL if needed."""
        if pid is None or not _is_process_alive(pid):
            return

        # Try process-group kill first (covers child processes), fall
        # back to single-process kill if the group call fails.
        if not kill_process_group(pid, sig=signal.SIGTERM):
            _send_signal(pid, signal.SIGTERM)

        deadline = time.monotonic() + _SIGTERM_GRACE_S
        while time.monotonic() < deadline:
            if not _is_process_alive(pid):
                logger.info("%s exited during drain cleanup", label)
                return
            await asyncio.sleep(0.05)

        if not kill_process_group(pid, sig=9):
            _send_signal(pid, 9)

        if _is_process_alive(pid):
            logger.warning("%s (pid %d) survived drain cleanup kill", label, pid)

    def _clean_runtime(self) -> None:
        """Remove ephemeral runtime files (agents.json, signals, PIDs)."""
        runtime_dir = self._workdir / ".sdd" / "runtime"
        if not runtime_dir.exists():
            return

        # Remove agents.json.
        agents_file = runtime_dir / "agents.json"
        agents_file.unlink(missing_ok=True)

        # Remove signals directory.
        signals_dir = runtime_dir / "signals"
        if signals_dir.exists():
            shutil.rmtree(signals_dir, ignore_errors=True)

        # Remove PID files.
        for pid_file in runtime_dir.glob("*.pid"):
            pid_file.unlink(missing_ok=True)

        # Remove other shutdown coordination artifacts from the finished run.
        (runtime_dir / "draining").unlink(missing_ok=True)
        (runtime_dir / "supervisor_state.json").unlink(missing_ok=True)
