## `GateResult` carries a structured `VerificationScope` (issue #5397 slice 1)

`bernstein.core.quality.gate_pipeline.GateResult` now carries a
`scope: VerificationScope | None` field that records, for every
non-skipped, non-bypassed result, which oracle produced the verdict
(`oracle_id`), what kind of check it was (`kind`), the concrete paths
or property names the gate actually exercised (`checked`), and the
known blind spots the gate could not evaluate (`cannot_check`).
`VerificationScope` is a frozen dataclass with ordered tuples so gate
authors can deterministically enumerate what they covered.

Slice 1 ships the field and its type so the interface is stable;
enforcement (requiring a non-`None` scope for non-skipped/non-bypassed
results) is deferred to slice 2.

(#5397)
