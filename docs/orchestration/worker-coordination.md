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

## Release receipts

Every transition that surrenders a held claim appends the matching
`task.release_receipt` event to the same chain, carrying `task_id`, `role`,
`released_by` (the holder that surrendered it), `task_version`,
`release_path`, `reason`, and the `from_status` / `to_status` pair.

| Path | `release_path` |
| --- | --- |
| `POST /tasks/{id}/force-claim` | `force_claim` |
| `POST /tasks/{id}/reopen` | `reopen` |
| `POST /tasks/{id}/release` | `release` |
| `POST /tasks/{id}/cancel` | `cancel_cascade` (root and descendants) |
| `TaskStore.cancel` | `cancel` |
| `POST /tasks/{id}/fail` | `fail` / `fail_contract_violation` |
| `POST /tasks/{id}/complete` with an empty summary | `fail_empty_completion` |
| Typed worker refusal | `refuse` |
| `TaskStore.abandon` (the abandoned task) | `abandon` |
| `TaskStore.abandon` (downstream tasks it blocks) | `abandon_cascade` |
| Stale-claim reset after a restart | `restart_recovery` |
| Cluster node departure | `node_departure` |

A surrender is minted only when the task carried evidence of an actual claim
(`claimed_at` or `claimed_by_session`). Status alone is not enough: a task can
reach `WAITING_FOR_SUBTASKS` or `BLOCKED` without ever having been claimed,
and a fabricated surrender on a signed chain cannot be told apart from a real
one by a verifier.

Delivery is not a surrender. A task reaching `DONE` or `CLOSED` mints no
receipt, because the worker finished the job it claimed. Its claim ends only
if the task later goes back to the pool, which happens through `reopen` and
does mint one.

Both halves are written by the server itself, so a plain `bernstein serve`
node records them without the orchestrator's audit wiring. A ledger that
records acquisitions and no surrenders replays as node A holding a task node
B is already executing; with both halves,
`bernstein.core.security.audit_chain.reconstruct_claim_holders` folds any
prefix of the chain into the last claimant of every task at that point,
offline. That projection is attribution, not liveness: a delivered task stays
mapped to the worker that delivered it, and the guarantee it does carry is
that no task is ever mapped to one claimant while another node holds it,
because every path back to the pool records a release first.

## Worker mailbox

`POST /tasks/{task_id}/messages` hands a structured payload to a worker's
task mid-run. This is not chat: payloads are typed, size-capped, and
addressed to exactly one task - no freeform threads, no undeclared fan-out.

| Field | Constraint |
| --- | --- |
| `kind` | `finding`, `artefact_ref`, or `question` (closed vocabulary) |
| `body` | 4096 bytes max, DLP-redacted on the write path |
| `sender` | Worker identifier recorded in the signed entry |
| `sender_card_fingerprint` | Optional `sha256:` fingerprint of the sender's agent card |

Per task, at most 128 messages are held (`429` beyond that). Unknown kinds
and oversize bodies are rejected with `422`.

### Who may post to which mailbox

Posting is a write against the addressed task, so it carries the same task
scope as every other per-task write. The credential decides the reach:

| Credential | May post to |
| --- | --- |
| Operator bearer token, SSO user, cluster shared secret or cluster JWT | any task's mailbox |
| Agent JWT with an empty `task_ids` claim (manager / orchestrator) | any task's mailbox |
| Agent JWT scoped to specific tasks | only the mailboxes of the tasks in its own `task_ids` |

A task-scoped agent posting to a task outside its scope receives `403` and
the mailbox is not written. Fan-out between tasks is therefore an
orchestrator-level operation: a worker cannot address a sibling task's
mailbox with the session token it was spawned with. Route the handoff
through the orchestrator, or mint a token scoped to both tasks. The MCP
`bernstein_update` tool authenticates with `BERNSTEIN_AUTH_TOKEN`, so it is
unaffected.

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/<task-id>/messages \
  -H "Authorization: Bearer <token>" \
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

## BLOCKER clearance gates

The bulletin board ships a typed signal vocabulary
(`alert | blocker | finding | status | dependency`). A posted `blocker`
used to be inert: the multi-cell tick logged it and moved on, so dependent
work kept getting claimed against an unresolved blocker and no attestable
record showed which dependent tasks ran while the blocker was open.

A per-signal action registry closes that gap. Its default action is
`observe` (a no-op), so `alert` / `finding` / `status` / `dependency` are
unchanged; only `blocker` carries the `materialize_clearance_gate` action.

Posting a `blocker` deterministically projects a clearance gate into the
task graph:

```
blocker signal -> clearance task + injected depends_on edges -> resolution
```

The projection is a pure function of `(ordered bulletin journal prefix,
blocker content hash, scope)` onto a canonical `(clearance_task_id,
injected_edge_set, graph_delta_hash)` (no wall-clock, no RNG), so two
operators replaying the same journal produce byte-identical gates. The
clearance task participates as an ordinary `depends_on` edge, so the
dependency gate described above already withholds every open dependent task
in the blocker's cell until the clearance reaches a terminal cleared state.

Every projection and every resolution is sealed as a `signal.gate_projection`
receipt on the HMAC audit chain, carrying `blocker_content_hash`,
`clearance_task_id`, `injected_edges`, `graph_delta_hash`, `scope_cell_id`,
`deadline`, `resolution` (`pending` / `cleared` / `expired`), `resolver`, and
(for a resolution) the `blocker_entry_hash` of the materialization it clears.
`graph_delta_hash` is a pure function of the recorded fields, so a verifier
recomputes it byte-identically from the chain entry alone. Clearance expiry is
a deterministic function of the recorded deadline and an explicit evaluation
instant, never a wall-clock read at query time.

The gate state is a projection of those chained rows, not mutable side-table
state: strip the deterministic scheduler and the audit chain and the feature
collapses back to a logged blocker.

```python
from bernstein.core.communication.signal_actions import ClearanceGateCoordinator

coordinator = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
board.set_post_hook(
    coordinator.materialize,
    outbox_path=Path(".sdd/runtime/signal_outbox.jsonl"),   # durable retry queue
)
# ... later, when the blocker is fixed:
coordinator.resolve(clearance_task_id, resolver="operator:alex")
```

A blocker whose gate fails to materialize is **not** acknowledged: `post()`
raises `SignalActionFailure` and queues the message in the retry outbox, so a
partially materialized blocker never looks handled. Callers that post blockers
on a board with a hook wired must handle that exception (or drain the queue
later with `board.retry_pending_actions()`):

```python
from bernstein.core.communication.bulletin import SignalActionFailure

try:
    board.post(BulletinMessage(agent_id="vp", type="blocker", content=msg, cell_id=cell_id))
except SignalActionFailure:
    logger.exception("blocker posted but its clearance gate did not materialize")
```

`bernstein audit verify-gates` reconstructs, offline from the chain alone,
that no dependent task was claimed between a blocker post and its clearance,
and reports any violation with the offending task id and its claim position.
The check also runs inside `bernstein audit verify`.
