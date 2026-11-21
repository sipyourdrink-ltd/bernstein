# ClickUp tracker adapter

Bernstein can pull work directly from a ClickUp list, post progress
comments back to the underlying task, and move tasks between statuses
when work transitions. ClickUp calls this record a task; the Bernstein
adapter exposes it as a Ticket, and `ticket.id` retains the ClickUp task
identifier. The adapter also supports per-step CLI choice: a
custom field can name the CLI adapter (`claude`, `codex`, `aider`, ...)
Bernstein should pick for each ticket.

The adapter implements `bernstein.core.trackers.AbstractTrackerAdapter`
and lives under `bernstein.core.trackers.builtin.clickup_adapter`. It is
disabled by default and must be opted in via `bernstein.yaml`.

It talks to the public ClickUp REST API v2 directly rather than the
ClickUp MCP server, which keeps the call surface predictable and avoids
paying the MCP metering bill.

## Auth

ClickUp accepts a personal API token in the `Authorization` header. Set
the environment variable named by `token_env` and reference it from
`bernstein.yaml`:

| Environment variable    | Purpose                                |
|-------------------------|----------------------------------------|
| `CLICKUP_API_TOKEN`     | Personal API token (default env name). |

```yaml
trackers:
  clickup:
    list_id: "9012345"
    workspace_id: "1234567"
    space_id: "8901234"
    auth:
      token_env: CLICKUP_API_TOKEN
```

OAuth bearer tokens work too; pass the full token string in the
environment variable and the adapter forwards it as-is.

## Status mapping

`status_map` maps canonical Bernstein statuses to the list's display
status names. ClickUp status names are case-sensitive and per-list:

```yaml
trackers:
  clickup:
    status_map:
      todo: "to do"
      in_progress: "in progress"
      done: "complete"
```

`transition(ticket_id, status_id)` resolves the target name through
`status_map` first, then sends it on the wire.

## Per-step CLI choice (worked example)

Add a custom field to the list (single-select or short-text) with values
`claude`, `codex`, `aider`. Capture the field id from the ClickUp UI or
the `/list/{list_id}/field` endpoint, then point the adapter at it:

```yaml
trackers:
  clickup:
    list_id: "9012345"
    cli_choice_custom_field_id: "1f0c9c8d-c0ff-ee00-1234-deadbeefcafe"
    auth:
      token_env: CLICKUP_API_TOKEN
```

The adapter exposes the field value on the ticket dataclass:

```python
from bernstein.core.trackers.builtin import (
    ClickUpAdapter,
    ClickUpConfig,
)

config = ClickUpConfig(
    list_id="9012345",
    status_filter="to do",
    cli_choice_custom_field_id="1f0c9c8d-c0ff-ee00-1234-deadbeefcafe",
    token_env="CLICKUP_API_TOKEN",
)
with ClickUpAdapter(config) as adapter:
    for ticket in adapter.pull_open_tickets():
        cli = ticket.routing_hint.cli  # "claude" / "codex" / "aider" / None
        # hand cli + ticket.id to the orchestrator's routing layer
```

When the orchestrator's routing layer sees a non-empty
`ticket.routing_hint.cli`, it pins the spawned CLI for that ticket.

## Comments and transitions

- `add_comment(ticket_id, body)` posts to `/task/{task_id}/comment`.
  When `idempotency_key` is set, the key is appended as an HTML marker
  so re-tries stay traceable without duplicating the visible body.
- `transition(ticket_id, status_id)` issues
  `PUT /task/{task_id}` with the resolved status name.

## Rate limiting

- A cooperative per-adapter token-bucket throttles outbound calls when
  `rate_limit_min_interval` is set. Size it per plan tier:

  | Plan        | Suggested `rate_limit_min_interval` |
  |-------------|--------------------------------------|
  | Free        | `0.6` s (about 100 rpm)              |
  | Unlimited   | `0.06` s (about 1000 rpm)            |
  | Enterprise  | `0.006` s (about 10000 rpm)          |

- Server-side `429` responses are translated into
  `RateLimited(retry_after=...)`. The hint is taken from `Retry-After`
  first, then `X-RateLimit-Reset`.
- ClickUp also returns `{"err": "...", "ECODE": "RATE_..."}` in a `200`
  body when a soft limit is exceeded. The adapter surfaces those as
  `RateLimited` too.

## Exceptions

| Exception                        | When                                            |
|----------------------------------|-------------------------------------------------|
| `TrackerUnavailable`             | 5xx, missing token, error payload from ClickUp. |
| `RateLimited`                    | 429 or inline `RATE_...` ECODE.                 |
| `OptimisticConcurrencyError`     | 412 Precondition Failed on a write.             |

## Out of scope

- ClickUp Docs (this adapter is tasks-only).
- ClickUp MCP server consumer mode (separate ticket if requested).
- Webhooks. Pair this adapter with the existing webhook trigger-source
  subsystem if you need push-style updates instead of polling.
