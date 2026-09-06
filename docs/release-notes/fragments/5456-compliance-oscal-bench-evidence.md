## Evidence packs carry signed benchmark bundles and NIST OSCAL export

Compliance evidence packs (`bernstein compliance pack`) now embed signed benchmark evaluation bundles keyed by control ID. The pack verifier checks bundle signatures and internal receipts, rejecting tampered bundles.

Operators can also export assessment results in NIST OSCAL v1.1.0 JSON format via `bernstein compliance oscal [--standard <id>] [--out <file>]`.
