# Error handling (Bernstein)

- Raise specific exception types — never bare `Exception` in business code.
- Wrap third-party errors at the boundary and re-raise a domain-specific
  class so callers don't leak vendor specifics.
- Use `from exc` to preserve the traceback.
- Log once at the layer that handles the error. Nested try/except ladders
  create double-log noise.

## HTTP / API
- Return typed Pydantic responses, not raw dicts.
- `4xx` errors always include a machine-readable `code` field.
- `5xx` responses should never include stack traces or internal paths.

## Async
- Propagate cancellation (`asyncio.CancelledError`) unchanged.
- Use `asyncio.timeout()` over `asyncio.wait_for()` for new code.
- Close network connections in `finally` or via `async with`.
