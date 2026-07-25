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

## Prompt caching

Bernstein supports provider prompt caching to reduce token costs on repeated context.

```yaml
prompt_caching:
  enabled: true
  strategy: "role_context"  # Cache role prompts and project context
```

Caching is most effective when:
- Multiple agents share the same role (e.g., several backend agents)
- Project context files are stable between tasks
- The same codebase analysis is reused across tasks

Expected savings: 30-60% on input tokens for cached portions.

## Batch API usage

For non-urgent workloads, use batch API pricing (typically 50% cheaper):

```yaml
batch_mode:
  enabled: true
  max_wait_seconds: 300
  min_batch_size: 5
```

Batch mode collects tasks and submits them together. Latency increases but cost decreases. Appropriate for overnight runs, CI/CD, and non-interactive workflows.

## Token budget management

Set per-task and per-run token budgets to prevent runaway costs:

```yaml
budget:
  per_task_max_tokens: 100000
  per_run_max_tokens: 2000000
  per_run_max_cost_usd: 50.00
  alert_threshold_pct: 80
```

When a task exceeds its token budget, it is terminated gracefully with partial results saved. The orchestrator can retry with a cheaper model.

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
2. `.sdd/runtime/run_config.json` (`per_run_max_cost_usd`).
3. `bernstein.yaml` `seed.budget_usd`.
4. Default `0.0` (= unlimited).

Three thresholds fire as cumulative spend rises: warn at 80%,
critical at 95%, hard stop at 100%. On hard stop the orchestrator
drains live agents, persists state, and exits. Non-positive values
normalise to `0.0` (unlimited); invalid env values are logged at
warning level so a typo never silently disables the guard.

This pairs with the soft-threshold `budget` block above: keep the
soft block for early signals during the run, set `--max-cost-usd`
for the line you do not want crossed.

### Cheaper retry strategy

```yaml
cheaper_retry:
  enabled: true
  fallback_model: "haiku"
  max_retries: 2
```

When a task fails or exceeds budget on an expensive model, retry it with a cheaper model. Many tasks succeed with less capable models on the second attempt because the first attempt's partial work narrows the problem.

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

### Context compression

Enable context compression to reduce token usage for large codebases:

```yaml
context:
  compression: true
  max_context_files: 20
  summary_depth: "shallow"  # shallow | medium | deep
```

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

## Cost monitoring and alerts

### Budget alerts

```yaml
notifications:
  channels:
    - type: slack
      webhook_url: "https://hooks.slack.com/..."
  alerts:
    - event: budget.threshold
      threshold_pct: 80
    - event: budget.exceeded
```

### Datadog integration

Export cost metrics to Datadog for dashboards and alerting:

```yaml
datadog:
  enabled: true
  host: "localhost"
  port: 8125
  prefix: "bernstein"
```

Metrics exported: `bernstein.cost.total`, `bernstein.cost.per_task`, `bernstein.tokens.input`, `bernstein.tokens.output`.

## Cost comparison: Bernstein vs. manual

For a typical 50-task feature:

| Approach | Time | API cost | Total cost (incl. engineer time) |
|----------|------|----------|----------------------------------|
| Manual (engineer + Copilot) | 40 hours | ~$5 | ~$4000+ |
| Bernstein (mixed models) | 2-4 hours | ~$20-40 | ~$200-400 |
| Bernstein (all opus) | 2-4 hours | ~$60-100 | ~$260-500 |

The cost savings come from parallelization and reduced engineer time, not cheaper API calls. Optimize for elapsed time first, API cost second.
