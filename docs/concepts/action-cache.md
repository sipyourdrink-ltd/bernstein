# Action cache and replay

The WAL gives Bernstein crash recovery. The **action cache** sits one
layer above it and gives Bernstein **deterministic replay without
paying the LLM bill**. Every action - prompt, model output, tool
call, tool result - is content-addressed by `(model_id,
normalized_prompt, tool_name, tool_args)` and stored under
`.sdd/runtime/action_cache/<aa>/<rest>.bin` (the SHA-256 hex digest split after its first byte). On replay, cache hits
return the recorded result; misses fall through to the live model and
append.

## Why it exists

Two scenarios drove this:

1. **CI re-runs.** The same self-evolving smoke test runs on every PR.
   Without a cache, each run pays full LLM cost.
2. **Regression bisecting.** Verifying that a fix doesn't break a
   known-good path is impossible if every re-run costs money and
   produces non-deterministic output.

The cache also produces a deterministic golden record we diff against
to catch silent agent-output drift between model versions.

## How to use it

`tuning.action_cache.mode` sets the default mode for `open_cache()`
callers that do not pass one (today only the MCP gateway's read-only
lookup). The `cache action` subcommands ignore it: `stats` opens the
cache in `off` mode and `replay` in `replay` mode. No shipped run path
records actions into the cache yet; callers that want record/replay wrap
their own calls with `ActionCache.get_or_call`.

```bash
# Mode is set in bernstein.yaml under `tuning:` (see Configuration):
#   record - always live, append every call to the cache
#   replay - cache-only, fail-loud on cache miss instead of calling the model
#   hybrid - replay on hit, fall through to live model on miss, append result
#   off    - no lookups or writes

# List a past run's recorded actions from the cache ($0, no live calls)
bernstein cache action replay <run_id>   # add --as-json for machine output

# Inspect on-disk size and entry count
bernstein cache action stats
```

The `cache action replay` subcommand lists every cached action tagged
with the run id (model, tool, tokens, cost, output head). It does not
re-execute anything or compare against live output.

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `tuning.action_cache.enabled` | `true` | Master switch. |
| `tuning.action_cache.mode` | `hybrid` | `record` / `replay` / `hybrid` / `off`. |
| `tuning.action_cache.size_mb` | `500` | LRU eviction cap. |

Metrics:

- `bernstein_action_cache_hits_total{model}`
- `bernstein_action_cache_savings_usd_total{model}` - estimated token-cost saved
  by replay hits.

## Limitations

- Exact-key only. No semantic-similarity match across "almost the
  same" prompts.
- Bash / exec tool calls have side effects we cannot replay (file
  writes, network calls). The cache covers the LLM and read-only tool
  layer; bash is recorded but not replayed.
- Single host. Cross-machine cache sharing rides on
  `core/storage/sink.py` plumbing if you need it.
- Replay is byte-comparison strict. A different timestamp in a
  recorded log is a "drift" finding even if functionally equivalent.

## Cache policy engine

`derive_key` here is input-only and intentionally survives Bernstein code
changes. When a task declares a `cache_policy` on its spec, its cache decisions
route through the shared [cache policy engine](cache-policy.md): keys are
composed over an ingredient recipe that also folds in the model version, adapter
version, base worktree commit, and tool schema hash; staleness becomes a pure
drift verdict over repo state; and eviction is transitive over `served_from`
lineage edges. Tasks with no declared policy behave exactly as described above.

## Related

- Source: `src/bernstein/core/persistence/action_cache.py`
- Layered on: `src/bernstein/core/persistence/fingerprint.py` (memo
  store)
- Policy engine: `src/bernstein/core/persistence/cache_policy.py`
- CLI: `src/bernstein/cli/commands/cache_cmd.py`,
  `replay_filter_cmd.py`
- PR #999
