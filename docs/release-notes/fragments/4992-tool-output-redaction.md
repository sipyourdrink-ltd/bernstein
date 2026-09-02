## Tool-output redaction on the hook path

Post-tool enforcement now runs on the `PostToolUse` hook branch. Tool input and output are inspected for secrets before the sidecar is written, audit records append to `.sdd/metrics/tool_audit.jsonl`, and dangerous patterns write a `TOOL_ABORT` signal through the existing abort chain. Closes the gap where a secret in a diff was caught by the pre-tool `check_secrets` flow while the same secret in tool output persisted verbatim. (#4992)
