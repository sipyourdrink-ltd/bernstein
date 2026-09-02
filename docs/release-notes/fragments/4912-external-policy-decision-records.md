## Record every external policy evaluation in the audit chain

`PolicyHookRegistry` now accepts an audit chain and appends one
`external_policy.decision` record per hook evaluation — allow, deny, abstain and
unavailable alike. Each record carries the engine name, the SHA-256 of the policy
that decided, the request's identifying fields, the verdict, the measured
latency, and a digest of the engine's error output. `UNAVAILABLE` is recorded as
itself rather than folded into `abstain`, so an operator can show offline that a
run stopped because a named policy engine could not answer, at a named chain
position. `OPAHook` now stamps its responses with the policy file's digest read
at evaluation time. Recording is opt-in; a registry without a chain decides as
before.

(#4912)
