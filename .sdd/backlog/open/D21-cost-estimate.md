# D21 — Cost Estimate Dry-Run Command

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
Users have no way to know how much a goal will cost before executing it. This creates anxiety about unexpected charges, especially for large or exploratory goals.

## Solution
- Implement `bernstein cost estimate "<goal>"` that performs a dry-run.
- Use the LLM planner to decompose the goal into tasks (same logic as a real run).
- For each task, estimate token count based on: goal complexity, expected context size, and model output patterns.
- Calculate expected cost using the model's per-token pricing from the provider config.
- Print a formatted table:
  ```
  Task                  Model              Est. Tokens    Est. Cost
  ────────────────────────────────────────────────────────────────
  #1 Analyze codebase   gpt-4o             ~4,200         $0.021
  #2 Generate tests     claude-sonnet-4    ~6,800         $0.034
  #3 Apply fixes        gpt-4o-mini        ~2,100         $0.002
  ────────────────────────────────────────────────────────────────
  Total                                    ~13,100        $0.057
  ```
- No code execution or file modification occurs during the estimate.
- Support `--json` flag for machine-readable output.

## Acceptance
- [ ] `bernstein cost estimate "<goal>"` decomposes the goal into tasks without executing them
- [ ] A formatted table is printed with task name, model, estimated tokens, and estimated cost
- [ ] Total estimated cost is shown at the bottom
- [ ] No files are created or modified during the estimate
- [ ] `--json` flag outputs valid JSON with the estimate data
