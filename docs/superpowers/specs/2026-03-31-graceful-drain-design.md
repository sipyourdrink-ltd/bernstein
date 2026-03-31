# Graceful Drain System — Design Spec

**Date:** 2026-03-31
**Status:** Approved
**Scope:** TUI, CLI, orchestrator backend

## Problem

Bernstein has no graceful shutdown. Both `q` (TUI) and `s` (TUI) send SIGTERM
to all processes immediately. After force-quit: worktrees litter the repo,
agent branches are orphaned, claimed tickets stay claimed, uncommitted work is
lost, and the user has to manually clean up every time.

## Solution

Replace the current quit flow with a 7-phase **drain** that lets agents finish
their current work, auto-commits unsaved changes, spawns an Opus agent to
cherry-pick completed work into main, cleans up all worktrees/branches, updates
ticket statuses, and shows a summary report — all with a live progress overlay
in the TUI.

## User-Facing Behavior

### Keybindings (TUI)

| Key | Action |
|-----|--------|
| `q` | Start drain (progress overlay appears) |
| `Esc` | Cancel drain (only during Phase 1-2, before agents start exiting) |
| `Ctrl+C` | Force kill at any point — minimal cleanup, immediate exit |
| `r` | Hot restart (unchanged) |

`s` (current "Stop All") is removed — `q` replaces it.

### CLI

`bernstein stop` (no flags) runs the same drain flow non-interactively, printing
progress to stdout. `bernstein stop --force` sends SIGKILL immediately (existing
hard-stop behavior, unchanged).

### Progress Overlay (DrainScreen)

A full-screen Textual `Screen` pushed on top of the dashboard. Shows:

```
╔═══════════════════ Shutting Down ════════════════════╗
║                                                       ║
║  Phase: Waiting for agents to save...        [3/7]    ║
║  ▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱  35%  ~45s remaining         ║
║                                                       ║
║  ✓ New task spawning disabled                         ║
║  ✓ SHUTDOWN signal sent to 4 agents                   ║
║  ◉ Waiting: backend-a3f2 (committing...)              ║
║  ◉ Waiting: security-9d1c (running tests...)          ║
║  ✓ backend-7f2a saved and exited                      ║
║  ✓ qa-c1b8 saved and exited                           ║
║                                                       ║
║  [Ctrl+C] Force quit now   [Esc] Cancel (phases 1-2)  ║
╚═══════════════════════════════════════════════════════╝
```

After drain completes:

```
╔═══════════════════ Run Complete ═════════════════════╗
║                                                       ║
║  Tasks: 17 done  ·  3 partial  ·  0 failed            ║
║  Cost: $0.42  ·  Duration: 1h 23m                     ║
║                                                       ║
║  Merge results (Opus):                                ║
║  ✓ 4 branches cherry-picked to main                   ║
║  ⊘ 2 branches skipped (conflicts/incomplete)          ║
║                                                       ║
║  Cleanup:                                             ║
║  ✓ 6 worktrees removed · 6 branches deleted           ║
║  ✓ 3 partial tickets annotated                        ║
║                                                       ║
║  Press any key to exit                                ║
╚═══════════════════════════════════════════════════════╝
```

## Architecture

### New Modules

```
src/bernstein/core/drain.py          — DrainCoordinator (phases 1-6, ~400 lines)
src/bernstein/core/drain_merge.py    — Opus merge spawner (~150 lines)
src/bernstein/cli/drain_screen.py    — Textual Screen overlay (~250 lines)
```

### Modified Modules

```
src/bernstein/cli/dashboard.py       — action_graceful_quit() pushes DrainScreen
src/bernstein/cli/stop_cmd.py        — soft_stop() delegates to DrainCoordinator
src/bernstein/core/server.py         — POST /drain endpoint
```

## Drain Phases

### Phase 1: Freeze (instant)

- POST `/drain` to task server — sets `accepting_claims = False`
- Orchestrator stops claiming new tasks
- **Cancellable:** Esc undoes freeze, returns to dashboard

### Phase 2: Signal (instant)

- Write SHUTDOWN files for all live agents
- Content: `DRAIN: Save all work, commit changes, and exit cleanly`
- Agents read SHUTDOWN on their next poll cycle (10-30s)
- **Cancellable:** Esc removes SHUTDOWN files, undoes freeze

### Phase 3: Wait (up to 120s, configurable)

- Poll every 2 seconds:
  - `process.poll()` — agent still alive?
  - `git -C <worktree> log --oneline -1` — new commits since signal?
- Update overlay per-agent: exited / committing / still running
- If all agents exit before timeout: advance immediately
- On timeout: SIGTERM remaining agents, wait 5s, SIGKILL survivors
- **Not cancellable** — Ctrl+C escalates to force quit

### Phase 4: Auto-commit (5s)

- For each worktree with uncommitted changes (dirty `git status`):
  ```
  git -C <worktree> add -A
  git -C <worktree> commit -m "WIP: auto-save during drain"
  ```
- Build list: `branches_to_evaluate` = all agent/* branches with commits ahead of main

### Phase 5: Merge via Opus (30-120s)

- Skip if no branches have commits ahead of main
- Spawn `claude` directly (not through orchestrator):
  - Model: opus, effort: max
  - Working directory: project root (main branch)
  - Timeout: 120s
- Prompt instructs Opus to:
  1. For each branch: `git log main..{branch}`, `git diff main..{branch} --stat`
  2. Decide MERGE or SKIP per branch
  3. Cherry-pick MERGE branches onto main
  4. Run `uv run ruff check` after each cherry-pick; revert if lint fails
  5. Output JSON report: `[{branch, action, files_changed, reason}]`
- Parse JSON report from agent stdout/log
- If Opus fails (timeout/crash): skip merge, log warning, continue to cleanup

### Phase 6: Cleanup (5s)

1. Remove all worktrees: `git worktree remove --force <path>`
2. Delete all agent/* branches: `git branch -D <branch>`
3. `git worktree prune`
4. Ticket status updates:
   - Task completed + merged → move YAML to `.sdd/backlog/done/`
   - Task completed + merge skipped → move to `done/` anyway
   - Task interrupted (agent killed before completing) → keep in `open/`, append HTML comment: `<!-- PARTIAL: N files committed, see branch reflog -->`
   - Claimed but agent never started → move to `open/`
5. Clean `.sdd/runtime/` (PID files, signals, logs — not config/backlog)
6. Save `session_state.json`

### Phase 7: Report (until keypress)

- Display summary screen (see mockup above)
- Wait for any keypress
- Exit TUI

## Data Types

```python
@dataclass
class DrainPhase:
    number: int          # 1-7
    name: str            # "freeze", "signal", "wait", "commit", "merge", "cleanup", "report"
    status: str          # "pending", "running", "done", "skipped", "failed"
    detail: str          # human-readable progress
    started_at: float    # time.monotonic()
    finished_at: float

@dataclass
class AgentDrainStatus:
    session_id: str
    role: str
    pid: int
    status: str          # "running", "committing", "exited", "killed"
    committed_files: int
    worktree_path: str

@dataclass
class MergeResult:
    branch: str
    action: str          # "merged", "skipped"
    files_changed: int
    reason: str

@dataclass
class DrainReport:
    phases: list[DrainPhase]
    agents: list[AgentDrainStatus]
    merges: list[MergeResult]
    tasks_done: int
    tasks_partial: int
    tasks_failed: int
    worktrees_removed: int
    branches_deleted: int
    total_duration_s: float
    cost_usd: float
```

## Server Endpoint

```
POST /drain
  → Sets accepting_claims = False
  → Returns {"status": "draining", "active_agents": N}

POST /drain/cancel
  → Sets accepting_claims = True
  → Returns {"status": "cancelled"}

GET /drain
  → Returns {"draining": bool, "active_agents": N}
```

## DrainCoordinator API

```python
class DrainCoordinator:
    def __init__(self, workdir: Path, server_url: str, config: DrainConfig) -> None: ...

    async def run(self, callback: Callable[[DrainPhase, list[AgentDrainStatus]], None]) -> DrainReport:
        """Execute all drain phases. Calls callback on each state change for UI updates."""

    async def cancel(self) -> None:
        """Cancel drain (only during phases 1-2)."""

    # Individual phases (called by run())
    async def _phase_freeze(self) -> None: ...
    async def _phase_signal(self) -> None: ...
    async def _phase_wait(self) -> None: ...
    async def _phase_commit(self) -> None: ...
    async def _phase_merge(self) -> DrainReport: ...
    async def _phase_cleanup(self, report: DrainReport) -> None: ...
```

## DrainConfig

```python
@dataclass
class DrainConfig:
    wait_timeout_s: int = 120      # Phase 3 max wait
    merge_timeout_s: int = 120     # Phase 5 Opus timeout
    merge_model: str = "opus"      # Model for merge agent
    merge_effort: str = "max"      # Effort for merge agent
    auto_commit: bool = True       # Phase 4: commit uncommitted work
    auto_merge: bool = True        # Phase 5: run Opus merge
    skip_merge_if_no_branches: bool = True
```

## Edge Cases

1. **No agents running:** Phases 2-3 are instant, skip to cleanup.
2. **Agent already committed and exited:** Phase 3 detects immediately, marks ✓.
3. **Opus merge fails:** Log warning, skip merge, continue cleanup. Report shows "merge skipped (agent error)".
4. **Ctrl+C during merge:** Kill Opus agent, skip remaining merges, proceed to cleanup.
5. **No branches ahead of main:** Phase 5 skipped entirely.
6. **Server unreachable:** Phase 1 freeze via PID signals instead of HTTP. Cleanup still works.
7. **Worktree already removed:** `git worktree remove` errors are caught and ignored.

## Testing

- `tests/unit/test_drain.py` — DrainCoordinator with mocked agents/git
- `tests/unit/test_drain_merge.py` — Opus merge prompt parsing, report parsing
- `tests/unit/test_drain_screen.py` — Textual Screen rendering (snapshot tests optional)
