## Inspect recorded governance decisions

Installation operators can load a run's recorded decisions through read-only
list and detail endpoints and expand their verdict, inputs, policy version
and journal anchor. Budget values carry provenance and explicit `verified`,
`unavailable` or `mismatch` states using the existing verifier; prior spend is
reported to the six decimal places bound by the input hash and withheld when the evidence is unavailable or mismatched. The read does not
verify the journal anchor or recompute verdicts in the browser (#5070).
