## A refused merge keeps the agent's `.sdd/` output

The decoy-commit merge guard refuses a staged set that touches `.sdd/`, and
the worktree was then cleaned up, so a file an agent wrote under `.sdd/` as
its plan asked was lost. The guard still refuses the merge. Before it aborts,
it now copies the refused `.sdd/` agent output to
`.sdd/quarantine/merge-refused/<branch>/` and names that folder in the
refusal and the log. Credential paths are never copied (#8078).
