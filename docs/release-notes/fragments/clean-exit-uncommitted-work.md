## A clean exit that left uncommitted work is no longer recorded as "no changes needed"

An orphaned agent was judged on two reads that never see a file on disk: an
empty diff, scraped from the agent's own log, and no commits on its branch. An
agent that wrote its whole deliverable and died before committing produced the
same two zeroes as one that decided nothing needed changing, so the task was
auto-completed and the run reported that nothing had changed about a branch
that then gained a salvaged commit no one verified. The orphan handler now
reads the worktree's uncommitted paths before that verdict and fails the task
as unverified instead, so its retry can land the work as a real commit. The
orchestrator's own worktree files - runtime state under `.sdd/`, the generated
`CLAUDE.md`, the adapter's `.claude` settings - are filtered out, and any
failure to read the status leaves the previous behaviour in place, so this can
only ever suppress an auto-completion and never fail a healthy task.

(#5619)
