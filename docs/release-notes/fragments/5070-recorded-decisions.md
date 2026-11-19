## Inspect recorded governance decisions

The Governance screen can load a run's recorded decisions through read-only
list and detail endpoints and expand their verdict, inputs, policy version
and journal anchor. Budget values carry provenance and explicit `verified`,
`unavailable` or `mismatch` states using the existing verifier; prior spend is
withheld when it cannot be matched to the decision inputs. The read does not
verify the journal anchor or recompute verdicts in the browser (#5070).
