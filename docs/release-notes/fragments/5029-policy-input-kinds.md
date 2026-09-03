## Type policy inputs as observed or operator-asserted

`bernstein compliance check` now emits two new lines: `Evidenced: N   Operator-declared: M`. The `--json-output` flag adds a new `summary` block with evidence coverage fields, and new library surface functions expose policy input kinds, evidence status, and classification helpers. The `audit_retention_days` CLI flag is removed, read from audit segments instead.

(#5029)