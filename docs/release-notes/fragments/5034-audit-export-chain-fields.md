## SIEM exports carry the chain, not just an opaque hmac

An exported audit record used to carry only `hmac` -- a string the receiver
could not check, since they never hold the signing key. `AuditEntry` now also
carries `prev_hmac` and a monotonic `sequence`, emitted by all six SIEM
exporters (Splunk, Elasticsearch, CloudWatch, syslog, webhook, file). Each
exported batch can be closed with a signed `SegmentReceipt` -- first/last
sequence, entry count, and the batch's chain-head HMAC, signed with the same
Ed25519 key used for lineage -- and `bernstein audit verify-export <file>`
checks an exported JSONL file for deletion, reordering, and gaps between
batches using only the file and the signer's public key, no database and no
HMAC key required. A batch that fails to export can now also write an
`audit.export.failed` chain event, so a silent forwarding outage is itself
part of the audit trail (#5034).
