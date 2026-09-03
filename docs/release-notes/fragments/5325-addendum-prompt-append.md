## Qwen, OpenCode, Codex, and Gemini now deliver the completion protocol

These four adapters accepted `system_addendum` -- the completion call,
heartbeat, and signal-check instructions a spawned agent needs to finish a
task correctly -- but never delivered it, so a run driven by `--cli qwen`
(or opencode / codex / gemini) never saw the protocol text the orchestrator
was waiting on. None of the four CLIs expose a separate system-prompt flag,
so the addendum is now appended to the user prompt after the task brief,
under a fixed heading, matching the fallback the other prompt-append
adapters already use. An empty addendum leaves the prompt unchanged. (#5325)
