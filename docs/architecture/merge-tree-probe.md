# Merge-tree integration probe

A deterministic, content-addressed answer to one question: **what do two live
worker commits compose to, right now?**

This page describes the probe primitive
(`bernstein.core.git.merge_tree_probe`). It is step 1 of issue #3279 and is
deliberately not yet wired into scheduling — see
[Status](#status-what-exists-today) below.

## The gap it closes

The existing pre-merge check, `detect_merge_conflicts` in
`core/git/merge_queue.py`, compares **one finished branch against the base**,
and it runs on the gate path — after the agent has already finished. The merge
queue then serialises merges FIFO, so by the time worker B's branch is checked,
worker A has already landed and B has already spent its entire run. The check
sits after the point where its answer could have changed anything.

Nothing looks at a *pair* of workers while both are still moving. Two workers
can each be individually mergeable into the base at the moment they are
admitted, and mutually unmergeable ten commits later. The first evidence that
two worktrees stopped composing is a conflict raised at merge-back, when the
cheapest available action is to discard one of the runs.

The probe covers the interval between spawn and merge-back.

## What the probe does

For an ordered pair of live worker commits:

```
git merge-tree --write-tree --name-only -z <a_commit> <b_commit>
```

Two outcomes, both content-addressed:

| Outcome | Exit | Result |
|---|---|---|
| Clean | `0` | the merged tree object id |
| Conflicting | `1` | the tree id of the conflicted result, plus the conflicted paths |

No working tree, index, or branch is touched.

### Why the tree id is the point

`--write-tree` does not return a *report* about a merge. It returns **the merge
itself, named by its content**. Two parties who probe the same pair either
produce the same object id or do not have the same repository.

That is what makes the result worth signing (step 3). A third party holding the
repository and stock git checks out the two recorded commit ids, runs the
recorded command under the recorded git version and merge configuration, and
compares the tree id. Equal means the probe told the truth about what those two
worktrees composed to at that moment. There is no bernstein-shaped step in the
middle of that argument.

### Re-deriving a recorded probe

Given a probe's `a_commit`, `b_commit`, `git_version`, and
`merge_config_digest`, with no bernstein installed:

```bash
git --version                       # must match the recorded git_version
git merge-tree --write-tree --name-only -z <a_commit> <b_commit>
```

The first NUL-separated field is the tree id; compare it to `tree_id`. The
fields up to the empty terminator are the conflicted paths; sort them and
compare the digest. Everything after the terminator is git's human-readable
narration — it is prose, it is not parsed, and it never reaches a digest.

### Why `-z`

Without it, git C-quotes any path containing a space, a quote, or a non-ASCII
byte. A recorded digest would then cover the *quoted* form, and a verifier
would have to reimplement git's quoting rules to check it. With `-z` the paths
are raw bytes and the split is unambiguous.

## Degraded modes

Stated in the result rather than papered over.

**Textual only.** A clean probe proves the two trees compose *textually*. It
says nothing about a signature change in one worktree breaking a call site in
the other. The verdict is spelled `TEXTUAL_CLEAN`, never `SAFE` — no such
member exists on the enum, so no caller can spell a claim stronger than what
was measured. No scheduling decision may treat a clean probe as a correctness
guarantee.

**Committed state only.** Uncommitted work in a worktree is invisible to the
probe. A caller that records the last probed commit per task makes the unprobed
window between it and merge-back explicit rather than assumed empty.

**git < 2.38.** `--write-tree` does not exist there, so probing is off and the
verdict is `UNAVAILABLE`. There is deliberately no fallback to the old
positional `git merge-tree <base> <ours> <theirs>` form: its output is a
diff-like text stream rather than a stable artefact, and an unverifiable
fallback would be worse than no probe.

**Merge configuration drift.** `.gitattributes` merge drivers and the
rename-detection knobs change what a merge produces, so `merge_config_digest`
binds them into the result. It covers every tracked `.gitattributes` by content
(a driver in a subdirectory changes results just as much as one at the root)
plus `merge.renames`, `merge.renameLimit`, `diff.renames`, `diff.renameLimit`,
and `merge.conflictStyle`. A recorded probe stays re-derivable only under its
own recorded digest. A verifier seeing a changed digest must report
configuration drift, **not** tampering.

**Failure is a verdict, never an exception.** Unresolvable commits, unrelated
histories, a missing git binary, and timeouts all return `UNAVAILABLE` carrying
a `reason`. `UNAVAILABLE` is a distinct member from `TEXTUAL_CLEAN` precisely
so a probe that could not run is never mistaken for a probe that found nothing
wrong.

## Cost

The probe mutates no working tree, no index, and no branch — but `--write-tree`
**does write tree objects into the object database**. Under a per-commit probe
cadence that is loose-object growth. Bounding it is the job of the changed-path
gate and the per-run probe budget in step 2; exhausting that budget is meant to
be a recorded fact, not a silent skip.

## Ordering

The pair is *ordered*. `(a, b)` and `(b, a)` are distinct probes: the two sides
map to ours/theirs, so conflict output can differ between them. Callers must
fix the order from a canonical source — chain order, in step 2 — rather than
from arrival order, which is a race.

Branch names given to the probe are resolved to object ids before being
recorded, so a recorded probe names immutable commits rather than refs that
later move.

## Status: what exists today

Step 1 only. The probe is a pure primitive: it is not wired into scheduling, it
emits no chain entry, and nothing in the tree calls it yet.

Still to come, each a separate change:

| Step | Scope |
|---|---|
| 2 | Derive the probe set from the chain prefix; gate pairs on changed-path intersection |
| 3 | Emit the probe as a signed, hash-linked audit-chain entry, projected into the run's lineage record |
| 4 | Pure `(verdict, chain state)` → `CONTINUE` / `QUIESCE_LATER` / `SERIALISE_REMAINDER`, applied to the later-in-chain task |
| 5 | `bernstein audit verify` re-runs every recorded probe and fails on a tree-id mismatch |

Related: [why deterministic](WHY_DETERMINISTIC.md) for why no model sits in the
scheduling loop — the absence of a deterministic branch-compatibility signal is
why the drain path currently reaches for one.

## Testing

```bash
uv run python scripts/run_tests.py -k merge_tree
```

The tests run against a real `git` binary in `tmp_path` and never mock git
plumbing: the whole point of the probe is that its output is what stock git
actually produces, so a stubbed subprocess would assert nothing worth knowing.
They skip rather than fail on a git older than 2.38.
