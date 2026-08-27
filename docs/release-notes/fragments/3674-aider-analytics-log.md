## Wire --analytics-log into aider spawn command

The aider adapter now writes a JSONL analytics file per spawn to `.sdd/runtime/analytics_<session_id>.jsonl`. This enables tracking of aider session analytics for debugging and optimization. Closes #3674.
