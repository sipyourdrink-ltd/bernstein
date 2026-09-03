## Agent spawn and salvage can no longer act on the operator checkout

Two paths could reach the repository the operator has checked out. A warm-pool
slot added by `prepare_speculative_warm_pool` carries `worktree_path=""`, and
`Path("")` is the orchestrator's own cwd, so a spawn that claimed one ran the
agent at the repository root, switched that checkout to `agent/<session>` and
merged back into it. Such a slot is now released and the spawn takes the cold
worktree path. Separately, `git` resolves a cwd that is no longer a registered
worktree by walking up to the enclosing repository, so salvage over a stale
`.sdd/worktrees/<id>` directory committed `.sdd` onto the integration branch
and renamed that branch to `salvage/<session>`; salvage now reads HEAD first
and refuses any branch that is not `agent/*`. The filesystem patch fallback is
unchanged, so a refused salvage still captures the work.
