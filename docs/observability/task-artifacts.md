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

The `bernstein_post_artifact` operation records evidence posted during a run; it
is distinct from a task's required deliverable. An [artifact contract](../operations/artifacts.md)
defines the output file and criteria checked when the task completes.

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

That proves the exact artifact bytes and their row/spine binding; it does not
prove that the task journal has every row it ever held. Task journals do not
currently have an external terminal-head seal, so verification outputs carry
`journal_identity: "unverifiable"` rather than laundering chain consistency
into a whole-journal completeness claim.

```bash
bernstein artifact list <task>            # every version + verify state
bernstein artifact list <task> --output-json  # the same three states as JSON
bernstein artifact show <task> <key>      # latest version + history
bernstein audit verify                    # recompute every artifact offline
```

`bernstein artifact list <task> --output-json` emits one JSON object with these
fields:

- `task`: the requested task ID.
- `artifacts`: an array of listed artifact versions. Each row has `key`,
  `version`, `type`, `link_kind` (null when not applicable), `content_hash`,
  `journal_index`, `journal_identity`, and `verified`.
- `verified`: the overall verification result; false if any listed row fails or
  if tampering hides every row. Check each row's `verified` field to identify an
  individual failure.
- `reason`: null when verification succeeds, or the first failure reason
  (including when tampering hides every row).

Exit statuses for this task-specific listing are 0 when rows are listed, 1 when
there are no artifacts and no tampering was found, and 2 when tampering hid every
row. In particular, a failed row does not make the command fail: status 0 means
the listing was rendered, not that every artifact passed. Automation should
inspect each row's `verified` value (and the top-level `verified` summary).

Without a task argument, `bernstein artifact list --output-json` is a different,
local inventory: its JSON object has only an `artifacts` array, with one entry per
lineage-spine URI and fields `uri` and `productions` (the number of productions).
It does not return task versions or verification verdicts. This inventory exits
successfully even when the array is empty; do not interpret it as a task listing.

`bernstein artifact list` with no task argument answers a different question --
the artifact keys the local lineage spines carry -- so the two listings stay
distinct under one name.

The plural `bernstein artifacts list|show` spelling still works and takes the
same arguments, but it prints a deprecation warning on stderr and is removed in
v4.0.0. See [deprecated command names](../operations/commands.md#deprecated-command-names).

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
