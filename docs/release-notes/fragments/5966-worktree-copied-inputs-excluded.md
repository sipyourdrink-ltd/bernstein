## `copy_files` names outside the merge deny list can no longer be committed from an agent worktree

`copy_files` gives each agent worktree its own copy of untracked per-checkout
inputs. `.env` and the other names in the merge guard's deny list were already
excluded in every worktree, but any other name -- `secrets.env`,
`config/local.toml` -- was stageable, so an agent running `git add -A`
committed it to its branch and the merge back into the parent repository
failed with an untracked-overwrite error naming a file the agent was never
asked to touch.

Every configured `copy_files` name is now added to the worktree's own excludes
file, anchored to the repository root with gitignore metacharacters escaped.
The exclusion is scoped to that one worktree and disappears with it: the
operator's checkout and other worktrees of the same clone still stage the file
normally. Directory entries and names that resolve outside the repository are
not excluded (#5966).
