## `tuning.agent` and `tuning.task` reach the code that judges and kills

`defaults.override` - the path `bernstein.yaml`'s `tuning:` block takes - rebinds
the module attribute rather than mutating the frozen singleton, and it runs long
after the consumer modules import. Six modules on the kill path had bound
`AGENT` / `TASK` with `from bernstein.core.defaults import ...` at import time, so
they held a permanent snapshot of the shipped defaults: a configured
`liveness_grace_s`, `escalation_sigterm_s`, `heartbeat_starting_timeout_s`,
`scope_timeout_s` or `xl_timeout_s` reached `config_snapshot.json` and no
decision. Measured: with `liveness_grace_s: 600` set, the liveness judge logged
`grace_s=90 verdict=DEAD` and four healthy agents were sent SIGTERM at 122-129s
of quiet. All of these now resolve through the module on every call. Two
thresholds additionally gained a floor so that lowering a tunable can never
judge an agent dead sooner than the shipped defaults do (#5381).
