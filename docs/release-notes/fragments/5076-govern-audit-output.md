## Governance audit JSON, SARIF and CI exit codes

`bernstein govern audit` now supports `--format text|json|sarif`, `--strict`
and `--quiet`. JSON preserves the complete finding fields, including `id` and
`message` (also included in `doctor --json`). SARIF 2.1.0 includes check rules
and file locations; passing checks use `kind: pass` and `level: none`.
`--quiet` suppresses reports and check-crash warnings while retaining sentinel
journaling and notifications.

The default gate keeps exit `1` for failures, check crashes and unmeasurable
checks. `--strict` distinguishes unmeasurable prerequisites with exit `3`,
unless a check failed or crashed (exit `1` takes precedence). Measured findings
without an explicit pass fail closed. Exit `2` remains reserved for invalid
CLI usage, including incompatible output flags. Declared findings do not affect
the gate, and an empty selection exits `1`. See the complete exit-code table in
[governance operations](../../operations/governance.md#govern-audit-machine-readable-output-and-ci-gates).
