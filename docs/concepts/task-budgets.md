# Task budgets countdown

Bernstein provides Python helpers for formatting a task-budget countdown and
checking whether an agent should finish gracefully. They are opt-in building
blocks: the formatter is not automatically inserted into adapter messages,
and the finish predicate does not automatically enforce a budget. The Claude
adapter can separately opt in to propagating Anthropic's
`task-budgets-2026-03-13` beta header; that header opt-in does not enable the
countdown helpers.

## Why it exists

A countdown can help an agent decide when to wrap up, but Bernstein does not
currently call the countdown formatter or finish predicate from adapter or
orchestrator production code. Use of either helper requires an explicit
caller. In particular, the helper alone neither changes the messages an agent
receives nor stops work when a budget is reached.

## The banner

The Python function
[`bernstein.core.cost.budget_countdown.format_countdown`][format] returns a
single deterministic line. For the inputs in the formatter unit test, it
returns:

```
[budget] tokens left: 18,420 of 64,000 (28%) | $0.42 of $1.50 | steps: 7 of 30 | mode: graceful-finish-on-low
```

The fields mean:

- `tokens left` is remaining tokens out of the card's token cap (and the
  percentage is the remaining fraction, truncated to an integer).
- `$0.42 of $1.50` is dollars already spent out of the card's dollar budget,
  not dollars of headroom.
- `steps: 7 of 30` is the current step and the card's maximum step count.
- `mode` is the budget mode selected on the identity card.

The formatter produces plain text without a newline. It does not itself
insert the banner into a user message or any other adapter prompt; an
integration must call it and decide where to use its result.

## Two budget modes

The Python predicate
[`bernstein.core.cost.budget_countdown.should_finish_gracefully`][gracefully]
returns a decision; it does not itself schedule a final turn, stop work, or
trigger an action. Its conditions are:

| Mode | Condition that returns `True` |
|---|---|
| `graceful-finish-on-low` (default) | The current step is at least `max_steps` (when `max_steps > 0`), or remaining tokens are at or below 20% of a positive token cap. If `max_tokens <= 0` (no token cap), the low-token condition is disabled; the step condition still applies. |
| `hard-stop-on-zero` | The computed remaining-token count is zero (or less). The step count and low-token threshold do not affect this mode; with `max_tokens <= 0`, the helper's computed remaining count is zero, so this condition is met. |

The graceful mode's default low threshold is 20%; callers can pass a different
threshold to the helper. A `True` result is only a signal to the caller, not
automatic enforcement.

## Opting in to the Anthropic beta

The Claude adapter can opt in to propagating Anthropic's beta header with the
process environment variable below. This is separate from calling either
countdown helper; enabling the header does not insert a countdown or enforce a
budget.

```bash
export BERNSTEIN_ANTHROPIC_TASK_BUDGETS=true
```

When enabled, the adapter sets
`ANTHROPIC_BETA=task-budgets-2026-03-13` for Claude Code, preserving any
pre-existing operator-set `ANTHROPIC_BETA` values and appending this value
once.

## Metrics

The module provides `record_graceful_finish` as an explicit metrics helper.
When called, it attempts to increment
`bernstein_task_budget_graceful_finish_total{role}` and observe
`bernstein_task_budget_remaining_at_finish_pct{role}` (the remaining-token
percentage at finish). The helper suppresses metrics exceptions. Its existence
does not mean it is called or that these counters are emitted on every run.

## References

- Anthropic task-budgets beta: <https://docs.anthropic.com/en/release-notes/api>
  (search `task-budgets-2026-03-13`)
- Cursor Glass arbitration pause: <https://cursor.com/changelog>
  (April 2 2026)

[format]: https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/cost/budget_countdown.py
[gracefully]: https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/cost/budget_countdown.py
