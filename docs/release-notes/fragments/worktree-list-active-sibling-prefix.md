## Worktree listing uses path containment instead of a string prefix

`WorktreeManager.list_active` decided membership with a bare string prefix, so `.sdd/worktrees-archive/archived-1` and the base directory itself came back as managed session ids. Membership is now `Path(wt_path).parent == base_dir`, so only worktrees one level below the base directory are listed (#5727).

Windows behaviour change: git emits forward-slash paths there, so the old `startswith` check never matched and `list_active()` always returned `[]`. It now returns the real sessions, which means `bernstein cleanup` will, on Windows, remove those worktrees and delete their `agent/<id>` branches for the first time.
