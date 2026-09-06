## Bind audit range window in run receipt subject

Run receipts embedding an audit range now bind `audit_range_since`, `audit_range_until`,
`audit_range_event_count`, and `audit_range_head_hmac` into the signed subject alongside
`audit_range_head_sha256` under `RUN_RECEIPT_SCHEMA_VERSION = "1.1.0"`. This prevents
post-signing window relabelling or HMAC tampering from silently passing verification.
Receipts with `schema_version = "1.0.0"` continue to verify under legacy rules with an
advisory warning (#5269).
