## `bernstein evolve run` tracks a recurring failure in one issue

`bernstein evolve run --dry-run` now builds its failure-pattern drafts from the
run ledgers — the same classified rows `bernstein runs report` prints — instead
of live task metrics, so each draft carries a fingerprint that is stable across
scans. With `--github`, a draft is reconciled against an `evolve-fingerprint-<hex>`
label rather than the issue title, so a failure that recurs updates the one issue
already tracking it instead of filing a new one per cycle. A new evolve CLI page
documents the run-ledger → draft → issue contour and the dry-run flow (#4504).
