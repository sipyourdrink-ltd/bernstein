# Graceful Drain — Implementation Plan

**Spec:** `2026-03-31-graceful-drain-design.md`

## Steps

### Step 1: DrainCoordinator backend (drain.py)
**Files:** `src/bernstein/core/drain.py` (new)
**Depends on:** nothing
**Agent:** can run in parallel

Create `DrainCoordinator` class with phases 1-6:
- `DrainConfig` dataclass
- `DrainPhase`, `AgentDrainStatus`, `DrainReport` dataclasses
- `async def run(callback)` — orchestrates all phases
- `async def cancel()` — cancels during phase 1-2
- Phase 1 (freeze): POST /drain or fallback to PID signals
- Phase 2 (signal): write SHUTDOWN files via AgentSignalManager
- Phase 3 (wait): poll loop with 2s interval, process.poll() + git status checks
- Phase 4 (commit): git add -A && git commit for dirty worktrees
- Phase 6 (cleanup): remove worktrees, delete branches, update tickets, clean runtime

Skip Phase 5 (merge) — that's Step 2.

### Step 2: Opus merge agent (drain_merge.py)
**Files:** `src/bernstein/core/drain_merge.py` (new)
**Depends on:** nothing (independent module)
**Agent:** can run in parallel

Create merge module:
- `async def run_merge_agent(branches, workdir, config) -> list[MergeResult]`
- Build the Opus prompt with branch list
- Spawn claude directly via subprocess (not orchestrator)
- Parse JSON report from agent output
- Handle timeout/failure gracefully
- `MergeResult` dataclass

### Step 3: Server drain endpoint
**Files:** `src/bernstein/core/server.py` (modify)
**Depends on:** nothing
**Agent:** can run in parallel

Add endpoints:
- `POST /drain` — set `accepting_claims = False`, return status
- `POST /drain/cancel` — set `accepting_claims = True`
- `GET /drain` — return current drain status
- Add `_draining: bool` state to the server app

### Step 4: DrainScreen TUI overlay (drain_screen.py)
**Files:** `src/bernstein/cli/drain_screen.py` (new)
**Depends on:** Step 1 (uses DrainCoordinator types)
**Agent:** must run after Step 1

Create Textual Screen:
- `DrainScreen(Screen[DrainReport])` 
- Keybindings: Ctrl+C (force quit), Esc (cancel in phase 1-2)
- Layout: phase indicator, progress bar, per-agent status list, footer
- `on_mount()`: start drain via asyncio task
- Callback from DrainCoordinator updates reactive properties
- After Phase 7: show report, wait for keypress, dismiss screen with report

### Step 5: Wire into dashboard + stop_cmd
**Files:** `src/bernstein/cli/dashboard.py` (modify), `src/bernstein/cli/stop_cmd.py` (modify)
**Depends on:** Steps 1, 2, 3, 4

Dashboard changes:
- `action_graceful_quit()`: push DrainScreen instead of killing everything
- Remove `s` binding (or repurpose)
- Handle DrainScreen result (DrainReport)

stop_cmd changes:
- `soft_stop()`: use DrainCoordinator synchronously (asyncio.run)
- Print phase progress to stdout
- Keep `--force` as-is

### Step 6: Tests
**Files:** `tests/unit/test_drain.py`, `tests/unit/test_drain_merge.py` (new)
**Depends on:** Steps 1, 2

Unit tests for DrainCoordinator and merge module.
