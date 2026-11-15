# Remote bridge observation capability status

The optional observation API in `bernstein.bridges.base` is a typed component
for bridge implementers. It introduces no orchestrator call path or operator
command. Existing OpenClaw behavior and configuration remain unchanged.

A concrete bridge may implement `remote_status(handle)` and
`cancel_remote(handle)`. The base methods refuse with `BridgeCapabilityError`
and name the unsupported capability. A refusal does not invoke legacy
`status`, `cancel`, `spawn`, `logs` or `name`.

An observed `unknown`, `input-required` or `auth-required` state remains
nonterminal. Cancellation outcomes distinguish `unconfirmed`, `unknown` and
`confirmed`. The types record observations; a terminal state or cancellation
outcome alone establishes no local acceptance, delivery, authorization,
fallback permission or closure. Legacy cancellation returning `None` retains
its original meaning.

There is no current observation/journal consumer for this component, so it
does not establish remote execution readiness or full #6992 slice acceptance.
No new service or credential is required by these types. Event journaling,
reattachment, approval handling, artifact intake, time limits and fallback
remain separate integration work.

The [architecture contract](../sdd/remote-bridge-observation-contract.md) records
the exact state vocabulary, immutable identity binding, timestamp validation
and the decision dissent.
