# Cross-worker coordination: dependency-aware claiming and the worker mailbox

Workers inside a run used to coordinate only through scheduler dependency
edges. A reviewer that found a cross-cutting problem had no channel to warn
the workers still writing code that repeated it - the information arrived one
full dispatch cycle later, after the tokens were spent. The task server now
ships two coordination primitives, both audit-chained.

## Dependency-aware claiming

Tasks declare the tasks they need finished first. Both spellings are
accepted and populate the same field:

```json
POST /tasks
{
  "title": "Wire the consumer",
  "description": "Consume the schema produced by the producer task",
  "role": "backend",
  "needs": ["<producer-task-id>"]
}
```

The claim API never offers a task whose dependencies are not all in a
terminal-success state (`done` or `closed`):

- `GET /tasks/next/{role}` skips gated tasks entirely.
- `POST /tasks/{id}/claim` returns `409 Conflict` for a gated task.
- `POST /tasks/claim-batch` reports gated ids under `failed`.

Every granted claim is appended to the HMAC audit chain as a
`task.claim_receipt` event carrying the dependency snapshot it was granted
under (`task_id`, `depends_on`, `claimed_by`, `task_version`, `claim_path`).
Claims are journal entries: claim eligibility is reconstructable offline
from the task journal plus the chain, and rebuilding the store from the
same JSONL journal reproduces the identical eligibility projection.

## Worker mailbox

`POST /tasks/{task_id}/messages` hands a structured payload to another
worker's task mid-run. This is not chat: payloads are typed, size-capped,
and addressed to exactly one task - no freeform threads, no undeclared
fan-out.

| Field | Constraint |
| --- | --- |
| `kind` | `finding`, `artefact_ref`, or `question` (closed vocabulary) |
| `body` | 4096 bytes max, DLP-redacted on the write path |
| `sender` | Worker identifier recorded in the signed entry |
| `sender_card_fingerprint` | Optional `sha256:` fingerprint of the sender's agent card |

Per task, at most 128 messages are held (`429` beyond that). Unknown kinds
and oversize bodies are rejected with `422`.

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/<task-id>/messages \
  -H "Content-Type: application/json" \
  -d '{"sender": "reviewer-1", "kind": "finding",
       "body": "Error mapping duplicated; use the shared helper in core/errors."}'
```

### Delivery

Delivery is deterministic and pull-based - the recipient receives pending
messages on its next poll, without a scheduler re-dispatch:

```bash
curl -s "http://127.0.0.1:8052/tasks/<task-id>/messages?since_seq=-1"
```

`since_seq` is a cursor: pass the highest `seq` already processed to fetch
only newer messages. Delivery order is the mailbox chain append order,
which is total - replaying the same journal always reproduces the same
order. At spawn time the same pending messages are rendered into the
worker's task context as a typed `## Coordination mailbox` section; the
rendering is a pure function of the journal, so every adapter type
receives byte-identical coordination context.

### The journal is the receipt

Every accepted message is appended to an HMAC-chained JSONL journal
(`.sdd/runtime/mailbox.jsonl`): each entry embeds the previous entry's
chain tag, is signed with the install's Ed25519 identity (binding sender
attribution to chain position), and is mirrored into the audit chain as a
`task.mailbox_message` event that records only hashes - never the body.

Verification is offline:

```python
from bernstein.core.communication.task_mailbox import TaskMailbox, verify_against_chain
from bernstein.core.security.audit_chain import AuditChainStore

mailbox = TaskMailbox(runtime_dir / "mailbox.jsonl", hmac_key=key, identity_dir=identity_dir)
ok, problems = mailbox.verify()                      # chain + signatures
ok, problems = verify_against_chain(mailbox, chain)  # journal == chain-attested log
```

A tampered body, a flipped sender, or a reordered journal breaks
verification; a message that skipped the audit mirror fails the
cross-check.

## Coordination patterns

**Reviewer broadcast-to-dependents.** A reviewer that finds a cross-cutting
problem posts one `finding` per still-open dependent task. Each worker
receives the finding on its next poll or, if not yet spawned, inside its
task context - no re-dispatch cycle, no repeated mistake.

**Planner steering.** A planner posts a `question` to a worker's task
(schema frozen? interface owned?) and the worker answers through its normal
result surface. The declared route keeps the exchange attributable and
chain-verifiable instead of becoming an unaudited side conversation.

**Artefact handoff.** A producer posts an `artefact_ref` carrying a content
address (for example an evidence-bundle hash). The consumer resolves the
address through the store it already trusts; the mailbox never carries the
artefact bytes themselves.
