## Spawned Letta Code agents start again

The `letta_code` adapter passed `--new-agent --conversation <id>` on every
spawn. Letta Code refuses that pair ("--conversation cannot be used with
--new-agent") and exits 1 before the prompt runs, so every spawned Letta
agent failed at once. `--conversation` also names an existing Letta
conversation, not a label. The adapter now passes `--new-agent` alone. The
new agent's conversation id is still read from the stream's envelope, and the
memory export keeps its per-session file name. Letta Code gets a contract
(`tests/contract/contracts/letta_code.yaml`) (#6588).
