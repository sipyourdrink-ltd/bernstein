# Schema validation retry

When a spawned agent emits malformed JSON (manager planning output,
MCP tool response, planner decoder), Bernstein validates the supplied
response and can request another response after a validation failure.
`max_attempts` is the maximum number of **total validations**, including
the supplied initial response—not a count of additional retries.
Validation feedback accumulated so far is placed before the base prompt
when requesting another response.

## Why it exists

Before this loop, schema failures fell into one of two paths: hard
fail (mark the task failed) or generic retry that re-spawned without
the validation error in context. Neither helped the agent self-correct.
The Self-Refine pattern (ICLR 2024) and adjacent work documents 15-45 %
quality improvement from a multi-attempt retry with detailed feedback.

## How to use it

The retry helper is wired into the call sites that decode structured
agent output. From your own code:

```python
from bernstein.core.tasks.schema_retry import (
    SchemaRetryContext,
    validate_with_retry,
)
from pydantic import BaseModel


class PlannerOutput(BaseModel):
    tasks: list[str]
    rationale: str


ctx = SchemaRetryContext()


def validate(raw: str) -> PlannerOutput:
    # raise ValueError on bad input; the retry loop catches it
    return PlannerOutput.model_validate_json(raw)


def ask_again(prompt_with_errors: str) -> str:
    # call your spawned agent / model with the augmented prompt
    return run_agent(prompt_with_errors)


result = validate_with_retry(
    raw_text,  # initial_response (positional)
    validate,  # parses-or-raises ValueError
    ctx,
    ask_again=ask_again,
    max_attempts=3,
)
```

The second positional argument is a `validate` callable that parses the
response or raises `ValueError`, not a schema class directly, so any
validator (Pydantic, JSONSchema, hand-rolled) plugs in. On a malformed
payload the helper:

1. Runs the `validate` callable on the current response, starting with
   `initial_response`.
2. Returns the result immediately if validation succeeds, including when
   success occurs on the final allowed validation.
3. On a `ValueError`, records the error in `ctx`. If this was the final
   allowed validation, raises `SchemaRetryExhausted` with the full error
   trail; it does not call `ask_again`.
4. Otherwise, calls `ask_again` with the accumulated validation-feedback
   preamble followed by `base_prompt` (if supplied), then validates the
   returned response. Thus, `max_attempts=1` validates only the supplied
   response and makes no callback; `max_attempts=3` validates at most
   three responses and makes at most two callbacks.

Errors accumulate across validations and across steps in the same
`SchemaRetryContext`, so the same agent across a multi-step pipeline sees
the trail of every prior validation failure, not just the most recent.

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `defaults.SCHEMA_RETRY_MAX_ATTEMPTS` | `3` | Maximum total validations per decode, including the supplied response. |
| `schema_retry_attempts_total{outcome}` | metric | `success` / `recovered` / `retry` / `exhausted` outcomes; scrape for cost-tracking the retry loop. |

## Limitations

- This is a **wrapper** around existing validation, not a replacement
  for Pydantic. Models still own field constraints.
- The agent itself is the repair loop - there is no LLM-based schema
  repair sitting between the validator and the agent.
- Token cost of retries is captured by the existing `cost_tracker`,
  not by this module.
- Only places that have been wired in see retry: `manager_parsing`
  and `mcp_tool_normalization`. Decoders elsewhere fall back to the
  one-shot `json.loads` path.

## Related

- Source: `src/bernstein/core/tasks/schema_retry.py`
- Wired in: `core/orchestration/manager_parsing.py`,
  `core/protocols/mcp/mcp_tool_normalization.py`
- PR #998
