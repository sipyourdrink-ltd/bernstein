# D13 — Graceful Degradation on Provider Errors

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
When a provider API returns 503 or times out, Bernstein crashes with a raw stack trace. This is confusing and makes the tool feel unreliable, even when the issue is transient.

## Solution
- Catch HTTP 503, 502, 504, and timeout errors from provider API calls.
- Display a friendly message: "OpenRouter is temporarily slow. Bernstein will retry with [fallback model] in 5s. To skip: Ctrl+C".
- Implement a retry loop with exponential backoff (5s, 10s, 20s) that falls back to the next model in the configured model list.
- On Ctrl+C during the wait, skip the current task gracefully instead of crashing.
- Log the raw error details to `.sdd/runs/latest/errors.log` for debugging.

## Acceptance
- [ ] A 503 response from the provider shows the friendly retry message, not a stack trace
- [ ] Bernstein retries with the fallback model after the countdown
- [ ] Ctrl+C during the retry wait skips the task without crashing
- [ ] Timeout errors (requests exceeding 60s) trigger the same graceful handling
- [ ] Raw error details are written to the run's error log
