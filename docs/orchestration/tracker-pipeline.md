# Tracker comments as a multi-agent handoff bus

## TL;DR

| Component | Purpose |
|-----------|---------|
| `PipelineConfig` | Typed view over `bernstein.yaml: orchestration.tracker_pipeline`. |
| `PipelineStage` | One role in the pipeline (claim status, success status, failure status, optional prior-role gate). |
| `ClaimLedger` | SQLite-backed distributed claim ledger with lease TTL. |
| `ClaimJournal` + `project_claims` | Opt-in signed, Merkle-chained claim journal; SQLite becomes a deterministic projection of it. |
| `make_idempotency_key` | Stable `sha256(tracker || ticket_id || role || stage || stage_attempt)`. |
| `FailurePayload` + `format_failure_comment` | Structured failure taxonomy embedded in a fenced YAML block. |
| `TrackerPipeline` | Stateless sweep loop binding the above pieces together. |
| `tracker_pipeline.handoff` hook | Lifecycle hook fired on every stage transition. |

Each specialist role (architect, backend, qa, security) reads and writes
the same tracker ticket. The tracker is the durable, audit-trailed,
human-observable substrate. No queue server, no DB, no service mesh.

## Configuration

Add a block to `bernstein.yaml`:

```yaml
orchestration:
  tracker_pipeline:
    claim_lock_ttl_seconds: 600
    concurrency:
      per_role_max_in_flight: 1
    pipeline_stages:
      - role: architect
        claim_status: ready-for-design
        success_status: design-approved
        failure_status: design-blocked
      - role: backend
        claim_status: design-approved
        success_status: code-review
        failure_status: blocked
        requires_prior_role: architect
      - role: qa
        claim_status: code-review
        success_status: qa-passed
        failure_status: qa-blocked
        requires_prior_role: backend
```

Field reference:

| Key | Type | Meaning |
|-----|------|---------|
| `pipeline_stages` | ordered list | Stage records (see below). |
| `claim_lock_ttl_seconds` | integer | Lease duration for a stage claim. Default `600`. |
| `concurrency.per_role_max_in_flight` | integer | Max live claims per role across trackers. Default `1`. |

Stage record fields:

| Key | Type | Meaning |
|-----|------|---------|
| `role` | string | Bernstein role prompt name. |
| `claim_status` | string | Tracker status the stage claims from. |
| `success_status` | string | Target status on success. |
| `failure_status` | string | Target status on a non-transient failure. Transient failures return the ticket to `claim_status`. |
| `requires_prior_role` | string (optional) | Stage may only claim if a structured success block from this role appears on the ticket. |

## Distributed claim ledger

The ledger lives at `.sdd/state/tracker_claims.db` (override with
`--state-root`). Rows are keyed by `(tracker, ticket_id, role)`. The
schema is created on first use.

Two agents racing for the same `(tracker, ticket_id, role)` produce
exactly one `INSERT OR FAIL` success. The losing caller learns the
holder's `claimer_id` and skips the ticket on the current tick. A
crashed worker's claim ages out after `claim_lock_ttl_seconds` and the
next caller picks it up.

The ledger also enforces `per_role_max_in_flight`: when a role's live
claim count reaches the ceiling, the loop stops dispatching new claims
for that role until somebody releases.

## Signed claim journal (opt-in projection)

The SQLite ledger is race-safe only when every claimer shares one
filesystem. For leaderless coordination the ledger can instead be driven
by a **signed, append-only, Merkle-chained claim journal** (`ClaimJournal`),
with the SQLite rows demoted to a deterministic *projection* of that
journal rather than the source of truth.

The path is **opt-in and off by default**. Existing callers construct
`ClaimLedger(db_path)` and touch no journal — behaviour is unchanged.
Passing a journal (`ClaimLedger(db_path, journal=...)`) turns on the
journal path.

| Piece | What it is |
|-------|------------|
| `ClaimReceipt` | One signed entry: `claim` / `release` / `renew` / `expire` / `supersede` / `fork`, binding `{tracker, ticket_id, role, claimer_id, node_id, lease_expires_at, prev_entry_hash, entry_hash}`. |
| `ClaimJournal` | Append-only JSONL store. Each receipt chains over the previous head and is Ed25519-signed with the node install identity; each is anchored into the HMAC audit chain (`cluster.claim_journal_receipt`). |
| `project_claims(receipts)` | Pure fold: the same ordered receipt set yields a byte-identical `ClaimState` and identical head hash on any node. |

**Determinism.** The `entry_hash` is computed with the signature blanked,
so two nodes signing the same body with different install keys still agree
on the hash and therefore on the journal head. When more than one live
claim contends for a key, the claim with the lowest `entry_hash` wins and
the losers are superseded — a total order independent of wall-clock, node
identity, or observation order.

**Convergence.** `ClaimJournal.reconcile(...)` appends a chain-anchored
`claim_superseded` receipt for every loser, naming the winner. Re-running
once every loser is superseded is a no-op.

**Verify.** `ClaimJournal.verify()` replays the journal offline, checking
every `prev_entry_hash` link, every recomputed `entry_hash`, and every
Ed25519 signature; a single flipped byte or a dropped receipt fails at the
exact entry index. Passing `chain=` also re-checks that every receipt is
anchored in the HMAC audit chain, which catches a journal that is
internally consistent but was never anchored. `bernstein cluster claims
verify` is the operator entry point — see
[`docs/cluster/deployment-patterns.md`](../cluster/deployment-patterns.md).

**Gossip and forks.** `ClaimJournal.ingest(receipt, ...)` folds a peer's
receipt only after its signature and its recomputed hash verify, and only
when its `prev_entry_hash` extends the local head. A receipt that does not
extend the head is **not** merged: it appends a signed `fork` receipt
carrying the divergence entry index, which `verify()` reports through
`ClaimJournalVerifyResult.forks`. Use `.clean` for "intact *and* not
forked"; `.ok` alone means only that the file itself is intact.

**Referenced data vs. signer identity.** A receipt's `claimer_id` /
`node_id` always name its signer. What it speaks *about* is carried
separately: `supersedes` (plus `superseded_*`) for a supersession,
`target_entry_hash` for the claim a `release` / `expire` retires, and the
`fork_*` fields for a divergence. That is what lets one node retire a
peer's expired lease without a verifier ever seeing a receipt signed by a
key that does not match the identity it declares.

**Schema versioning.** `CLAIM_JOURNAL_SCHEMA_VERSION` is stamped on every
receipt, and the signing payload projects away fields introduced *after* a
receipt's own version. An append-only journal written by an earlier release
therefore keeps verifying byte-for-byte across an upgrade instead of
reporting tamper on entries nobody touched.

**Multi-writer safety.** Each append resolves the chain tail and writes the
new receipt as one critical section under a single exclusive advisory lock
on the journal file. Two nodes appending to the same journal on a shared
filesystem are therefore serialised across the read-modify-write of
`prev_entry_hash`, so concurrent honest claims stay on one linear chain
instead of forking — a fork that offline `verify()` could not distinguish
from tampering. Convergence between the concurrent claimants is then the
ordinary lowest-`entry_hash` rule plus `reconcile(...)`.

**Journal is the source of truth.** On the journal path a granted claim
mints the signed receipt first and materialises the SQLite row *from that
receipt* inside the same transaction. If the receipt cannot be written
(disk or signing error) the transaction is rolled back, so the ledger never
holds a row without a backing receipt — the projection can always be
rebuilt from the journal alone.

Deferred (not in this slice): MESH topology wiring in the orchestrator,
A2A gossip of receipts between machines, fork detection at merge time, and
the `bernstein cluster claims log | head | verify` CLI. Those land once the
verified peer-identity surface is in place.

## Idempotency keys

Every tracker write carries a stable key:

```text
sha256(tracker || "\x1f" || ticket_id || "\x1f" || role || "\x1f" || stage || "\x1f" || stage_attempt)
```

The pipeline derives the key inside `_process_ticket` and threads
`<key>:comment` and `<key>:transition` into the adapter calls. Adapters
that honour HTTP `Idempotency-Key` headers reuse the same key; adapters
that need an in-comment fingerprint embed the key in the structured
block.

## Structured failure taxonomy

Every failure-side stage transition writes a comment with a fenced
block:

````text
qa noticed three flaky cases.

```yaml bernstein:failure
role: "qa"
stage_attempt: 1
idempotency_key: "<sha256-hex>"
reason_code: "tests.failed"
category: "transient"
transient: true
next_action: "retry"
detail: "3 cases red"
```
````

Allowed categories: `transient`, `permanent`, `policy`, `unknown`.
Allowed `next_action`: `retry`, `escalate`, `abandon`, `manual`.

`parse_failure_block(comment_body)` lifts the block back into a Python
dict so downstream automation does not need to re-implement YAML
parsing.

The success path writes a symmetric ``bernstein:success`` block with the
role's free-text summary so handoff consumers recognise a clean
transition without re-parsing prose.

## Lifecycle hook

On every stage transition the pipeline fires the
`tracker_pipeline.handoff` event through any `HookRegistry` instance
attached on the `TrackerPipeline`. The payload keys:

* `handoff_event_name`: always `"tracker_pipeline.handoff"`.
* `tracker`, `ticket_id`, `role`.
* `from_status`, `to_status`.
* `stage_attempt`, `outcome` (`"success"` or `"failure"`),
  `idempotency_key`.

Operators wire metrics dashboards, escalation rules, and tracker
mirroring through this single seam.

## Worked example: architect -> backend -> qa

```python
from pathlib import Path

from bernstein.core.lifecycle.hooks import HookRegistry
from bernstein.core.orchestration.tracker_pipeline import (
    build_pipeline_from_yaml,
)

raw = {
    "pipeline_stages": [
        {
            "role": "architect",
            "claim_status": "ready-for-design",
            "success_status": "design-approved",
            "failure_status": "design-blocked",
        },
        {
            "role": "backend",
            "claim_status": "design-approved",
            "success_status": "code-review",
            "failure_status": "blocked",
            "requires_prior_role": "architect",
        },
        {
            "role": "qa",
            "claim_status": "code-review",
            "success_status": "qa-passed",
            "failure_status": "qa-blocked",
            "requires_prior_role": "backend",
        },
    ],
    "claim_lock_ttl_seconds": 600,
    "concurrency": {"per_role_max_in_flight": 1},
}

# trackers and dispatcher are supplied by the orchestrator; see
# bernstein.core.trackers.contract for the adapter contract.
pipeline = build_pipeline_from_yaml(
    raw,
    trackers=trackers,        # mapping name -> AbstractTrackerAdapter
    dispatcher=dispatcher,    # supplies role execution
    state_root=Path(".sdd"),
    hook_registry=HookRegistry(),
)

# One non-blocking sweep across configured trackers. Schedule via cron,
# systemd, or `bernstein daemon`.
pipeline.tick()
```

## CLI

| Command | Purpose |
|---------|---------|
| `bernstein pipeline run --dry-run` | Print resolved pipeline without dispatching. |
| `bernstein pipeline run` | One non-blocking sweep across configured trackers. |
| `bernstein pipeline status` | Print live (non-expired) handoffs from the SQLite ledger. |
| `bernstein pipeline status --as-json` | Machine-readable output for dashboards. |

Per-tracker filtering is not exposed on the CLI yet: the dispatch
wiring lives in `build_pipeline_from_yaml` plus the tracker adapter
registry, which the CLI does not yet drive. Construct the pipeline
programmatically with a single-entry `trackers` mapping until the
registry wiring ships.

## What is deliberately out of scope

* Tracker adapter implementations themselves (separate per-tracker tickets).
* Webhook ingestion (separate ticket).
* Auto-discovery of pipeline shape from a tracker workflow.
