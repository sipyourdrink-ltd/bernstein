# Conduct scope comparison boundary

`core.replay.conduct_fold` remains a pure supplied-record projection. The
aggregate's additive `scope_comparison` section joins caller-verified activity
scope evidence with the caller-supplied effective period scope. The small
`ConductScopeActivity` envelope carries run/principal identity, recorded activity
name, original source range and an optional existing `DelegationScope`.

Authority rules are not copied: comparison delegates to the existing narrowing
implementation. No scope is reconstructed from counters or prose. Missing
projection/evidence is unavailable, and mixed known/missing evidence is partial.
The declared scope body/reference, divergent axes, recorded scope and original
source citations let readers inspect exactly what was compared.

Callers own verified source records, attribution and period authority resolution;
this layer neither creates authority nor claims chain verification. One supplied
projection per run is required when comparison is requested. Per-run projection
serialization and aggregate counts/coverage/window membership remain unchanged.
