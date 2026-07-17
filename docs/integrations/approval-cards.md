# Approval cards v2

An approval card is what an operator sees when a gated tool call needs a human
decision. In v2 the card is a **hash-committed decision record**: the whole
decision context is canonicalised, hashed, and appended to the HMAC audit
chain, so a postmortem can prove not just *what* was approved but *what the
approver was told* at decision time.

## What the card carries

Every field below lives inside one hashed envelope (`ApprovalCardV2`):

| Field | Meaning |
|---|---|
| `action` | Tool name plus a canonical SHA-256 digest of its arguments |
| `reasoning` | The agent's stated intent, bounded to a fixed length |
| `impact` | Blast-radius score, `hard_one_way` flag, rationale, and the ids of every detector that fired |
| `rollback` | A per-tool-class undo procedure, with an explicit `irreversible` marker when a one-way-door detector fired |
| `not_after` | The expiry deadline, in unix epoch seconds |

`card_hash` is the SHA-256 over the canonical JSON of the envelope (sorted
keys, compact separators). Because it commits to every field the operator saw,
a decision that echoes `card_hash` commits to exactly what was displayed. A
card that merely displays extra text without hashing it cannot offer that
guarantee.

## The lifecycle

1. **Issue.** The gate builds the envelope, computes `card_hash`, and appends a
   `chat.approval_card.issued` event carrying the full envelope, the hash, and
   the previous chain digest. Drivers render the envelope fields **verbatim**
   (`render_card_text`), so the displayed card equals the hashed record.
2. **Resolve.** A decision must echo the exact `card_hash`. The gate refuses
   when:
   - the echoed hash matches no issued envelope (any field the operator saw was
     changed), recording a `chat.approval_card.refused` event with reason
     `hash_mismatch`, or
   - the decision arrives at or after `not_after`, recording a refusal with
     reason `expired`.
   A clean resolve records `chat.approval_card.resolved`.
3. **Verify offline.** `bernstein audit verify` reconstructs every resolved
   card from the issue event, confirms the stored envelope still hashes to its
   recorded `card_hash`, confirms the decision echoed an issued envelope, and
   confirms the decision landed before expiry.

## Chain-enforced expiry

Expiry is decided by the chain-side clock against the envelope's `not_after`,
never by whatever buttons the chat client still renders. This holds across a
chat-process restart: a fresh process reconstructs the issued envelope from the
audit chain and still refuses a stale approve. The refusal is chain-recorded,
so an operator can prove a late decision was contained and never executed.

## Determinism

Issuing the same pending approval against identical repository state produces
byte-identical envelopes and an identical `card_hash`. The envelope is a pure
projection of its inputs (the tool call, the stated intent, the blast-radius
detectors, and the issue time), so two operators reconstruct the same card.

## Irreversible actions

When a change trips a `hard_one_way` blast-radius detector (schema migration,
secrets write, `rm -rf`, `DROP`/`DELETE` SQL, ...), the card carries
`rollback.irreversible = true` and renders an explicit irreversible marker.
Because the flag is part of the hashed envelope, the marker is cryptographically
committed and cannot be stripped without changing `card_hash`.

## Server-initiated prompts

MCP `elicitation/create` requests that match no auto-resolve policy, and A2A
tasks entering `input-required`, are routed into the same pipeline. The
server-initiated prompt becomes a v2 card on the bound chat thread and inherits
the whole discipline: committed decision context, chain-side expiry, and the
audit trail. For an MCP elicitation the response equals the operator decision,
and the issue and resolve events share `card_hash`, so the answer and the
approval record are chain-linked.

## Related

- Chat bridges (delivery surfaces): `operations/chat-bridges.md`
- Microsoft Teams setup: `integrations/teams-setup.md`
- Audit log and `bernstein audit verify`: `security/audit-log.md`
