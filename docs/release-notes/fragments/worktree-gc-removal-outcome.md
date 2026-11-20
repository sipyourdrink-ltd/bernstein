## Worktree GC hooks report directory removal outcomes

When `bernstein worktrees gc` applies a removal, its best-effort `worktree.gc`
hook reports success only when the directory remover succeeds. The audit entry
records the intent before the attempt.
