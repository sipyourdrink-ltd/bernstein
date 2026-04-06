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

### Configuring model policy

```yaml
# bernstein.yaml
model_policy:
  simple: "haiku"
  medium: "sonnet"
  complex: "opus"
```

The orchestrator classifies tasks by complexity using file count, scope breadth, and dependency depth. Override per-task in plan files:

```yaml
steps:
  - goal: "Fix typo in README"
    complexity: simple    # Forces haiku
  - goal: "Redesign auth system"
    complexity: complex   # Forces opus
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

### Cheaper retry strategy

```yaml
cheaper_retry:
  enabled: true
  fallback_model: "haiku"
  max_retries: 2
```

When a task fails or exceeds budget on an expensive model, retry it with a cheaper model. Many tasks succeed with less capable models on the second attempt because the first attempt's partial work narrows the problem.

## Context window optimization

### Scope isolation

Narrow task scopes reduce the amount of context each agent needs:

```yaml
steps:
  - goal: "Add validation to User model"
    scope: ["src/models/user.py", "tests/test_user.py"]
```

Precise scopes mean:
- Fewer files loaded into context
- Fewer tokens consumed
- Lower cost per task

### Context compression

Enable context compression to reduce token usage for large codebases:

```yaml
context:
  compression: true
  max_context_files: 20
  summary_depth: "shallow"  # shallow | medium | deep
```

## Spend forecasting

Bernstein tracks token usage and projects future costs:

```bash
# View current run cost
bernstein status --cost

# View cost breakdown by model
bernstein status --cost --by-model

# View cost forecast
bernstein status --forecast
```

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
