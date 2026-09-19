## Worktree listing no longer accepts paths outside the base directory

`WorktreeManager.list_active` decided membership with a bare string prefix, so `.sdd/worktrees-archive/archived-1` and the base directory itself came back as managed session ids. Because `bernstein cleanup` feeds that list into `cleanup(session_id)`, a harvested name could become a removal target for a branch the manager does not own. Membership is now a path containment test - a worktree's parent must be the base directory itself - so only real one-level-down sessions are listed (#5727).
