# Verifiable spending mandates

When an agent spends against an external service, Bernstein records the
authority and the settlement as one **journal-anchored consent receipt** so
"this payment was authorized by this exact intent" is provable offline. The
receipt is not "a payment plus an audit line" - it *is* the verifiable
artefact, anchored by a lineage-spine entry hash. Strip the spine and it is
just a file; anchored, it recomputes.

Module: `src/bernstein/core/protocols/payments/mandates.py`. CLI group:
`bernstein mandate`.

## The shapes (AP2 protocol naming)

| Object | Meaning |
|---|---|
| **Intent Mandate** | What the operator wants: allowed tool calls, a per-task spend cap, an optional expiry. Signed with the audit-chain key. |
| **Cart Mandate** | The concrete action: the specific tool calls proposed under an intent, plus the settlement amount. Signed; refused unless its calls are a subset of the intent. |
| **Settlement reference** | The HTTP 402 pay-and-retry reference (x402 / AP2): the `402` challenge digest, the payment reference, and the retried-request digest. |
| **Consent receipt** | Binds `{mandate_hash, authorized_tool_calls_hash, settlement_ref, journal_entry_hash}`. Its `journal_entry_hash` is the spine entry hash over the receipt bytes. |

## Guarantees

- **Verifiability** - `bernstein mandate verify <mandate_hash>` recomputes both
  mandate signatures, the cart-to-intent binding, the receipt's three bound
  hashes, and the spine anchor over the receipt bytes. Any single-byte tamper
  fails the check.
- **Determinism** - the authorized action set is a pure projection of
  `(intent, cart, revocation state, clock)`. Two operators with identical state
  authorize the byte-identical action set. No LLM in the loop.
- **Spend caps** - the cost ledger (`.sdd/cost/ledger.jsonl`) is the single
  enforcement point. A settlement whose cumulative task spend plus amount would
  breach the intent's cap is refused.
- **Revocation** - `bernstein mandate revoke <mandate_hash>` appends a signed
  entry to `.sdd/mandates/revocations.jsonl` rather than mutating anything.
  Subsequent actions under the mandate are refused while the original stays
  provable. A revocation signed with the wrong key does not count.

## CLI

```
bernstein mandate emit   --intent i.json --cart c.json --settlement s.json
bernstein mandate verify <mandate_hash> --intent i.json --cart c.json
bernstein mandate revoke <mandate_hash> --reason "budget change"
```

`emit` binds the mandate and settlement into a consent receipt, anchors it in
the mandate lineage spine (`run_id="mandates"`), and mirrors it into the
HMAC-chained audit log (`mandate.consent_receipt`). Exit code `1` means the
settlement was refused (cap breach, revocation, or a signature / intent-binding
gate).

## On-disk layout

| Path | Contents |
|---|---|
| `.sdd/mandates/receipts/<mandate_hash>.json` | The consent receipt binding + its anchor. |
| `.sdd/mandates/revocations.jsonl` | Append-only signed revocation ledger. |
| `.sdd/lineage/mandates/spine.jsonl` | The lineage spine anchoring every receipt. |
| `.sdd/audit/` | The HMAC audit chain carrying the mirrored events. |
