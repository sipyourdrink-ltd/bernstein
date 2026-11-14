# Cost Optimization Guide

Strategies for reducing API spend and compute costs when running Bernstein.

## Model selection

Model cost is typically the dominant expense. Bernstein selects models per-task based on complexity.

### Model tiers

| Tier | Use case | Relative cost |
|------|----------|---------------|
| Haiku / GPT-4o-mini | Simple refactors, formatting, test fixes | 1x |
| Sonnet / GPT-4o | Standard features, bug fixes, moderate complexity | 5-10x |
| Opus / o1 | Architecture decisions, complex multi-file changes | 20-50x |

### Configuring model routing

The orchestrator maps task complexity to models automatically. Default mapping (from `src/bernstein/core/defaults.py`, `PLAN.model_by_complexity`):

| Complexity | Default model |
|-----------|---------------|
| `low` | `haiku` |
| `medium` | `sonnet` |
| `high` | `opus` |

Override per-task in plan files:

```yaml
steps:
  - title: "Fix typo in README"
    complexity: low       # Routes to haiku
  - title: "Redesign auth system"
    complexity: high      # Routes to opus
```

### Cost impact of model choice

For a typical 100-task project:
- All opus: ~$50-150
- Mixed (default policy): ~$15-40
- All haiku: ~$3-8

The mixed approach usually delivers the best quality-to-cost ratio.

### MiniMax cost estimates

For new MiniMax text runs through `openai_agents`, select `MiniMax-M3.1-Flash`
with your configured MiniMax endpoint; `MiniMax-M3` remains available as an
explicit model choice. Existing saved model IDs are preserved. Direct
`https://api.minimax.io/v1` and `https://api.minimaxi.com/v1` runs retain the
model's `reasoning_content` across tool calls, alongside the assistant text,
tool arguments, and tool results.
Direct MiniMax runs require an `openai-agents` SDK that exposes the
reasoning-content replay-policy hook. When the installed SDK lacks that hook,
the runner reports `sdk_incompatible` and stops before dispatch rather than
silently dropping reasoning history.

Flash is estimated at $0.15 per million input tokens and $0.60 per million
output tokens, independently of the broader M3 model name. Its published
cache-read rate is $0.015 per million tokens; no cache-write price is assumed.
See [MiniMax's current pricing](https://platform.minimax.io/docs/guides/pricing-paygo).

The runner currently prices aggregate input/output tokens, so cached input is
still estimated at the ordinary input rate. Existing M3 estimates retain the
$0.60/$2.40 rates; they do not distinguish per-request context tiers or priority
service. Treat these values as budget estimates rather than invoice totals.

## Prompt caching

Prompt caching depends on the provider and adapter. Reuse stable role prompts
and project context where the provider supports it. For cache accounting, see
[Cost-aware scheduling](cost-aware-scheduling.md).

## Batch API usage

Enable the provider batch manager and optionally filter candidates by
case-insensitive substrings in task titles or descriptions:

```yaml
batch:
  enabled: true
  eligible: [documentation]
```

The dispatch path also checks task eligibility and adapter capabilities. See
[Batch routing](../architecture/batch-routing.md) for the route conditions.

## Run spending cap

Set `budget` to a scalar USD amount in `bernstein.yaml`:

```yaml
budget: "$50"
```

For pre-dispatch ceilings by model, pool, role, or run, see
[Cost-aware scheduling](cost-aware-scheduling.md).

### Hard cap via `--max-cost-usd`

For a hard ceiling that aborts the whole run rather than the
single offending task, use the per-run flag:

```bash
bernstein run plan.yaml --max-cost-usd 1.50
```

The flag writes its value to `BERNSTEIN_MAX_COST_USD` before
bootstrap. The orchestrator resolves the effective cap from
the following precedence chain:

1. `BERNSTEIN_MAX_COST_USD` env var (set by the CLI flag).
2. `.sdd/runtime/run_config.json` (`budget_usd`).
3. `budget` in `bernstein.yaml` (parsed into `SeedConfig.budget_usd`).
4. Default `0.0` (= unlimited).

Three thresholds fire as cumulative spend rises: warn at 80%,
critical at 95%, hard stop at 100%. On hard stop the orchestrator
drains live agents, persists state, and exits. Non-positive values
normalise to `0.0` (unlimited); invalid env values are logged at
warning level so a typo never silently disables the guard.

### Model fallback after provider errors

Configure an ordered `model_fallback.fallback_chain` for supported provider
failures. The default strike limit is 3. Connection timeouts and HTTP status
codes `429`, `503`, and `529` count by default. A run budget halt follows the
spending-cap rules above. See [Configuration Reference](CONFIG.md#model-fallback).

## Context window optimization

### Scope and file isolation

Use `scope` (`small`, `medium`, `large`) and `files` to limit context per task:

```yaml
steps:
  - title: "Add validation to User model"
    scope: small
    files:
      - "src/models/user.py"
      - "tests/test_user.py"
```

Narrow scopes and explicit file lists mean:
- Fewer files loaded into context
- Fewer tokens consumed
- Lower cost per task
- Better conflict detection (via `files` field)

### Context files

Use `context_files` in `bernstein.yaml` to list the files to include in agent
context. Keep that list focused on files the agents need for the run.

## Spend visibility

Bernstein tracks token usage and provides cost breakdowns via `bernstein cost`:

```bash
# View current run cost
bernstein cost

# View cost breakdown by model
bernstein cost --by model

# View cost breakdown by agent
bernstein cost --by agent

# View cost for last 24 hours
bernstein cost --last 24h

# JSON output for scripting
bernstein cost --json
```

Cost forecasting is handled by `src/bernstein/core/cost/spend_forecast.py` and `src/bernstein/core/cost/cost_forecast.py`.

### Run savings summary

Every `bernstein run` ends with a summary card. Since `v1.10.1` the card
includes a **Model routing savings** row that estimates how much spend
the cascade router avoided versus running the same plan single-shot
through the most expensive routed model.

```text
╭─────────────────────── Run Complete ───────────────────────╮
│ Metric                                          Value      │
│ Tasks completed                                 12/12      │
│ Total time                                      4m 18s     │
│ Total cost                                      $0.4231    │
│ Cost per task                                   $0.0353    │
│ Model routing savings                           $1.8742    │
│ Quality score                                   92%        │
╰────────────────────────────────────────────────────────────╯
```

The same number is persisted to `.sdd/runs/<run-id>/summary.json`
(`routing_savings_usd`) for `bernstein recap` and downstream dashboards.

**Baseline anchor.** The comparison model is **Opus** - the most
expensive tier in the default cascade (`sonnet → opus`). Picking
the top of the cascade gives an intentionally generous "what if I had
just used the smartest model for everything" number; the buyer-facing
question the card is built to answer.

**How it is computed.** For every completed task the run priced through
something other than Opus, multiply its total tokens (prompt +
completion) by the Opus per-1K rate, subtract the actual spend, and sum
the positive deltas. Tasks already routed to Opus, fast-path bypasses,
and zero-token tasks contribute nothing. Implementation:
`compute_savings_vs_opus()` in `src/bernstein/core/cost/cost.py`,
wrapped by `calculate_savings()` in
`src/bernstein/core/cost/savings_calculator.py`.

**Caveats.**

- This is an **estimate**, not a counterfactual replay. Opus would have
  used a different prompt strategy and likely a different token count;
  the number assumes identical token usage across tiers.
- Savings can read as zero on a run that was already 100% Opus or 100%
  fast-path - there is nothing to compare.
- Token rates come from the static catalogue in
  `_MODEL_COST_USD_PER_1K`. When provider pricing changes, the catalogue
  needs to move with it, otherwise the savings number drifts.

For a multi-run aggregate of the same comparison see `bernstein cost` -
the `--json` payload includes `savings_vs_opus_usd` over the configured
window.

## Cost monitoring

The run emits warnings at the spending thresholds described above. Use the
cost commands in this guide to inspect spend, and see
[OTLP export](../observability/otlp-export.md) for telemetry configuration.

## Cost comparison: Bernstein vs. manual

For a typical 50-task feature:

| Approach | Time | API cost | Total cost (incl. engineer time) |
|----------|------|----------|----------------------------------|
| Manual (engineer + Copilot) | 40 hours | ~$5 | ~$4000+ |
| Bernstein (mixed models) | 2-4 hours | ~$20-40 | ~$200-400 |
| Bernstein (all opus) | 2-4 hours | ~$60-100 | ~$260-500 |

The cost savings come from parallelization and reduced engineer time, not cheaper API calls. Optimize for elapsed time first, API cost second.
