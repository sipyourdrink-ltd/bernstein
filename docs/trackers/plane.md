# Plane tracker adapter

Bernstein can pull work directly from a Plane project, post progress
comments back to the underlying issue, and move the issue between
states when a task transitions. Plane is an OSS, self-hostable project
tracker; the adapter works against both Plane Cloud and any
self-hosted Plane deployment.

The adapter exposes each Plane issue as a Bernstein `Ticket`. The ticket ID
is the underlying Plane issue ID. The display sequence number is stored
separately as `sequence_id` in the ticket's raw metadata.

The adapter implements `bernstein.core.trackers.AbstractTrackerAdapter`
and lives under `bernstein.core.trackers.builtin.plane_adapter`. It is
disabled by default and must be opted in via `bernstein.yaml`.

## Auth

Plane exposes a single API key per workspace. Generate one in
`Workspace settings -> API tokens` and export it under an environment
variable. The adapter reads from `PLANE_API_KEY` by default; set
`api_token_env` to use a different name.

| Environment variable | Purpose                                          |
|----------------------|--------------------------------------------------|
| `PLANE_API_KEY`      | API key (default; override via `api_token_env`). |
| `PLANE_URL`          | Base URL override for self-hosted deployments.   |

When `PLANE_URL` is set it overrides `instance_url` from the config so
operators can move a single config file between staging and production
without editing it.

```yaml
trackers:
  plane:
    workspace_slug: acme
    project_id: 11111111-1111-1111-1111-111111111111
    instance_url: https://plane.acme.internal
    api_token_env: PLANE_API_KEY
```

For Plane Cloud, drop `instance_url` (the adapter defaults to
`https://api.plane.so`):

```yaml
trackers:
  plane:
    workspace_slug: acme
    project_id: 11111111-1111-1111-1111-111111111111
```

## State mapping

`state_filter` filters `pull_open_tickets` to a single state name.
`state_map` maps canonical Bernstein statuses to the project's display
names:

```yaml
trackers:
  plane:
    state_filter: Todo
    state_map:
      todo: Todo
      in_progress: In Progress
      done: Done
```

The adapter resolves a transition target in this order:
1. Treat the value as a Plane state UUID (opaque).
2. Look it up in `state_map`.
3. Look it up in the project's state list by display name.

## Per-step CLI choice (worked example)

Plane does not ship a "per-issue CLI" field, so the adapter overloads
labels for this purpose. Add labels named `cli:claude`, `cli:codex`,
or `cli:aider` to a Plane issue and configure the prefix:

```yaml
trackers:
  plane:
    workspace_slug: acme
    project_id: 11111111-1111-1111-1111-111111111111
    cli_choice_label_prefix: "cli:"
    api_token_env: PLANE_API_KEY
```

The adapter strips the prefix and exposes the remainder on the ticket:

```python
from bernstein.core.trackers.builtin import PlaneAdapter, PlaneConfig

config = PlaneConfig(
    workspace_slug="acme",
    project_id="11111111-1111-1111-1111-111111111111",
    instance_url="https://plane.acme.internal",
    state_filter="Todo",
    cli_choice_label_prefix="cli:",
    api_token_env="PLANE_API_KEY",
)
with PlaneAdapter(config) as adapter:
    for ticket in adapter.pull_open_tickets():
        cli = ticket.routing_hint.cli  # "claude" / "codex" / "aider" / None
        # hand cli + ticket.id to the orchestrator's routing layer
```

When the orchestrator's routing layer sees a non-empty
`ticket.routing_hint.cli`, it pins the spawned CLI for that ticket.

| Ticket          | Label       | CLI Bernstein spawns |
|-----------------|-------------|----------------------|
| Refactor parser | cli:claude  | claude               |
| Add unit tests  | cli:codex   | codex                |
| Vendor libs     | cli:aider   | aider                |

## Local docker-compose example

A minimal "Plane plus Bernstein on one host" setup; pair it with the
official Plane self-hosted compose file (`plane-ce.yml` in the Plane
repo) for a quick local trial.

```yaml
services:
  bernstein:
    image: ghcr.io/sipyourdrink-ltd/bernstein:latest
    environment:
      PLANE_API_KEY: ${PLANE_API_KEY}
      PLANE_URL: http://plane-api:8000
    depends_on:
      - plane-api
    volumes:
      - ./bernstein.yaml:/etc/bernstein/bernstein.yaml:ro
```

## Comments and transitions

- `add_comment(ticket_id, body)` posts to
  `/api/v1/workspaces/<slug>/projects/<id>/issues/<id>/comments/`. The
  `idempotency_key` is sent as the `Idempotency-Key` HTTP header.
- `transition(ticket_id, status_id)` PATCHes the issue's `state`
  field to the resolved state UUID.

## Rate limiting

- A cooperative per-adapter token-bucket throttles outbound calls
  when `rate_limit_min_interval` is set.
- Server-side `429` responses are translated into
  `RateLimited(retry_after=...)`. The hint is taken from the
  `Retry-After` header.

## Exceptions

| Exception                     | When                                                |
|-------------------------------|-----------------------------------------------------|
| `TrackerUnavailable`          | 4xx (other than 412/429), 5xx, missing token, unknown state, malformed JSON. |
| `RateLimited`                 | 429.                                                |
| `OptimisticConcurrencyError` | 412 Precondition Failed on a write.                  |

## Out of scope

- Plane's bundled MCP server consumer mode (separate ticket).
- Cycles / modules / workspace-level views; v1 targets the
  project-level issue surface only.
