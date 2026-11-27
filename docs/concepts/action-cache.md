# Action cache and replay

The WAL gives Bernstein crash recovery. The **action cache** sits one
layer above it and gives Bernstein **deterministic replay without
paying the LLM bill**. Every action - prompt, model output, tool
call, tool result - is content-addressed by `(model_id,
normalized_prompt, tool_name, tool_args)` and stored under
`.sdd/runtime/action_cache/<aa>/<rest>.bin` (the SHA-256 hex digest split after its first byte). The `ActionCache.get_or_call` wrapper looks up that key first: a hit returns the recorded result without a live call; miss behavior depends on the configured mode, as described below.

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
# `ActionCache.get_or_call` behavior:
#   record - return a hit; on a miss call live_call and write its result
#   replay - return a hit; on a miss raise CacheMiss without calling live_call
#   hybrid - return a hit; on a miss call live_call and write its result
#   off    - skip lookup, call live_call, and do not write

# List a past run's recorded actions from the cache ($0, no live calls)
bernstein cache action replay <run_id>   # add --as-json for machine output

# Inspect on-disk size and entry count
bernstein cache action stats
```

The `cache action replay` subcommand lists every cached action tagged
with the run id (model, tool, tokens, cost, output head). It does not
re-execute anything or compare against live output. This command name is
separate from `replay` mode, which controls cache lookup in `get_or_call`.

### `ActionCache.get_or_call` by mode

The wrapper attempts a lookup before deciding whether to call `live_call`.
A hit in `record`, `replay`, or `hybrid` returns the cached record without
calling the callback or writing a new record. `off` skips lookup, so every
call reaches the callback.

| Mode | Hit | Miss |
|---|---|---|
| `record` | Return cached record; no callback or write. | Call callback, then write its result. |
| `replay` | Return cached record; no callback or write. | Raise `CacheMiss`; no callback or write. |
| `hybrid` | Return cached record; no callback or write. | Call callback, then write its result. |
| `off` | Lookup is skipped, so there is no hit. | Call callback; do not write. |

In particular, `record` mode on `get_or_call` does not mean “always live”:
a repeated call with the same key can reuse the first result:

```python
calls = 0


def live_call():
    global calls
    calls += 1
    return ("answer", None, None, 0.0)


first = cache.get_or_call(model_id="opus", prompt="p", live_call=live_call)
second = cache.get_or_call(model_id="opus", prompt="p", live_call=live_call)
assert first.output_text == second.output_text == "answer"
assert calls == 1  # second call was a cache hit, even in record mode
```

This is distinct from calling `ActionCache.record` directly: `record`
constructs and returns the supplied record without looking up an existing
one. It writes in `record` and `hybrid` modes, and does not persist in
`replay` or `off` modes.

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
