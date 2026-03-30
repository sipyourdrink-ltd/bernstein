# D09 — `bernstein diff` Task Change Viewer

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
After a task completes, users must manually navigate to the worktree and run git commands to see what code the agent changed. There is no convenient way to review agent-produced diffs from the CLI.

## Solution
- Add a `bernstein diff <task-id>` command
- Locate the git worktree associated with the given task ID
- Run `git diff` on the worktree to produce a unified diff of all changes
- Pipe the diff output through a syntax-highlighted renderer (use Rich's `Syntax` widget or `pygments`)
- Support `--stat` flag to show a `git diff --stat` summary view (files changed, insertions, deletions)
- Support `--file <path>` flag to filter the diff to a specific file
- If the worktree has been cleaned up, fall back to reading the stored diff from `.sdd/traces/<task-id>/diff.patch` if available
- Page long diffs through the system pager (`$PAGER` or `less`)

## Acceptance
- [ ] `bernstein diff <task-id>` displays a syntax-highlighted unified diff of agent changes
- [ ] `bernstein diff <task-id> --stat` shows a summary of files changed with insertion/deletion counts
- [ ] `bernstein diff <task-id> --file src/auth.py` filters output to only that file's changes
- [ ] Long diffs are paged through the system pager
- [ ] Running with an invalid task ID prints a clear error message
- [ ] The command works even if the worktree has been cleaned up (falls back to stored diff)
