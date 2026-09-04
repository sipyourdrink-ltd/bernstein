## Leakage benchmark suite and all-surface canary scanning

Seeded canary secrets (fake API keys, internal emails, absolute paths, nonces) across 5 encodings (Plain, base64, URL-encoded, split lines, JSON-escaped) and 5 seed points are now suite-tested across all 8 governed output surfaces (journal, receipts, PR text, logs, telemetry, evidence pack, bench bundle, run archive). The suite enforces a zero-hits requirement and attributes any detected leakage to the responsible redaction stage (#5450).
