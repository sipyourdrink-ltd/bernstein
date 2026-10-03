## Run-helper capture library and `bernstein runs helpers` (not yet wired into worktree gc)

`bernstein.core.worktrees.run_helpers` classifies agent-created files that
were executed during a run from the run journal's `file_create` and
`file_execute` rows. It content-addresses their bytes into `.sdd/cas` and
names each one with an origin step, execution count, exit codes and trust
class in `.sdd/runs/<run_id>/run_helpers.jsonl`. `bernstein runs helpers
<run-id>` lists those records. The run journal is not written, so a sealed
run stays sealable. A helper that exited non-zero still counts. An execution
with no exit code is recorded as unknown, not as `0`. Nothing captures
automatically yet: no adapter emits those journal rows and
`bernstein worktrees gc` does not capture before it reaps, so no helper is
captured in a real run today. Promotion to a skill is separate work. This is a
partial implementation. (#5322)
