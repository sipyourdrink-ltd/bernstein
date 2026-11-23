## Record token usage and cost for completed openai_agents tasks

The `openai_agents` runner now extracts `context_wrapper.usage` and Responses
API token breakdowns (`cached_tokens` / `reasoning_tokens`), streams per-turn
usage deltas to the `.tokens` sidecar without double-counting, and reconciles
prompt and completion token counts into `collector.task_metrics` (#8077).
