# Durable work ledger

Run state (WAL, per-step journal, worktrees) is machine-local. A crash
mid-run is recoverable on the same host, but as goals stretch from minutes
to days, state that dies with the host becomes the limiting factor: an
in-flight goal could not move to another machine, be handed to a colleague,
or survive a reimage. The durable work ledger closes that gap.

```
bernstein ledger verify <run-id>
bernstein ledger anchor <run-id>
bernstein ledger fetch  <run-id>
bernstein ledger resume <run-id>
bernstein ledger runs
bernstein ledger gc     <run-id>
```

## What the ledger is

A hash-chained, append-only JSONL record of the task graph and every state
transition, stored per run under `.sdd/runtime/ledger/<run-id>/`. Each
entry links the previous entry's hash:

```
entry_hash = SHA256(canonical_json({
    "kind":      "task.completed",
    "payload":   { ... redacted ... },
    "prev_hash": "<entry_hash of entry N-1, or 64 zeros for genesis>",
    "task_id":   "t1",
}))
```

`canonical_json` is sorted-keys, compact-separators, UTF-8 -- the same
contract the per-step replay journal uses, so one verifier walks both
chains. `seq`, `ts`, `redactions`, and `schema_version` are row metadata
and never enter the hash.

Well-known transition kinds: `run.open`, `run.resumed`, `run.closed`,
`task.scheduled`, `task.started`, `task.completed`, `task.failed`,
`task.abandoned`. Unknown kinds are carried through replay untouched, so
a ledger written by a newer scheduler still resumes on an older install.
The ledger is the shared resumable-state substrate: schedulers and
long-horizon runners record into it rather than inventing new state files.

### Secrets never become portable

The ledger is designed to travel, so the redaction layer runs *before* an
entry is hashed or written: every string in a payload passes through the
secret scrubber (API keys, tokens, bearer headers, URL credentials). The
hash is computed over the redacted payload, so the portable chain verifies
everywhere without ever carrying cleartext.

## Anchoring: the chain travels with the repo

`bernstein ledger anchor <run-id>` verifies the chain end to end, then
publishes it to a dedicated side ref:

```
refs/bernstein/work-ledger/<run-id>
```

The anchor tree contains the canonical lines chunked into
`chunk-<n>.jsonl` blobs plus a `LEDGER.json` metadata file
(`{run_id, head_hash, entry_count, chunk_count, format_version}`). The
tree carries no timestamps: two operators anchoring the same chain produce
the byte-identical tree, so the tree sha is the anchor's verifiable
identity. Each anchor is mirrored into the HMAC audit chain as a
`work_ledger.anchor` event, and `git push origin
'refs/bernstein/work-ledger/*:refs/bernstein/work-ledger/*'` shares it.

A broken chain is never anchored. A torn trailing line (crash mid-write)
is excluded exactly like writer recovery excludes it.

## Crash recovery (same host)

A hard kill mid-write leaves at most a torn trailing line. On the next
open, recovery revalidates every entry hash from genesis, drops the torn
tail, and continues from the last verified entry. Interior corruption
(a tampered or edited row followed by valid rows) fails closed with the
exact position named -- the chain refuses to extend a poisoned anchor.

```
bernstein ledger verify goal-42
bernstein ledger resume goal-42
```

`resume` rebuilds scheduler state by replaying the verified chain --
completed, in-flight, scheduled, and failed tasks -- then records the
resume as a new chain entry and writes one signal per frontier task into
`.sdd/runtime/resume/`, the same watch directory `bernstein resume` uses
for checkpoint resumes. Completed work is never redone: it is part of the
verified chain.

## Machine migration

On the original host (or from its remote), anchor and push:

```
bernstein ledger anchor goal-42
git push origin 'refs/bernstein/work-ledger/goal-42:refs/bernstein/work-ledger/goal-42'
```

On the new machine:

```
git clone <repo> && cd <repo>
bernstein ledger fetch goal-42     # side refs are not fetched by a default clone
bernstein ledger resume goal-42
```

`fetch` verifies the anchored chain before a byte is written locally.
`resume` verifies again end to end, replays state, and continues from the
last verified entry. Worktree contents rehydrate from committed
checkpoints -- fork-from-step on worktree commits is the existing
substrate; the ledger carries the commit hashes in its payloads.

## Handoff to a colleague

Identical to migration: anchor + push, then the colleague clones, fetches,
and resumes. The chain is the handoff document -- `bernstein ledger resume
--dry-run --json` prints the full projected state (what finished, what was
in flight, how many prior resumes) without changing anything.

## Divergence: two resumes are an error, never a merge

Every resume appends a `run.resumed` entry carrying a fresh nonce, so two
independent resumes of the same head become structurally divergent at the
very next entry: two chains extending the same parent. Wherever the two
lineages meet -- an anchor over a diverged ref, a fetch onto a diverged
local chain, a resume with both present -- the operation is refused with
the exact fork entry and both heads named:

```
work ledgers diverge at entry 6: two chains extend the same parent entry
(local head 3f9c..., anchored head a01b...). Two divergent resumes of this
run exist; refusing to resume. Inspect both chains with 'bernstein ledger
verify --json', keep exactly one lineage, and re-anchor it.
```

Resolution is explicit: pick the lineage to keep, move the other aside,
re-anchor. Nothing is merged silently.

## Repo growth

Every re-anchor adds one commit whose tree shares unchanged chunk blobs
with its parent (git deduplicates by content). For very long runs, squash
the anchor history:

```
bernstein ledger gc goal-42
```

This rewrites the ref to a single parentless commit preserving the current
anchored tree byte for byte; superseded chunk blobs become unreachable and
a normal `git gc` reclaims them.

## Verification guarantees

| Fault | Surfaces as |
|---|---|
| Byte flipped in any entry | `entry <seq> (line <n>): entry_hash mismatch` |
| Entry re-linked to skip a predecessor | `prev_hash mismatch` at the exact entry |
| Suffix rewritten and re-chained | head-hash mismatch against the anchored head |
| Crash mid-write | torn tail dropped; resume from last verified entry |
| Interior row edited after the fact | fail-closed refusal naming the position |
| Two divergent resumes | refusal naming the fork entry and both heads |

Exit codes across the group: `0` ok, `1` no ledger, `2` verification
failed, `3` divergence.
