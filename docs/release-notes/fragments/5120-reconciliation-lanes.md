## Reconciliation lanes are data, and bootstrap is create-if-absent

`bernstein.core.govern.lanes` adds the lane record that did not exist: a
`name`, a `selector`, a `schedule`, a `timeout_seconds`, a
`log_destination`, and a `barrier` of `per-step` or `free`. An operator
changes how reconciliation is grouped by editing a file, and that edit is
hashed and reviewable rather than a code change to a scheduler.

The barrier is why a lane is one mechanism instead of two: a canary lane
that must serialize its steps and a bulk lane where one stuck target must
not block the rest are the same runner with one flag flipped.

`lane_hash` is computed from the content, never accepted from the caller,
and a hash present in a loaded document is verified rather than trusted —
an edited lane cannot keep the identity of the one that was reviewed.
`reconcile_lanes` makes the same three-way decision `pool register`
already makes, over a whole set, so a second bootstrap against an
unchanged lane set changes nothing and says so. A lane absent from the
file is not retired: that is a decision an operator makes deliberately,
not one a bootstrap infers from an omission (#5120).
