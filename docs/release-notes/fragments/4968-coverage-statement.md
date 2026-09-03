## Coverage statement on the run-attestation receipt

The run-attestation receipt now carries a `coverage` field that classifies each
reported event by its coverage detail and groups the counts under
`(profile_name, source_kind)`, so a downstream reader can see at a glance
which sources contributed evidence for the run. Receipt issuance raises
`RunAttestationReceiptError` when a reported event arrives without a
coverage detail, and `verify_run_attestation_projection` re-derives the
statement from the embedded events and compares it to the signed value, so
a tampered `coverage_detail` field fails verification offline and the
diverging element is named rather than reported as a bare false.

(#4968)
