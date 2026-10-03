## `bench verify` checks coverage and score, and mock verdicts are no longer signed

`bernstein bench verify` answered MATCH for a bundle with no tasks, for a subset
that left the failing tasks out, for one passing task repeated, and for a stored
score the replay did not produce: it replayed whatever `task_results` held and
compared only `passed`.

It now requires exactly one result for every suite task (`COVERAGE_MISMATCH`
otherwise), compares the replayed score as well as the verdict
(`FABRICATED_SCORE`), and recomputes `harness_fingerprint` from `scheduler_config`.
A bundle file edited after it was written is reported as an error with exit 1
instead of a traceback.

`golden-v1` and `.json` suites have no production adapter: they were scored by a
mock that passes every task with score 1.0, and the result was signed with the
install identity. `bench run` now refuses to sign mock-scored bundles and
`--reliability` receipts with the install identity. `--stub-signer` still works,
records `adapter: mock` in the bundle's `scheduler_config`, and `run` and `verify`
print a MOCK notice.

`resolve_rate`, `abstain_rate` and `confident_error_rate` were placeholders; a
task result records no abstention. They are `null` in the bundle JSON.
`lambda_value` is now covered by `bundle_hash` when it is not the default, and
`bench compare` ranks with it (it read `scheduler_config["lambda"]`, default 1.0)
(#6363).
