## Copied worktree inputs can no longer be committed onto an agent's branch

`copy_files` gives each agent worktree its own copy of untracked per-checkout
inputs such as `.env`. Nothing kept those out of the index, so an agent running
`git add -A` committed them to its branch and the merge back into the parent
repository failed with an untracked-overwrite error -- naming a file the agent
was never asked to touch, because the same file sits untracked in the
destination work tree. Copied names are now registered in the worktree's
`info/exclude`, anchored to the repository root (#5966).
