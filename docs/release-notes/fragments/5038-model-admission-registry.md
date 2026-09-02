## Model admission recorded as audit-chain events

Admitting a model for use by an installation now appends a `model.admitted`
event to the HMAC-chained audit log, and withdrawing one appends
`model.withdrawn`. The set of admitted models is not stored anywhere: it is
recomputed by replaying those events up to a named instant, so "was this model
permitted when that artefact was produced" is answered from the record rather
than from whatever a configuration file happens to say today. Replaying the
same log at the same instant yields byte-identical state for any reader,
admissions lapse at their stated expiry with no event written at expiry time,
and a log that does not verify yields no registry at all rather than a
permissive one. Nothing in the routing path consults the registry yet. (#5038)
