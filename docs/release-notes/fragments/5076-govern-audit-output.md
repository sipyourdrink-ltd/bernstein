## Governance audit JSON, SARIF and CI exit codes

`bernstein govern audit` now supports `--format text|json|sarif`, `--strict`
and `--quiet`. JSON preserves the complete finding fields, including `id` and
`message` (also included in `doctor --json`). SARIF 2.1.0 includes check rules
and workspace-relative file locations; passing checks use `kind: pass` and
`level: none`. Unmeasurable results use `kind: review` and `level: none`, with
their reason in the message. Evidence locators are omitted, and absolute local
paths in diagnostics are redacted. Run from the repository root for code scanning.
`--quiet` suppresses reports and check-crash warnings while retaining sentinel
journaling and notifications.

The default gate keeps exit `1` for failures, check crashes and unmeasurable
checks. `--strict` distinguishes unmeasurable prerequisites with exit `2`,
unless a check failed or crashed (exit `1` takes precedence). Measured findings
without an explicit pass fail closed. Click usage errors, including incompatible
output flags, also exit `2`. Declared findings no longer fail the gate: they
previously caused exit `1` on main and now allow exit `0` when no other finding
blocks it. An empty selection exits `1`. See the complete exit-code table in
[governance operations](../../operations/governance.md#govern-audit-machine-readable-output-and-ci-gates).
