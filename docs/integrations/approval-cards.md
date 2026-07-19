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
   the previous chain digest. The event is appended *before* the card becomes
   resolvable, so a failed append leaves no settleable card behind. Drivers
   render the envelope fields **verbatim** (`render_card_text`): every hashed
   field is shown with a round-trippable value, so an operator can rebuild the
   envelope from what they read and confirm it hashes to the displayed
   `card_hash`.

   Field values have their line separators escaped. One row per line is what
   makes the projection readable, and that is exactly what an agent-supplied
   value containing a newline would exploit: unescaped, `reasoning` can render
   a structurally complete, benign-looking card ending in a forged
   `Card hash:` row *above* the true fields, so an operator approving from the
   rendered text approves the forgery. The escape is injective, so the display
   stays lossless, and it happens at render time only, so no `card_hash` moves.

   The rendered body is bounded and does not grow with argument length, because
   no chat driver chunks: Discord rejects a body over 2000 characters and Slack
   caps a section at 3000, and an oversized card is not truncated, it fails to
   deliver. The canonical JSON envelope is therefore **not** inlined in the chat
   body; `render_card_envelope` exposes it where length is unconstrained.
2. **Resolve.** A decision must echo the exact `card_hash`. The whole
   check-and-commit runs under one lock, so concurrent decisions on the same
   hash cannot both settle. The gate refuses, recording a
   `chat.approval_card.refused` event, when:

   | Reason | Refused because |
   |---|---|
   | `hash_mismatch` | The echoed hash matches no issued envelope, so some field the operator saw was changed |
   | `already_settled` | The card has already been decided; a card settles exactly once |
   | `invalid_decision` | The decision is not `approve` or `reject` |
   | `before_issue` | The decision clock predates the envelope's `created_at` |
   | `expired` | The decision arrived at or after `not_after` |
   | `cross_worktree` | The card was pinned to a different worktree |
   | `cross_conversation` | The card was issued on a different conversation |

   A clean resolve records `chat.approval_card.resolved`. That event records the
   origin the decision **arrived from** in `worktree_id` / `thread_id`, and the
   origin it was **issued into** in `issued_worktree_id` / `issued_thread_id`,
   so the chain attributes a decision to whoever actually made it.
3. **Verify offline.** `bernstein audit verify` walks the chain in order and,
   for every resolved card, confirms the stored envelope still hashes to its
   recorded `card_hash`, that the decision echoed an envelope issued *earlier
   in the chain*, that no card is settled twice, that the decision is one the
   gate would accept, that it was settled from the origin the card was issued
   into, and that the decision timestamp is finite, positive, and inside the
   envelope's window (`created_at <= resolved_at < not_after`).

   The verifier **reports, it never raises.** Every event is processed under a
   guard that turns any fault into a recorded failure. This matters because the
   approval-card pillar runs before three others in `bernstein audit verify`:
   an escaping exception would abort the run, so one malformed record could
   suppress detection of unrelated tampering elsewhere. A verifier that its own
   input can crash is a denial-of-audit primitive, not a check.

   Failures are reported by **who they accuse**:

   | Channel | Meaning | What to do |
   |---|---|---|
   | `errors` | The record was evaluated and failed | Treat as possible tampering |
   | `verifier_errors` | The record could not be evaluated; this code raised | File a bug, not a security incident |

   Reporting an internal fault through the same channel as "envelope was
   mutated after issue" would tell an operator their log was tampered with when
   in fact the verifier is broken. Both channels set `ok = false`: an
   unevaluable record is not a passing record, and a bug here must never
   produce a clean bill of health.

   The pinned origin is read from the **issue** event, not from the
   `issued_*` keys on the settlement being checked, so a forger cannot clear
   the origin check by rewriting both halves of the pair to agree.

The gate and the verifier enforce the same window, in full: a settlement
timestamp must be finite, strictly positive, and at or after `created_at`. The
gate refuses `before_issue` rather than appending a settlement its own verifier
would reject, because the audit log is append-only and such a record would make
`bernstein audit verify` fail permanently with no remediation. Checking only
one half of the window would still let a card issued at `created_at = 0`
settle at `now = 0.0` and write exactly that record.

Everything the gate enforces, the verifier reconstructs. The gate is the live
control and the chain is the proof, so an invariant that held only at the gate
would be unauditable on a chain written by an older build or a second writer.

## Settling exactly once

A card settles once. The settled set is rebuilt from the chain's `resolved` and
terminally-`refused` events, not from process memory, so a restart does not
reopen a card the chain already shows as decided, and a captured `card_hash` is
a single-use token rather than a reusable one.

Exactly-once is also **reconstructable**, not merely enforced live: the offline
verifier fails a chain carrying two settlements of one issued card. A chain
written by an older build or a second writer therefore still surfaces the
violation, rather than passing the check that exists to detect it.

Only expiry counts as a terminal refusal. Expiry is monotone: once the chain has
seen a card pass its `not_after`, no later clock reading revives it. The other
refusal reasons describe a rejected *attempt*, not a settled card, and
deliberately leave the card pending. Burning a card on a `cross_worktree` or
`hash_mismatch` refusal would let anyone who can reach the chat surface deny the
operator their pending decision.

## Origin pinning

A card issued into a worktree and a conversation commits to that origin. A
decision arriving from a different worktree or conversation is refused and
chain-recorded rather than honoured, so observing a `card_hash` in one context
does not let it be exercised in another.

A check is skipped only when the card carried no such pin. Once a card is
pinned the comparison is unconditional, **including against an empty incoming
origin** - a caller that cannot say where a decision came from cannot settle a
pinned card. The value the guard exists to distrust must not be able to switch
the guard off by being absent.

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

Timestamps must be finite numbers, and integer timestamps are widened to
floats before hashing. Both rules protect the projection: `NaN` compares false
against everything, so a `NaN` `not_after` would produce a card that never
expires, and `1000` and `1000.0` serialise to different bytes, so the same
instant would otherwise yield two different hashes. Canonical JSON is emitted
with `allow_nan` disabled, so an envelope can never hash over bytes that no
conforming JSON parser reads back.

`to_dict` emits the envelope in its **persisted normal form**: every field is
coerced to exactly the type `from_dict` rebuilds, so the round-trip is a fixed
point and the hash commits to the bytes that actually get stored rather than to
the in-memory object. This matters because `card_hash` is recomputed from
stored JSON on two paths (gate rehydration after a restart, and the offline
verifier), and JSON does not preserve Python's numeric types: an `int` `0`
serialises as `0` while the `float` rebuilt on read serialises as `0.0`.
Without the normal form those two disagree, and an honest card becomes
unresolvable after a restart and fails `audit verify` permanently.

Two fields are bounded so the envelope cannot be inflated by its inputs: the
reasoning digest (`REASONING_MAX_CHARS`) and the path embedded in the rollback
template (`ROLLBACK_PATH_MAX_CHARS`). Both bounds are deterministic, so the same
input always truncates to the same bytes, and the truncated form is what gets
hashed and displayed. The full arguments stay committed through
`action.args_digest`, so bounding costs nothing in what the card proves.

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
