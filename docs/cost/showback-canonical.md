# Canonical money and text for showback statements

Tenant showback statements (#2554) are recomputed independently by two
parties and compared byte for byte, so every value that enters a statement
needs exactly one encoding. `bernstein.core.cost.showback_canonical` fixes
the three ground rules; everything downstream (line-item receipts, rollup
projections, `bernstein tenant showback` / `verify-statement`) builds on
them.

## Money: fixed-scale integers

| Rule | Value |
|---|---|
| Scale | nano-USD (`1 USD = 10^9`), integer arithmetic only |
| Rounding | exactly once, at line-item creation (`nano_usd_from_float`), banker's rounding to 1 nano-USD |
| Totals | integer sums of exact line-item values; aggregation order can never change a digit |
| In payloads | string-encoded integers (`"250000000"`) so every raw JSON number stays inside the I-JSON safe range |
| Operator text form | `nano_usd_from_decimal_str` / `nano_usd_to_decimal_str`; rendering always carries nine fractional digits |

Nano rather than micro scale: per-token line items on inexpensive models
fall below one micro-USD, and a fixed scale must hold the smallest value
the ledger can attribute without a second rounding step.

## Text: reject, don't repair

Every string key and value in a statement must already be Unicode NFC.
`require_nfc` rejects anything else instead of normalizing it, because a
verifier must hash exactly the bytes it was handed, and normalization
tables move between Unicode versions; rejection semantics are stable, and
data accepted once stays NFC under later Unicode versions.

## Statement bytes

`canonical_statement_bytes` validates the payload tree (no floats
anywhere, NFC everywhere, integers inside the I-JSON range) and then
encodes it with the shared RFC 8785 canonicalizer used for agent-card
signing. Two writers with the same payload produce identical bytes; the
statement hash is therefore comparable across machines and languages.

## Cross-language vectors

`tests/fixtures/showback/canonical_vectors.json` carries the parse,
render, float-bridge, NFC, and statement-hash vectors a non-Python
implementation must reproduce. Extend the vectors file rather than adding
cases in test code; `tests/unit/test_showback_canonical.py` binds the file
to the Python implementation.
