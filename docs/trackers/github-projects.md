# GitHub Projects v2 tracker adapter

Bernstein can pull work directly from a GitHub Projects v2 board, post
progress comments back to the underlying Issue or PR, and move the
project item between status columns when a task transitions. The
adapter also supports per-step CLI choice: a project field can name the
CLI adapter (`claude`, `codex`, `aider`, ...) Bernstein should pick for
each ticket.

The adapter uses the project item ID for status changes and the linked
work item ID for comments.

The adapter implements `bernstein.core.trackers.AbstractTrackerAdapter`
and lives under
`bernstein.core.trackers.builtin.github_projects_adapter`. It is
disabled by default and must be opted in via `bernstein.yaml`.

## Auth modes

Two auth modes are supported. App auth is preferred for orchestrator
workloads because the per-installation rate-limit budget is roughly
twice the per-user one.

### GitHub App installation token (recommended)

Reuses `bernstein.github_app.app` for JWT minting and the
`POST /app/installations/<id>/access_tokens` exchange. Set:

| Environment variable        | Purpose                                  |
|-----------------------------|------------------------------------------|
| `GITHUB_APP_PRIVATE_KEY`    | PEM string or path to the App's PEM key. |
| `GITHUB_WEBHOOK_SECRET`     | Webhook secret (reused for App config).  |

Then point the adapter at the App in `bernstein.yaml`:

```yaml
trackers:
  github_projects:
    project_owner: acme
    project_number: 42
    auth:
      app_id: "123456"
      private_key_path: /etc/bernstein/gh-app.pem
      installation_id: 7891011
```

### Personal Access Token

For solo developers and small teams, a fine-grained personal access
token is sufficient. Fine-grained PATs use granular per-resource
permissions rather than the classic scope strings, so configure the
token with:

- Organization permissions: **Projects: Read and write** (required for
  Projects v2 API access; the project owner must be an organization for
  fine-grained PATs).
- Repository permissions: **Issues: Read and write** and
  **Pull requests: Read and write** for the repositories whose content
  is linked into the project.

The legacy scope strings `read:project`, `repo`, and `write:project`
only apply to classic PATs and are not valid fine-grained PAT
permissions. Point the adapter at an environment variable holding the
token:

```yaml
trackers:
  github_projects:
    project_owner: acme
    project_number: 42
    auth:
      pat_env: GH_BERNSTEIN_PROJECTS_PAT
```

If `pat_env` is omitted the adapter falls back to `GITHUB_TOKEN`.

## Status mapping

`status_field_name` defaults to `Status`. Override it if your project
uses a different single-select field. `status_map` maps canonical
Bernstein statuses to the project's display names:

```yaml
trackers:
  github_projects:
    status_field_name: Status
    status_map:
      todo: Todo
      in_progress: In Progress
      done: Done
```

The adapter resolves a transition target in this order:
1. Treat the value as an option id (opaque).
2. Look it up in `status_map`.
3. Look it up in the project's option list by display name.

## Per-step CLI choice (worked example)

Add a single-select project field named `CLI` with values
`claude`, `codex`, `aider`. Each ticket can then pick its CLI:

```yaml
trackers:
  github_projects:
    project_owner: acme
    project_number: 42
    status_field_name: Status
    cli_choice_field_name: CLI
    auth:
      pat_env: GH_BERNSTEIN_PROJECTS_PAT
```

The adapter exposes the field value on the ticket dataclass:

```python
from bernstein.core.trackers.builtin import (
    GitHubProjectsV2Adapter,
    GitHubProjectsV2Config,
)

config = GitHubProjectsV2Config(
    project_owner="acme",
    project_number=42,
    status_field_name="Status",
    status_filter="Todo",
    cli_choice_field_name="CLI",
    pat_env="GH_BERNSTEIN_PROJECTS_PAT",
)
with GitHubProjectsV2Adapter(config) as adapter:
    for ticket in adapter.pull_open_tickets():
        cli = ticket.routing_hint.cli  # "claude" / "codex" / "aider" / None
        # hand cli + ticket.id to the orchestrator's routing layer
```

When the orchestrator's routing layer sees a non-empty
`ticket.routing_hint.cli`, it pins the spawned CLI for that ticket.
Operators can mix CLIs on a per-ticket basis:

| Ticket                  | `CLI` value | CLI Bernstein spawns |
|-------------------------|-------------|----------------------|
| Refactor parser         | claude      | claude               |
| Add unit tests          | codex       | codex                |
| Vendor third-party libs | aider       | aider                |

## Comments and transitions

- `add_comment(ticket_id, body)` posts to the underlying Issue or PR.
  Pass `ticket.raw["content_id"]` as `ticket_id` (the project item
  itself has no comment surface in the GraphQL API).
- `transition(ticket_id, status_id)` updates the project item's
  `status_field` value. Pass the project item id (the `ticket.id`
  yielded by `pull_open_tickets`).

Both methods accept an `idempotency_key` that is sent as the GraphQL
`clientMutationId`.

## Rate limiting

- A cooperative per-adapter token-bucket throttles outbound calls when
  `rate_limit_min_interval` is set.
- Server-side `429` and the `403`-shaped abuse-detection responses are
  translated into `RateLimited(retry_after=...)`. The hint is taken
  from `Retry-After` first, then `X-RateLimit-Reset`.
- Secondary rate-limit errors that the GraphQL API returns inside a
  200 response (`{"errors": [{"type": "RATE_LIMITED", ...}]}`) are
  surfaced the same way.

## Exceptions

| Exception                        | When                                              |
|----------------------------------|---------------------------------------------------|
| `TrackerUnavailable`             | 5xx, missing token, unknown status, malformed JSON. |
| `RateLimited`                    | 429, 403 abuse-detection, GraphQL `RATE_LIMITED`. |
| `OptimisticConcurrencyError`    | 412 Precondition Failed on a write.                |

## Out of scope

- Classic (v1) GitHub Projects -- deprecated and unsupported.
- Repository-level Issues without a Project board -- use the existing
  GitHub App webhook trigger source (`bernstein.github_app.webhooks`).
- Workflow / Actions-side hooks -- a separate cross-cutting ticket.
