## Consolidate `(errors, ok)`, `(detail, name, passed)`, and `(failures, ok)` result types

- Defined canonical frozen dataclasses `ReceiptVerification`, `CheckOutcome`, and
  `FailuresResult` in `bernstein.core.verify_result`.
- Consolidated 11 duplicate declarations across non-security modules onto those
  three shared definitions via compatibility aliases:
  - `(errors, ok)`: `sla_receipt.ReceiptVerification`,
    `supervisor_receipt.ReceiptVerification`,
    `selection_receipt.ReceiptVerification`, `c2pa.ManifestVerification`, and
    `otel_projection.ProjectionVerification` -> `ReceiptVerification`.
  - `(detail, name, passed)`: `run_summary.GateResult`, `pr_gen.GateResult`,
    `a2a_conformance.ConformanceCheck`, and
    `compaction_validate.ValidatorVerdict` -> `CheckOutcome`.
  - `(failures, ok)`: `a2a_consume.PolicyVerdict` and `lineage.gate.GateResult`
    -> `FailuresResult`.
- Removed those three collision groups from `_PENDING_SHAPES` in
  `tests/unit/test_verify_result_field_shape_collisions.py`. Refs #5099.
