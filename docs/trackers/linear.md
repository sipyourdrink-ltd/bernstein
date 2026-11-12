# Linear tracker adapter

Bernstein can pull work directly from a Linear team, post progress
comments back to the underlying issue, and move the issue between
workflow states when a task transitions. The adapter also supports
per-ticket CLI choice via Linear labels: a label prefix such as
`cli/claude` pins the spawned CLI for that ticket.

At the adapter boundary, each Linear issue is exposed as a Bernstein
`Ticket`. The `Ticket` ID holds the Linear issue ID, while its display
identifier is kept separately in the raw metadata.

The adapter implements `bernstein.core.trackers.AbstractTrackerAdapter`
and lives at `bernstein.core.trackers.linear.LinearTracker`. It is
disabled by default and must be opted in via `bernstein.yaml`.

## Auth

The adapter authenticates with a Linear Personal API Key (issued in
Linear under `Settings -> API -> Personal API keys`).

| Environment variable | Purpose |
|----------------------|---------|
| `LINEAR_API_KEY`     | Personal API key sent verbatim as the `Authorization` header. |

Override the env var name with `LinearConfig.api_key_env` when running
multiple Linear adapters against different workspaces:

```yaml
trackers:
  linear:
    team_key: ENG
    api_key_env: LINEAR_API_KEY_PROD
```

## State mapping

Linear teams own a list of workflow states (e.g. `Todo`, `In Progress`,
`Done`, `Canceled`). The adapter discovers them once on first call and
caches the schema. `state_map` maps canonical Bernstein statuses to the
team's state names:

```yaml
trackers:
  linear:
    team_key: ENG
    state_map:
      todo: Todo
      in_progress: In Progress
      done: Done
```

The adapter resolves a transition target in this order:
1. Treat the value as a state id (opaque UUID).
2. Look it up in `state_map`.
3. Look it up in the team's workflow-state list by display name.

## Per-ticket CLI choice via labels

Linear does not have first-class single-select fields the way GitHub
Projects v2 does. Instead, the adapter routes by label prefix. Add
labels named `cli/claude`, `cli/codex`, `cli/aider` to issues that
need a specific CLI:

```yaml
trackers:
  linear:
    team_key: ENG
    label_routing_field: "cli/"
```

The adapter exposes the selected CLI on the ticket dataclass:

```python
from bernstein.core.trackers.linear import LinearTracker, LinearConfig

config = LinearConfig(
    team_key="ENG",
    state_filter="Todo",
    label_routing_field="cli/",
)
with LinearTracker(config) as adapter:
    for ticket in adapter.pull_open_tickets():
        cli = ticket.routing_hint.cli  # "claude" / "codex" / "aider" / None
        # hand cli + ticket.id to the orchestrator's routing layer
```

## Comments and transitions

- `add_comment(ticket_id, body)` posts to the underlying Linear issue
  via the `commentCreate` mutation. Pass the issue UUID as
  `ticket_id` (the `ticket.id` yielded by `pull_open_tickets`).
- `transition(ticket_id, status_id)` updates the issue's workflow
  state via the `issueUpdate` mutation.

Linear's GraphQL mutations do not surface a `clientMutationId`
parameter, so `add_comment` appends `idempotency_key` to the comment
body as a hidden HTML-comment marker so operators can de-duplicate
posts client-side.

## Rate limiting

- A cooperative per-adapter token-bucket throttles outbound calls when
  `rate_limit_min_interval` is set.
- Server-side `429` is translated into `RateLimited(retry_after=...)`,
  with the hint taken from the `Retry-After` header.
- Secondary rate-limit errors that Linear returns inside a 200 (with
  `extensions.code == "RATELIMITED"`) are surfaced the same way.

## Exceptions

| Exception                       | When                                                          |
|---------------------------------|---------------------------------------------------------------|
| `TrackerUnavailable`            | 5xx, missing token, unknown state, malformed JSON, 4xx auth.  |
| `RateLimited`                   | 429, GraphQL `RATELIMITED` error.                              |
| `OptimisticConcurrencyError`    | 412 Precondition Failed on a write.                            |

## Out of scope

- Webhook ingestion (separate cross-cutting ticket).
- Linear Sub-Issues federation (handled by the federation layer).
- Linear documents and projects -- this adapter handles issues only.
