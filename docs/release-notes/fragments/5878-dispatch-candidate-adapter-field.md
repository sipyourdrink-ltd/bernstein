## Dispatch candidates now carry the real adapter name

`build_dispatch_candidates` read a task's adapter override off a nonexistent
`adapter` attribute instead of the actual `cli` field on `Task`, so
`DispatchCandidate.adapter` was always empty. Because the knob-matrix resolver
gates batch lane and prompt-cache warm-up on the adapter name, this silently
kept every task on the interactive lane with no cache warm-up, regardless of
what the pinned knob matrix granted. Dispatch candidates now read the task's
`cli` field (falling back to the run's effective adapter when a task has no
per-step override), so batch-capable and cache-capable adapters get the lane
and cache economics the matrix actually declares for them (#5878).

**Operator impact:** this is the first time the #2519 knob matrix's batch
lane and cache warm-up economics actually engage in production -- they were
dead code before this fix. On the first tick after upgrading, batch-eligible
tasks on a batch-capable adapter (e.g. Claude) start resolving the discounted
`LANE_BATCH` rate instead of interactive, and cache-capable adapters may start
issuing warm-up calls; expect a shift in per-tick cost and latency profile for
that traffic, sized by the multipliers the matrix already declares for your
configured models. If you tuned `cost_policy.knobs` against the previously
inert batch/cache paths, re-check those multipliers now that they take
effect.
