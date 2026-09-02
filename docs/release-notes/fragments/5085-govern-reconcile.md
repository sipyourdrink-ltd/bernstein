## `bernstein govern reconcile --propose` (Issue #5085)

An operator who declared what should be installed had no way to ask whether it
still is. `bernstein govern reconcile --propose` enumerates the governed surface
-- registered adapters, cost lanes, scheduled tasks, declared capability entries
-- into a snapshot stamped with one `observed_at`, diffs it against a
desired-state document, and writes the result as one anchored governance decision
record.

The run mutates nothing else. No entity is added, removed, or mutated, so the
diff stays a reviewable artefact rather than a change that already happened.

Each entity classifies as `unchanged`, `new`, `changed`, `declared_but_absent`,
or `present_but_undeclared`, and the desired-state document's `prune` and
`self_heal` flags decide what is proposed for it. An entity that is present but
undeclared under `prune: false` is a `hold` finding, never a queued removal --
nothing is destroyed because a document forgot to mention it.

`new` is measured against the previous run's own record rather than a side file,
so a consecutive run over an unchanged environment reports nothing and exits `0`;
`--full` prints the whole state. Drift exits `2` with one verdict line per
drifted entity.
