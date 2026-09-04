## Declare which executors a repository may spawn on

`bernstein.yaml` now accepts an optional `admission:` block: ordered allow/deny
rules over the executor identity of every spawn — role, adapter, model,
endpoint base URL, sandbox tier and task type. The gate runs inside the spawner
after that identity is resolved and ahead of any process start, and it is
fail-closed: every `deny` rule is evaluated first, a subject matching no `allow`
rule is refused, and a malformed policy raises rather than admitting. A refusal
starts no agent and appends an `admission_refusal` event to the HMAC-chained
audit log; an admitted spawn records the rule id that admitted it, so both
outcomes are readable from the chain. `mode: warn` and `mode: off` admit while
still reporting the refusing rule, so a policy can be staged before it is
enforced. `bernstein admission check` prints the decision and deciding rule for
each configured role without spawning anything, exiting non-zero when a role is
refused. A config with no `admission:` block behaves exactly as before.

(#4907)
