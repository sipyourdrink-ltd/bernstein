# Asana tracker adapter

Bernstein can pull work from an Asana project, post progress stories
back to the task, and move the task between sections when a task
transitions. The adapter also supports per-step CLI choice: an
operator-declared custom field can name the CLI adapter (`claude`,
`codex`, `aider`, ...) Bernstein should pick for each task.

The adapter implements `bernstein.core.trackers.AbstractTrackerAdapter`
and lives under `bernstein.core.trackers.builtin.asana_adapter`. It is
disabled by default and must be opted in via `bernstein.yaml`.

An Asana task is represented as a `Ticket` by the adapter. Its Asana GID
becomes `Ticket.id`, and its permalink becomes `Ticket.external_url`.

## Auth

Authentication uses an Asana Personal Access Token (PAT). Point the
adapter at the environment variable that holds the token. If `pat_env`
is omitted the adapter falls back to `ASANA_PERSONAL_ACCESS_TOKEN`.

```yaml
trackers:
  asana:
    workspace_gid: "1199999999999999"
    project_gid: "1201234567890123"
    auth:
      pat_env: ASANA_PERSONAL_ACCESS_TOKEN
```

A PAT with read+write access on the workspace is sufficient. See the
Asana developer docs for how to mint one.

## Sections vs. status

Asana does not have a global "status" surface. Sections are per-project
workflow buckets and act as the status surface for this adapter.
Section gids are operator-supplied because they cannot be inferred
without the schema.

```yaml
trackers:
  asana:
    workspace_gid: "1199999999999999"
    project_gid: "1201234567890123"
    section_filter_gid: "1207770000000001"  # default section to pull
    section_map:
      ready: "1207770000000001"
      claimed: "1207770000000002"
      done: "1207770000000003"
      failed: "1207770000000004"
```

`transition(ticket_id, status_id)` resolves `status_id` in this order:

1. Treat the value as a section gid (opaque).
2. Look it up in `section_map`.

If neither resolves, `TrackerUnavailable` is raised.

## Per-step CLI choice

Add a single-select custom field whose enum values name the CLI
(`claude`, `codex`, `aider`). Point the adapter at the field's gid:

```yaml
trackers:
  asana:
    workspace_gid: "1199999999999999"
    project_gid: "1201234567890123"
    cli_choice_custom_field_gid: "1207770099999999"
```

The adapter exposes the field value on the ticket dataclass:

```python
from bernstein.core.trackers.builtin import AsanaAdapter, AsanaConfig

config = AsanaConfig(
    workspace_gid="1199999999999999",
    project_gid="1201234567890123",
    section_filter_gid="1207770000000001",
    cli_choice_custom_field_gid="1207770099999999",
    pat_env="ASANA_PERSONAL_ACCESS_TOKEN",
)
with AsanaAdapter(config) as adapter:
    for ticket in adapter.pull_open_tickets():
        cli = ticket.routing_hint.cli  # "claude" / "codex" / "aider" / None
```

When the orchestrator's routing layer sees a non-empty
`ticket.routing_hint.cli`, it pins the spawned CLI for that ticket.

## Example workflow

A common ops-team setup keeps four sections in one Asana project:

| Section   | Bernstein status | Meaning                          |
|-----------|------------------|----------------------------------|
| Ready     | ready            | Available for an agent to claim. |
| In flight | claimed          | An agent has picked it up.       |
| Done      | done             | Acceptance criteria green.       |
| Failed    | failed           | Hard halt, operator follow-up.   |

A custom field `Agent` (text or enum) lets the requester pin a specific
CLI per task. Stories (comments) on the task carry the agent's status
updates back to the requester without leaving the Asana UI.

## Comments and transitions

- `add_comment(ticket_id, body)` posts a story to the task. Pass
  `ticket.id` (the task gid) as `ticket_id`.
- `transition(ticket_id, status_id)` moves the task into the target
  section via `POST /sections/{section_gid}/addTask`.

Both methods accept an `idempotency_key` that is forwarded as an
`X-Idempotency-Key` request header. The server ignores unknown headers;
operator-side logging can use the value to correlate retries.

## Custom-field writes

For non-section custom-field writes, call
`adapter.update_custom_field(ticket_id, custom_field_gid, value)`. The
accepted shape of `value` depends on the custom-field type (text,
number, enum option gid, etc.) and matches the body shape documented in
the Asana API reference.

## Rate limiting

- A cooperative per-adapter token-bucket throttles outbound calls when
  `rate_limit_min_interval` is set.
- A `429` response is translated into `RateLimited(retry_after=...)`.
  The hint is taken from the `Retry-After` header when present.

## Exceptions

| Exception            | When                                            |
|----------------------|-------------------------------------------------|
| `TrackerUnavailable` | 4xx (other than 429), 5xx, malformed JSON, payload `errors`, missing token, empty section gid. |
| `RateLimited`        | 429.                                            |

## Out of scope

- Portfolios and goals (separate adapter shape if needed).
- Asana Forms (we read tasks, not forms).
- Auto-discovery of custom-field schemas. Custom-field gids must be
  declared in `bernstein.yaml` by the operator.
