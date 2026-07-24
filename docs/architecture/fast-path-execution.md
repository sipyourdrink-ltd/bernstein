# Fast-path execution

Skip the LLM agent entirely for trivial, mechanically-recognizable tasks -
formatting, lint fixes, import sorting, a simple symbol rename - and run a
deterministic tool instead.

## Why

Spawning an agent for "run ruff format on these files" burns a full LLM call
for work a linter already does deterministically, in under a second, for
free. Fast-path execution classifies every task before it reaches the
spawner and routes the mechanically-obvious ones to a deterministic
executor instead of an agent.

## How it works

`classify_task()` (`src/bernstein/core/quality/fast_path.py`) runs on every
task batch and assigns one of three levels:

| Level | Meaning | What happens |
|---|---|---|
| `L0` (trivial) | Matches a deterministic pattern (formatting, lint fix, import sort, a `rename X to Y` instruction) | Bypasses the spawner; runs a fixed executor (`ruff format`, `ruff check --fix`, `ruff check --select I --fix`, or a word-boundary regex rename) directly against the task's `owned_files` |
| `L1` (simple) | Matches a lighter pattern (docstring, type hint, typo fix) or is low-complexity + small-scope with no L0 match | Still goes through an agent, but routed to the cheapest configured model instead of the default |
| `L2` (complex) | Everything else | Full LLM agent, unchanged behavior |

A task is **never** fast-pathed regardless of text match when any of these
hold: `role` is `manager`, `architect`, or `security`; `complexity` is
`HIGH`; `scope` is `LARGE`; `priority == 1`; or the task explicitly requests
`model: opus`.

`try_fast_path_batch()` only handles single-task batches classified `L0`. It
claims the task, runs the matched executor
(`RUFF_FORMAT` / `RUFF_FIX` / `SORT_IMPORTS` / `RENAME_SYMBOL`), and marks the
task complete or failed on the task server directly - the batch never reaches
`AgentSpawner`. On executor failure the task is marked failed with the
fast-path error as the reason, so it retries through the normal LLM path
rather than silently disappearing.

Every L0 execution is recorded to `.sdd/metrics/tasks.jsonl` with
`model: "fast-path"`, zero tokens, zero `cost_usd`, and an
`estimated_savings_usd` field (a flat $0.15/task estimate versus a Sonnet
call) that feeds `bernstein cost` reporting. `compute_savings_vs_opus()`
excludes fast-path tasks from the Opus-comparison savings calculation
entirely - they never priced through a model, so there is nothing to
compare against.

## Configuration

Fast-path is on by default with the built-in patterns above. To override or
disable it, add a `fast_path:` block to `.sdd/config/routing.yaml`:

```yaml
fast_path:
  enabled: true          # false disables L0/L1 classification entirely
  l0_patterns:
    - pattern: "\\b(format|formatting|black|prettier)\\b"
      action: ruff_format
      label: formatting
  l1_patterns:
    - pattern: "\\b(add docstring|missing docstring)\\b"
      label: docstring
  l1_model: haiku         # required for L1 tasks to route anywhere
  l1_effort: normal       # optional; ignored if l1_model is unset
```

`load_fast_path_config()` is called once, at orchestrator startup, if
`.sdd/config/routing.yaml` exists. `enabled: false` clears both pattern
lists, so every task falls through to `L2`. Setting `l0_patterns` or
`l1_patterns` replaces the built-in list wholesale (not a merge); a malformed
regex in an entry is skipped with a warning, not fatal to the load.

There is no hardcoded fallback model for `L1`: if `fast_path.l1_model` is
never set (no `routing.yaml`, or the key omitted), `get_l1_model_config()`
raises `ModelNotConfiguredError` rather than silently defaulting to a
specific model.

## Limitations

- `RENAME_SYMBOL` is a word-boundary regex substitution, not an AST-aware
  rename - it does not understand scope, so a common short identifier can
  match unrelated occurrences across `owned_files`.
- Classification is purely text-pattern-based (task title + description);
  there is no dry-run or confidence threshold exposed to the operator beyond
  the fixed `confidence` field recorded internally.
- L0/L1 classification only ever narrows toward the deterministic/cheap path
  when the task text matches; it never widens L2 tasks down. A high-value
  rewrite that happens to be titled "format the module" would match L0
  unless it also trips one of the exclusion rules (role, complexity, scope,
  priority, explicit `opus` request).

## Source

- `src/bernstein/core/quality/fast_path.py` - classification, executors,
  config loading, metrics recording.
- `src/bernstein/core/orchestration/orchestrator.py` - wiring at
  orchestrator startup (`load_fast_path_config`) and stats tracking.
- `src/bernstein/core/tasks/task_lifecycle.py` - calls
  `try_fast_path_batch()` before a batch reaches the spawner.
