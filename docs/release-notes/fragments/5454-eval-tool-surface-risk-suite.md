## Tool-surface risk benchmark suite and forced approval gating

Added the `tool-surface-v1` evaluation benchmark suite (`CTRL-TOOL-INVENTORY`, `ASI02`, `AST04`) with 10 synthetic MCP server fixtures. The suite scores tool servers into risk classes (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `MINIMAL`) and generates verifiable `CapabilityReceipt`s. Any server combining untrusted input ingestion, sensitive data reach, and external egress (the lethal "Risky Triple") forces an approval gate and fails closed (deny by default) when no approver is configured (#5454).
