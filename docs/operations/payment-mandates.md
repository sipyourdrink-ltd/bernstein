# Payment mandates and transaction receipts

**Audience:** operators who let an agent pay for something mid-run (a metered
data API, a paid search endpoint, a compute-credit top-up) and need one
auditable answer to "who authorized this charge, under what limit, and did the
run stay inside it?"

**What:** `bernstein payment-mandate` issues a signed spending authorization, and
records every transaction attempt under it — approved or refused — as a
chain-anchored receipt that verifies offline.

**Boundary:** Bernstein never moves money. It authorizes, bounds, and *proves*;
the actual settlement stays with whatever external payment rail you already use.
The deliverable is the verifiable authorization record, not a wallet. See
`src/bernstein/core/payments/`.

---

## The two artefacts

| Artefact | What it is | Identity |
|---|---|---|
| `SpendMandate` | An operator-issued, Ed25519-signed authorization with a bound scope. | `sha256` of the signed body (`mandate_hash`). |
| `TransactionReceipt` | The record of one transaction attempt under a mandate. | `sha256` of the receipt body (`receipt_hash`). |

A mandate is signed with the install's existing agent-card keystore key — no new
key surface is introduced. Each receipt is appended to the lineage spine with a
detached-JWS `.jws` sidecar and mirrored as a `payment.authorized` /
`payment.refused` audit event carrying the chain digest captured at decision
time. Strip the lineage signature or the audit chain and a receipt is just a
JSON file; anchored, it is an offline-verifiable proof.

## Presence mode — the structural distinction

A mandate is issued in one of two presence modes, and the mode changes *what the
signature covers*:

| Mode | The signature binds | Enforcement |
|---|---|---|
| `human_present` | A **concrete transaction envelope**: an exact amount + recipient the operator signed off in the loop. | No per-transaction cap; cumulative spend cannot exceed that one concrete amount (effectively single-shot). |
| `delegated` | A **bounded envelope** the agent transacts under: a max amount, an expiry, optionally a per-transaction cap and an allowed-category set. | Several transactions allowed while their cumulative total stays inside the bound. |

The mode is a signed field. Every receipt records which mode authorized it.

## Money and text encoding

- Amounts are string-encoded integer **nano-units** (`1e-9` of a major unit),
  rounded half-even exactly once when the amount is first encoded. No float ever
  enters a signed payload, and every comparison and cumulative sum is exact
  integer arithmetic.
- `recipient` and category strings must already be **NFC**; a non-NFC string is
  rejected, never silently normalized, so the signed bytes equal the input
  bytes.
- `currency` is an ISO-4217-style three-letter uppercase ASCII code.

## Refusals are first-class receipts

An out-of-scope request is refused, and the refusal is itself a signed,
chain-anchored receipt with a closed-enum reason hash-bound to the mandate:

| `refusal_reason` | Trigger |
|---|---|
| `bad_signature` | The mandate signature does not verify. |
| `wrong_presence_mode` | The request's presence mode differs from the mandate's. |
| `expired` | `now` is past the mandate's `not_after`. |
| `wrong_recipient` | The request recipient differs from the mandate's. |
| `over_max_amount` | The amount exceeds `max_amount` (or the per-transaction cap). |
| `cumulative_exceeded` | This amount plus prior authorized spend would exceed `max_amount`. |

When several checks fail, the reported reason follows a fixed precedence:
`bad_signature` → `wrong_presence_mode` → `expired` → `wrong_recipient` →
`over_max_amount` → `cumulative_exceeded`, so the decision is deterministic. A
request whose currency does not match the mandate's is a malformed request (a
hard error), not a spend-policy refusal.

## Cumulative-spend safety

Cumulative spend is aggregated on read from an append-only receipt ledger keyed
on `mandate_hash`. The read-aggregate-decide-append sequence runs under an
exclusive file lock, so two concurrent workers sharing one mandate can never
both observe a stale total and each admit spend that, together, exceeds the cap.

## CLI

```
# Issue a delegated mandate: up to $100, $25 per transaction, expires at <unix>.
bernstein payment-mandate issue \
    --presence-mode delegated \
    --max-amount 100.00 --currency USD \
    --recipient vendor:acme-data-api \
    --per-tx-cap 25.00 --allowed-category data \
    --not-after 2000000000

# Inspect a stored mandate and verify its signature offline.
bernstein payment-mandate show <mandate_hash>

# Attempt a transaction; emits an anchored receipt (authorized or refused).
bernstein payment-mandate spend \
    --mandate <mandate_hash> \
    --amount 20.00 --to vendor:acme-data-api \
    --category data --presence-mode delegated

# Verify a receipt entirely offline.
bernstein payment-mandate verify --receipt <receipt_hash>
```

`spend` exits `0` when authorized and `1` when refused (the refusal receipt is
still recorded). `verify` exits `0` when every check passes and `1` otherwise.

## Offline verification

`payment-mandate verify` recomputes, with no live process:

1. the mandate's Ed25519 signature;
2. that the receipt is bound to that mandate (`mandate_hash`);
3. the receipt's lineage entry, its content hash, its operator HMAC, and its
   detached JWS sidecar;
4. the full audit-chain HMAC;
5. the `payment.authorized` / `payment.refused` event mirroring the receipt.

It then reports the bound scope (amount, recipient, expiry) the decision was
checked against. Tampering with the receipt body, the mandate scope, or the
chain digest — or stripping the `.jws` sidecar or the audit event — makes
verification fail, and a bare receipt file cannot be replayed as an
authorization.

## Interop

External signed-mandate schemes plug in through a narrow `MandateAdapter`
protocol (`to_external` / `from_external`). The core ships two scheme-agnostic
adapters — a bernstein-native one and a generic JWS pass-through — and blesses no
external scheme as canonical. A concrete settlement path (issue #2528) is one
adapter over this surface.

## On-disk layout

```
<workdir>/.sdd/payments/
    mandates/<mandate_hash>.json    signed mandates
    receipts/<receipt_hash>.json    anchored receipts
    ledger.jsonl                    append-only receipt ledger (cumulative aggregation)
<workdir>/.sdd/lineage/             lineage spine + .jws sidecars
<workdir>/.sdd/audit/               HMAC audit chain
<workdir>/.bernstein/keys/          operator agent-card signing keypair
```
