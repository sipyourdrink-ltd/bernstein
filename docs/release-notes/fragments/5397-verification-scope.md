## `GateResult` carries a structured `VerificationScope` (issue #5397 slice 1)

`bernstein.core.quality.gate_pipeline.GateResult` now carries a
`scope: VerificationScope | None` field that records, for every
non-skipped, non-bypassed result, which oracle produced the verdict
(`oracle_id`), what kind of check it was (`kind`), the concrete paths
or property names the gate actually exercised (`checked`), and the
known blind spots the gate could not evaluate (`cannot_check`).
`VerificationScope` is a frozen dataclass with ordered tuples so gate
authors can deterministically enumerate what they covered. The new
invariant is enforced in `__post_init__`: a result whose status is
not `skipped` or `bypassed` and whose `scope` is `None` is refused at
construction, so a green verdict with no recorded coverage can never
escape into a gate report.

(#5397)
