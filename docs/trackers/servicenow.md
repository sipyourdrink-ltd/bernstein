# ServiceNow tracker adapter

Bernstein can pull work directly from a ServiceNow tenant, post
progress to the underlying record's `work_notes` field, and move the
record through its state field when a task transitions. The adapter
targets the `incident` table by default. `change_request`, `problem`,
and custom-application tables are supported via the `table_name`
config field.

The adapter implements `bernstein.core.trackers.AbstractTrackerAdapter`
and lives at `bernstein.core.trackers.servicenow.ServiceNowTracker`.
It is disabled by default and must be opted in via `bernstein.yaml`.

## Auth

Basic auth with three environment variables. This is the most common
path for ServiceNow Table API tenants. OAuth client-credentials is a
future extension.

| Environment variable        | Purpose                                  |
|-----------------------------|------------------------------------------|
| `SERVICENOW_INSTANCE_URL`   | Base URL of the tenant, e.g. `https://dev12345.service-now.com`. |
| `SERVICENOW_USERNAME`       | Basic-auth username.                     |
| `SERVICENOW_PASSWORD`       | Basic-auth password.                     |

Override the env var names per tenant via `username_env` and
`password_env` on `ServiceNowConfig`.

## Configuration

```yaml
trackers:
  servicenow:
    instance_url: https://dev12345.service-now.com
    table_name: incident
    state_field: state
    open_query: "active=true"
    state_map:
      todo: "1"
      in_progress: "2"
      done: "6"
```

| Field                      | Default        | Purpose                            |
|----------------------------|----------------|------------------------------------|
| `instance_url`             | env var        | Tenant base URL.                   |
| `table_name`               | `incident`     | Table to operate against.          |
| `state_field`              | `state`        | Workflow state field.              |
| `open_query`               | `active=true`  | Default `sysparm_query` clause.    |
| `state_map`                | `{}`           | Canonical -> tenant state mapping. |
| `page_size`                | `50`           | `sysparm_limit` page size.         |
| `rate_limit_min_interval`  | `0.0`          | Min seconds between calls.         |

## Pulling, commenting, transitioning

The normalized `Ticket.id` is the ServiceNow record's `sys_id`. Its
display `number` identifies the record in the user interface; it is
available as `ticket.raw["number"]` and is not the adapter's record ID.

```python
from bernstein.core.trackers import ServiceNowConfig, ServiceNowTracker

config = ServiceNowConfig(
    instance_url="https://dev12345.service-now.com",
    table_name="incident",
    state_map={"done": "6"},
)
with ServiceNowTracker(config) as tracker:
    for ticket in tracker.pull_open_tickets():
        tracker.add_comment(ticket.id, "Bernstein picked this up.")
        tracker.transition(ticket.id, "done")
```

- `pull_open_tickets(filter)` accepts `sysparm_query` and `fields`
  overrides via the filter dict.
- `add_comment(ticket_id, body)` appends to `work_notes`. The
  `ticket_id` is the record's `sys_id`.
- `transition(ticket_id, status_id)` updates the state field. The
  value is resolved through `state_map` first, then passed to
  ServiceNow as-is.

## Rate limiting

- 429 responses surface as `RateLimited(retry_after=...)`. The hint is
  taken from the `Retry-After` header.
- A cooperative per-adapter min-interval throttle is available via
  `rate_limit_min_interval`.

## Exceptions

| Exception                     | When                                                  |
|-------------------------------|-------------------------------------------------------|
| `TrackerUnavailable`          | 5xx, 401/403, missing config, malformed JSON.         |
| `RateLimited`                 | 429 with optional `Retry-After`.                      |
| `OptimisticConcurrencyError`  | 412 Precondition Failed on a write with an `etag`.    |

## Out of scope

- OAuth client-credentials auth (future extension).
- Custom-application schema discovery (operator declares the table
  and state field).
- Multi-instance routing inside a single adapter instance.
