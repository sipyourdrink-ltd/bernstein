## Bundles record what a verdict cost

A submission bundle reported verdicts and nothing about the tokens, money or
wall time spent producing them, so two bundles could be compared on score and
not on cost.

Each task result may now carry `cost` — `tokens`, `cost_usd`, `wall_time_s` —
and the bundle derives `total_cost`, `measured_tasks` and `cost_per_verdict`.
`bernstein bench compare` prints cost deltas beside the score deltas.

An unmeasured cost is **absent**, not zero: a run that did not measure and a run
that was free are different facts. That omission is also what keeps existing
bundles loadable — `load` recomputes `bundle_hash` over a payload containing
every task result and refuses a mismatch as tampering, so a `"cost": null`
written unconditionally would have made every bundle produced before this change
fail to load with an accusation of forgery.

Recorded costs are part of the hash the signature commits to, so editing one
after signing is caught. The derived totals are not hashed — they are read off
the rows, like `pass_rate` — so a bundle cannot disagree with itself about its
own cost.

The `--budget` gate from the same issue is not here: its refusal receipt reuses
an envelope that has not landed yet.
