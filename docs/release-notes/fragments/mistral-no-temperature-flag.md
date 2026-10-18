## The mistral adapter no longer claims vibe takes a temperature

The `mistral` adapter declared temperature support and appended
`--temperature <t>` to `vibe`. vibe (2.26) has no such flag. It answers
"unrecognized arguments: --temperature" and exits 1, so a task that asked
for a temperature passed the sampling gate and then failed at launch. The
adapter now declares no sampling capability. The gate refuses a temperature
request before spawn and names the reason, and the argv never carries it.
Mistral Vibe gets a contract (`tests/contract/contracts/mistral.yaml`).
