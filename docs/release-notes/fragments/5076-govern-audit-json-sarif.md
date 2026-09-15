## govern audit: JSON and SARIF output, and CI exit code gating

`bernstein govern audit` emits machine-readable findings and a CI-gated exit code (#5076):

- `--format json` outputs a JSON list containing one object per finding with every contract field.
- `--format sarif` outputs standard SARIF 2.1.0 with `ruleId = finding.id` and a `kind` property carrying the three-state verdict.
- Exit codes: 0 when every required check is measured and passed; 1 when any required check is measured and failed; 2 when any required check is `not_measurable` and `--strict` is set. Declared findings never change the exit code.
- `--quiet` suppresses all terminal output, leaving the exit code as the sole result.
