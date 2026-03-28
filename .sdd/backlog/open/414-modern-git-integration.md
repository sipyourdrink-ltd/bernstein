# 414 — Modern git integration: branches, PRs, smart commits, context from history

**Role:** architect
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem

Git is used as a dumb append-only log. The current state:

**Commit messages are garbage:**
- 29 consecutive "Auto-evolve: improvements from self-development cycle" commits
- `_evolve_auto_commit` generates messages from file names, not from actual change semantics
- Zero traceability — can't find what changed or why

**No branching:**
- Everything goes straight to master. No isolation, no review, no safety net.
- Agent worktrees create `agent/{session}` branches but merge instantly to master with `--no-ff`
- Evolution changes land on master without review

**No pull before push:**
- Only `git push origin HEAD`. No fetch, no rebase, no divergence detection.
- This caused a 30-vs-29 divergence that required manual conflict resolution.

**Agents are blind to git history:**
- No `git blame` before editing — agents don't know WHY code exists
- No `git bisect` on test failure — just `git checkout -- .` (nuke and retry)
- Partial `git log` in context.py but not used for planning

**Staging is reckless:**
- `git add -A` stages everything including runtime artifacts
- Then `git reset HEAD -- .sdd/runtime/` to unstage. Backwards.

## Implementation

### 1. Branching Strategy (`src/bernstein/core/git_ops.py` — new module)

Central git operations module replacing scattered subprocess calls:

```
master (protected)
  ├── evolve/<cycle-id>      ← each evolution cycle
  ├── task/<task-ids>         ← each task batch
  └── agent/<session-id>     ← each agent (already exists via worktree)
```

Rules:
- **master is merge-only.** No direct commits. No `git push origin HEAD` to master.
- **Each evolve cycle** creates `evolve/{cycle_number}` branch, does work, runs tests. If tests pass → fast-forward merge to master. If tests fail → branch stays open for inspection.
- **Each task batch** works on `task/{task_id_short}` branch (or `agent/{session}` via worktree). On completion + janitor pass → merge to master via `--no-ff` with a meaningful merge commit.
- **Auto-cleanup:** branches older than 24h with no open work get pruned.

### 2. Conventional Commits (`src/bernstein/core/git_ops.py`)

Generate commit messages from diff analysis, not file names:

```
feat(evolution): add EWMA anomaly detection to metrics aggregator

- Add exponentially weighted moving average with λ=0.2
- Integrate CUSUM change-point detection for trend shifts
- Wire into MetricsAggregator.analyze() pipeline

Refs: #106
```

Implementation:
- Parse `git diff --cached --stat` for scope detection (which module/directory changed most)
- Parse `git diff --cached` to classify change type:
  - New files/classes/functions → `feat`
  - Modified existing logic → `refactor` or `fix` (check if tests were failing)
  - Test files only → `test`
  - Config/docs only → `chore` or `docs`
- Body: summarize key changes (3-5 bullets max) from diff hunks
- Footer: `Refs: #task_id` from the task being worked on
- Co-Authored-By trailer: `Co-Authored-By: bernstein[bot] <noreply@bernstein.dev>`
- **No LLM call for commit messages.** Parse the diff deterministically. If the orchestrator has a task title, use it as the description. If evolve mode, summarize from file-level changes.

### 3. Pull-Before-Push (`src/bernstein/core/git_ops.py`)

Before any push:
```python
def safe_push(branch: str) -> bool:
    # 1. Fetch remote
    run(["git", "fetch", "origin"])
    # 2. Check divergence
    behind = run(["git", "rev-list", "--count", f"HEAD..origin/{branch}"])
    if int(behind) > 0:
        # 3. Rebase local on top of remote
        result = run(["git", "rebase", f"origin/{branch}"])
        if result.returncode != 0:
            run(["git", "rebase", "--abort"])
            # Fall back to merge
            run(["git", "merge", f"origin/{branch}", "--no-edit"])
    # 4. Push
    run(["git", "push", "origin", branch])
```

### 4. Git Context for Agents (`src/bernstein/core/git_context.py` — new)

Before spawning an agent, gather git intelligence for the owned files:

**a) Blame context** — why does this code exist?
```python
def blame_summary(file_path: str, line_range: tuple[int, int] | None = None) -> str:
    """Get recent authors and commit messages for key sections of a file."""
    # git blame --line-porcelain <file> | parse authors + messages
    # Summarize: "Last 5 changes: refactored auth flow (2d ago), fixed token expiry (5d ago)"
```

**b) Hot files detection** — which files are unstable?
```python
def hot_files(days: int = 14) -> list[tuple[str, int]]:
    """Files with most commits in the last N days. Unstable = needs more care."""
    # git log --since=14.days --name-only --pretty=format: | sort | uniq -c | sort -rn
```

**c) Co-change graph** — what files change together?
```python
def cochange_files(file_path: str, depth: int = 20) -> list[str]:
    """Files that frequently change in the same commits as file_path."""
    # git log --follow --name-only -n {depth} -- {file_path} | parse co-occurring files
```
(Already partially in context.py — extract and strengthen.)

**d) Recent changes context** — what happened recently?
```python
def recent_changes(file_path: str, n: int = 5) -> list[dict]:
    """Last N commits touching this file with message + diff stat."""
    # git log --follow -n {n} --pretty=format:"%h %s" -- {file_path}
```

Inject into agent prompt as warm context:
```
### Git Context (auto-generated)
#### File history for src/bernstein/evolution/loop.py:
- 2d ago: feat(evolution): add EWMA anomaly detection (#106)
- 5d ago: fix(evolution): prevent empty proposals on idle cycles
- Hot file: 8 commits in 14 days (top 5% by churn)
- Co-changes with: detector.py (80%), proposals.py (60%), aggregator.py (40%)
```

### 5. Git Bisect on Test Failure (`src/bernstein/core/git_ops.py`)

When tests fail after an agent's work:
```python
def bisect_regression(test_cmd: str, good_ref: str = "HEAD~10") -> str | None:
    """Find which commit introduced a test regression."""
    # git bisect start HEAD {good_ref}
    # git bisect run {test_cmd}
    # Return the first bad commit hash
    # git bisect reset
```

Use in janitor: if a task fails verification and the agent made multiple commits, bisect to find which commit broke it. Report the specific bad commit in the failure summary.

### 6. Explicit Staging (`src/bernstein/core/git_ops.py`)

Replace `git add -A` with explicit staging:
```python
def stage_task_files(task_ids: list[str], owned_files: list[str]) -> list[str]:
    """Stage only files owned by the task. Return list of staged paths."""
    # git add <owned_files>
    # Also stage new files in the same directories (tests, etc.)
    # NEVER stage: .sdd/runtime/*, .env, *.pid, *.log
```

### 7. Tags and Milestones

- After each successful evolve cycle that improves metrics: `git tag evolve-{cycle}-{date}`
- On significant feature completion: auto-tag `v0.x.y` based on conventional commit analysis (feat = minor, fix = patch)
- `bernstein version` CLI to show current version from tags

### 8. Migration

Replace all scattered `subprocess.run(["git", ...])` calls across:
- `orchestrator.py` (auto-commit, push)
- `upgrade_executor.py` (commit, revert)
- `spawner.py` (worktree merge)
- `sandbox.py` (worktree create/cleanup)
- `context.py` (git log, git ls-files)
- `worktree.py` (worktree management)

All go through `git_ops.py` and `git_context.py`. Single source of truth for git operations.

## Files
- src/bernstein/core/git_ops.py (new) — all git write operations
- src/bernstein/core/git_context.py (new) — all git read operations for agent context
- src/bernstein/core/orchestrator.py — replace inline git calls
- src/bernstein/core/upgrade_executor.py — replace inline git calls
- src/bernstein/core/spawner.py — replace inline git calls
- src/bernstein/core/worktree.py — delegate to git_ops
- src/bernstein/evolution/sandbox.py — delegate to git_ops
- src/bernstein/core/context.py — delegate to git_context
- tests/unit/test_git_ops.py (new)
- tests/unit/test_git_context.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_git_ops.py -x -q
- test_passes: uv run pytest tests/unit/test_git_context.py -x -q
- file_contains: src/bernstein/core/git_ops.py :: conventional_commit
- file_contains: src/bernstein/core/git_ops.py :: safe_push
- file_contains: src/bernstein/core/git_ops.py :: bisect_regression
- file_contains: src/bernstein/core/git_context.py :: blame_summary
- file_contains: src/bernstein/core/git_context.py :: hot_files
- grep_absent: src/bernstein/core/orchestrator.py :: subprocess.run.*git
