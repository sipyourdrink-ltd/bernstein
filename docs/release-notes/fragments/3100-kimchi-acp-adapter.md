## Kimchi ACP adapter

Adapters can now target Kimchi for open-weight and hosted execution. The
adapter binds the upstream `--mode acp` JSON-RPC stream onto the existing
ACP client transport, journaled content-addressed. Native session resume is
wired via `--session <path>`. #3100
