## `bernstein worktrees gc` is a dry run unless you ask it to delete

An unqualified `bernstein worktrees gc` now prints the reap plan and
stops, instead of going straight to a confirmation prompt for a
destructive sweep. Deleting is opt-in through the new `--apply`.

`--yes` still deletes, so an existing `gc --yes` in a cron job or a
script keeps working unchanged: it has always meant "do it without asking
me", and reading it as a dry run would leave those runs reporting success
while cleaning nothing up. `--dry` still forces a dry run and outranks
both, so `gc --yes --dry` means what it always meant.

A dry run no longer prompts — there is nothing to authorise — and says
after the plan that nothing was deleted and what to pass instead (#5112).
