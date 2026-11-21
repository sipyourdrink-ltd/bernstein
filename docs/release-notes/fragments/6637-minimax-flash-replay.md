## MiniMax Flash estimates and tool continuations stay model-specific

Direct MiniMax text runs now estimate MiniMax-M3.1-Flash at its own published
rate instead of falling through to the broader M3 entry. Compatible
openai-agents SDKs also retain same-model MiniMax reasoning across tool calls;
older SDKs without that replay-policy hook stop before dispatch rather than
silently dropping reasoning history (#6637).
