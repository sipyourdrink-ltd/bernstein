## Janitor `test_passes` timeout is now configurable, and reports as a timeout

The janitor's `test_passes` completion signal used to hard-code a 120s
timeout on the verification command with no way to change it. A project
whose command legitimately took longer than 120s had a completion signal
that could never pass: the command was killed every run, the agent was
treated as dead, and the task was reopened for a fresh agent to run the same
slow command again.

`test_timeout_s` (default `120`, unchanged) is now tunable via
`tuning.quality.test_timeout_s` in `bernstein.yaml` or the
`BERNSTEIN_TEST_TIMEOUT_S` env var (checked first), resolved fresh on every
janitor check.

A killed command is also reported distinctly from a failing test: the
completion-signal detail and the janitor log line now read `timed out after
Ns` instead of the generic `non-zero exit`, so an operator is not sent
looking for a broken test when the process was actually killed for running
too long.
