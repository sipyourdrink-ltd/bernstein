## Record model drift as a signed, chain-anchored observation

`bernstein.eval.model_drift` runs a fixed, versioned bench suite against an
admitted model on demand and emits a `DriftObservation`: the model reference,
the suite and baseline content hashes, every per-case outcome, and the
deterministic delta against the baseline. The observation is signed off the
install identity and anchored in the audit chain as a new
`model.drift_observation` record, so the series is ordered and a later edit is
visible. It embeds the run bundle and the baseline snapshot, so
`DriftObservationVerifier` recomputes the delta from the observation file alone
-- no suite file, no baseline file, no database and no adapter. A probe that
runs only part of the suite must record which cases ran and why: an observation
whose coverage label disagrees with the cases it names is refused, and a
baseline carrying no score for a case that ran is reported incomparable rather
than reduced to a delta that a subset cannot support.

(#5041)
