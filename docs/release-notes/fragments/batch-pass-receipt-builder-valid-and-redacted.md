## Batch pass receipts: the builder only returns payloads the verifier accepts, and failure reasons are redacted

`build_batch_pass_payload` now checks the assembled payload against the same
validator the registered `batch.pass` kind uses and raises `ValueError` at
build time, instead of returning a payload that signs fine and then fails
`bernstein verify`. `BatchItemOutcome.from_output` gives a failed item with an
empty or whitespace-only output tail a reason (`exit <code>`, or `no output`
when there was no process), so such an item no longer produces an invalid
receipt.

The failure reason copied from the last line of the worker's output is now
passed through the log redaction helper before it is truncated, so credentials
and PII in that line no longer end up in the signed, shareable receipt or in
`error_breakdown`. The tail digest is unchanged.
