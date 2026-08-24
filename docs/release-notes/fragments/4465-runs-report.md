## `bernstein runs report` classifies finished runs from the work ledger

Asking what came of a batch of runs meant reading `.sdd/` by hand once the
orchestrator and its task server had exited. `bernstein runs report` projects
the work ledger into one row per finished run -- `pr-opened`, `gate-failed`,
`no-changes`, `infra-error` or `wedged` -- each carrying the line of evidence
it was classified from. `--since` narrows the window and `--json` emits stable
machine-readable rows (#4465).
