# Signed batch pass receipts

A batch pass walks a flat list of items (accounts, files, records) and the
batch ledger chains the successes so a resumed pass skips them. The ledger
answers "which items are done". It says nothing checkable about the rest of
the pass: which items failed and on what error, how many attempts each took,
what the worker exited with, what its output ended on. A pass that exited 0
with half its items failed read as a clean run, and the answer lived in log
tails.

Each pass now leaves one receipt. It is a kind of the shared receipt
protocol: signed with Ed25519, verified offline from its own bytes, and
anchored to the ledger's chain heads so the successes it claims are exactly
the entries the ledger appended during the pass.

## What the receipt carries

| Field | Meaning |
|---|---|
| `batch_id`, `pass_id` | the batch and the pass, as the caller names them |
| `ledger_head_before`, `ledger_head_after` | the ledger's chain head when the pass started and ended |
| `items[]` | every item considered, in processing order |
| `items[].outcome` | `success`, `failed` or `skipped` |
| `items[].attempts` | attempts made; `0` for a skipped item |
| `items[].exit_code` | the last attempt's process exit code, or `null` |
| `items[].output_tail_sha256` | digest of the worker's output tail |
| `items[].failure_reason` | the last non-empty line of the output tail, failures only, at most 200 characters |
| `success[]`, `failed[]`, `skipped[]` | the item ids per bucket, in processing order |
| `error_breakdown` | `{reason: count}` across the failed items |

The buckets and the breakdown are derived from `items[]`. The verifier
recomputes them, so an item moved between buckets or an edited count is
reported by name, before the signature is even consulted.

No wall-clock value enters the payload; the ledger entries carry their
instants. Two builds over the same outcomes produce byte-identical bytes.

## Producing one

```python
from bernstein.core.persistence.batch_ledger import BatchLedger
from bernstein.core.persistence.batch_receipt import (
    RECEIPT_KIND,
    BatchItemOutcome,
    build_batch_pass_payload,
)
from bernstein.core.receipts.protocol import sign_receipt

ledger = BatchLedger(ledger_dir)
head_before = ledger.head_hash()
outcomes = []
for item in items:
    result = process(item)                     # your worker
    if result.ok:
        ledger.record(item.id)
    outcomes.append(
        BatchItemOutcome.from_output(
            item.id,
            "success" if result.ok else "failed",
            attempts=result.attempts,
            exit_code=result.exit_code,
            output_tail=result.output_tail,   # digest + reason derived here
        )
    )

payload = build_batch_pass_payload(
    batch_id="nightly-accounts",
    pass_id="2026-09-25",
    items=outcomes,
    ledger_head_before=head_before,
    ledger_head_after=ledger.head_hash(),
)
envelope = sign_receipt(RECEIPT_KIND, payload, private_key_pem=..., public_key_pem=...)
receipt_path.write_text(json.dumps(envelope.to_dict(), indent=2))
```

`from_output` derives the tail digest and, for a failure, the reason from the
tail, so a caller cannot state a reason the tail does not end on. Items the
pass never attempted go in as `skipped` with no attempts and no output.

## Verifying one

Offline, from the file alone:

```bash
bernstein verify pass.receipt.json
```

The command reads the envelope's `kind`, checks the payload digest, the
Ed25519 signature over kind and payload together, and the payload's own
consistency, and exits 0 or 1. The key is the one embedded in the envelope
(trust on first use). The same check is available in Python through
`bernstein.core.receipts.protocol.verify_receipt`.

Against the ledger, when you hold it:

```python
from bernstein.core.persistence.batch_receipt import verify_batch_pass_against_ledger

errors = verify_batch_pass_against_ledger(envelope.payload, BatchLedger(ledger_dir))
```

This re-verifies the ledger's hash chain, walks it from `ledger_head_before`
to `ledger_head_after`, and requires the entity ids on that segment to equal
the receipt's `success` list in order. A success the ledger never chained, a
chained success the receipt omits, an unknown head, or an edited ledger line
is each reported by name.

## Failure modes

| Symptom | Cause |
|---|---|
| `success: 'x' is claimed but the ledger segment does not record it` | the receipt claims an item the pass did not chain |
| `ledger_head_after: no ledger entry after ledger_head_before has hash ...` | the receipt points at another pass, or the ledger was compacted past it |
| `ledger chain: entry N ... has a hash its content does not produce` | the ledger file was edited |
| `error_breakdown: does not match the failed items` | the breakdown was edited, or the items were |
| `signature: ...` | any signed byte changed after signing |
