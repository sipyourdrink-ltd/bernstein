# Task artifacts and chain-computed progress

A worker in the middle of a run can attach the substance of its work to its own
task, and every task carries a progress number that cannot be inflated, only
earned. Both signals come out of the run's own chained records, never out of the
agent's account of itself.

## Two surfaces

| Surface | What it is | How it is trusted |
|---|---|---|
| Artifact channel | Agent-posted reports, tables, and links | Content-addressed, spine-sealed, journal-anchored, audit-mirrored |
| Progress vector | How far along a task is | A pure projection of journaled work; never postable |

## Artifact channel

A worker posts an artifact with the `bernstein_post_artifact` MCP tool, scoped so
it can attach artifacts only to a task whose claim it holds. Artifacts are typed:

- `report` -- a markdown body.
- `table` -- columns plus rows.
- `link` -- a URL with a declared kind (`preview`, `dashboard`, `document`).

Posting stores the payload content-addressed in the evidence store, appends an
`artifact_posted` row to the task's Merkle-chained journal carrying the content
hash and key, seals the canonical record into the lineage spine, and mirrors it
into the HMAC audit chain. The artifact's identity is its spine entry hash; the
record is the receipt.

Reposting an existing key appends a new version whose record references the prior
version's hash, so history is a chain, never an overwrite. Rendering always
re-checks the stored blob hash against the journal row: a tampered blob displays
as tampered, not as content.

```bash
bernstein artifacts list <task>          # every version + verify state
bernstein artifacts show <task> <key>    # latest version + history
bernstein audit verify                   # recompute every artifact offline
```

`bernstein audit verify` walks every artifact row, recomputes the stored blob
hash, the spine anchor, and the chain mirror. Flipping one byte in a stored blob
or its journal row fails verification, naming the artifact key and the exact
journal position.

## Progress vector

There is deliberately no progress artifact type and no input that sets a
percentage. Progress is a pure projection folded from rows that only real work
produces: checkpoint references in the journal, evidence producers declared
versus passed, diff and gate attempts, and task transitions in the work ledger.

The projection excludes wall clock, has canonical bytes and a stable hash, and is
exposed on the task detail API, over SSE, and inside the MCP task handle
projection. Projecting the same run twice -- including on a second host after a
ledger resume -- yields byte-identical canonical bytes and the same hash. A
worker moves the number only by doing journaled work: posting fifty reports while
checkpointing nothing leaves the vector unchanged.

## Delivery

New `task.artifact` and `task.progress` SSE event types stream over the existing
bus, and an Artifacts panel plus a progress strip render on the task detail
screen. A posted artifact appears without a reload, bound to the exact run
position that produced it.
