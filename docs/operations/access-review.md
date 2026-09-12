# Access review

A periodic access review asks one question: who granted this agent the power it
had, when, and on whose authority. Bernstein already records every fact that
answers it — the per-hop delegation receipts hold the authorization trail, and
the grant chain holds the capability ceilings — but until now an operator had to
join the two ledgers by hand.

`bernstein identity review` derives that document from the chains, signs it, and
records a reviewer's sign-off as its own chain event.

## Deriving the review

```
bernstein identity review [--since D] [--until D] [--root DIR]
                          [--format json|csv] [--output FILE]
```

Reads `<root>/delegation/*.jsonl` and `<root>/grants/*.jsonl` (`--root` defaults
to `.sdd/audit`) and emits a signed envelope. `--since` and `--until` accept an
ISO date (`2026-01-01`), an ISO datetime (`2026-01-01T00:00:00Z`, naive values
read as UTC), or a bare epoch second. The window is half-open:
`since <= created < until`.

`--until` is left open by default on purpose. Defaulting it to "now" would read
a wall clock, and two operators projecting the same chain would then get
different bytes. Pass it explicitly when the review must close at a period end.

Each row carries:

| Field | Meaning |
|---|---|
| `principal` | Whose access the row describes (`task:<id>` for grants, the delegation audience for hops) |
| `event` | `grant_issued`, `grant_revoked`, `delegation`, or `capability_ceiling_change` |
| `authorized_by` | The principal that authorized it (the grant issuer, or the delegating party) |
| `chain` / `run_id` | Which ledger and which run the fact came from |
| `chain_event` | The HMAC of the record that carries it — the chain event itself |
| `created` | Epoch second of the recorded event |
| `detail` | Kind-specific facts: grant id, audience, expiry, ceiling, act, scope reference |

A `capability_ceiling_change` row is derived by comparing a task's successive
grants across the *whole* chain, so a ceiling set before the window still names
the value it changed from; only the row itself is windowed.

`--format csv` renders the same rows for a spreadsheet. The signed artefact is
always the JSON envelope: a rendering carries no verification authority.

### Unverified chains produce no rows

A run whose grant or delegation chain fails HMAC verification contributes
nothing to `rows`. It is named under `unverified` with the error instead, so a
review over a broken chain says so rather than quietly listing fewer facts.

### Two anchors, no clock

The envelope carries the same pair of anchors the grant records already use:

* `digest` — `sha256:<hex>` over the RFC 8785 canonical bytes of the review
  body. This is the content address a sign-off attaches to.
* `signature` — Ed25519 by the install manager identity over the same body,
  checkable offline against the embedded `issuer_pubkey`.

Neither reads a wall clock, so re-deriving the review over the same window and
the same chain state is byte-identical. Re-indenting the file does not change
what it means: both anchors are recomputed from the parsed body.

## Recording a sign-off

```
bernstein identity review sign-off --document FILE --reviewer WHO
                                   [--decision approved|rejected]
                                   [--note TEXT] [--root DIR]
```

Verifies the document first — a review whose digest or signature does not check
out is refused, so no sign-off can name bytes that were already broken — and
then appends one HMAC-chained record to
`<root>/access_review/signoffs.jsonl`, keyed by the install audit key exactly as
the delegation and grant chains are. The record names the reviewer, the
decision, the reviewed window, the row count, and the document digest.

There is no `--decision auto`. A review records a human decision; it does not
make one.

## Verifying a sign-off

```
bernstein identity review verify --document FILE [--reviewer WHO] [--root DIR]
```

Recomputes the digest **from the file** rather than reading it, walks the
sign-off chain from genesis, and reports the record that carries that digest.
A document edited after its sign-off therefore inherits nothing: the digest
moves, no record names it, and the command exits non-zero. A mutated, deleted,
or reordered sign-off record surfaces as a chain break naming the offending
index.

Exit codes: `0` verified, `1` verification or read failure, `2` invalid
arguments.

## What a pass does not establish

That the reviewed grants were appropriate policy; that the chains handed to the
reviewer were complete; that the reviewer read what they signed. It establishes
only that these exact bytes were projected from those chains, and that a named
reviewer signed off on them.

RBAC and budget verdicts are a different question with a different command —
see `bernstein governance verify`.
