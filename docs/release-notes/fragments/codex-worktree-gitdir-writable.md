## A codex agent in a Bernstein worktree can commit again

A linked git worktree's `.git` is a file pointing at `<repo>/.git/worktrees/<id>`,
which lies outside the workspace root, so `codex exec --sandbox workspace-write`
refused to create `index.lock` and no codex agent could ever commit: it did the
work, exited 0 with the tree dirty, and the reap-and-merge recorded it as `[WIP]`
while the task was marked failed. The sandboxed spawn now passes the worktree's
git common dir as a writable root. A normal checkout adds nothing, and a spawn
whose vendor sandbox is already dropped by a host-isolation declaration (#5341)
is unchanged.
