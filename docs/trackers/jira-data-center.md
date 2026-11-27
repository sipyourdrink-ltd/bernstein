# Jira Data Center tracker adapter

Bernstein can pull work from a self-hosted Jira Data Center (or
late-model Jira Server) deployment, post progress comments back to the
underlying issue, and transition issues through the workflow when a
task completes. The adapter is the sibling of the GitHub Projects v2
adapter and follows the same `AbstractTrackerAdapter` contract.

The adapter lives at
`bernstein.core.trackers.builtin.jira_dc_adapter` and is disabled by
default. Operators opt in via `bernstein.yaml`.

## Auth

Jira Data Center supports bearer-token auth with Personal Access
Tokens. There is no email-pair flow as on Atlassian Cloud, and the
adapter never asks for one.

| Environment variable | Purpose                                       |
|----------------------|-----------------------------------------------|
| `JIRA_DC_PAT`        | Personal Access Token used for every request. |
| `BERNSTEIN_CA_BUNDLE`| Optional PEM bundle for self-signed Jira TLS. |

```yaml
trackers:
  jira_data_center:
    base_url: https://jira.acme.internal
    auth:
      pat_env: JIRA_DC_PAT
    project_key: ENG
```

The PAT environment variable name is configurable via `pat_env` so
operators can run multiple instances side by side.

## TLS

`BERNSTEIN_CA_BUNDLE` overrides `verify_tls` when set, so operators can
ship a fleet-wide CA bundle without rebuilding the wheel.

```bash
export BERNSTEIN_CA_BUNDLE=/etc/bernstein/internal-ca.pem
```

Set `verify_tls: false` in `bernstein.yaml` only for short-lived test
deployments. Production deployments should pin the bundle.

## Pulling tickets

`pull_open_tickets` runs a paginated JQL search. Pass an override in
the filter dict, or rely on the configured `default_jql`:

```python
from bernstein.core.trackers.builtin import (
    JiraDataCenterAdapter,
    JiraDataCenterConfig,
)

config = JiraDataCenterConfig(
    base_url="https://jira.acme.internal",
    project_key="ENG",
    cli_choice_field_id="customfield_10100",
)
with JiraDataCenterAdapter(config) as adapter:
    for ticket in adapter.pull_open_tickets({"status": "Ready"}):
        cli = ticket.routing_hint.cli  # "claude" / "codex" / "aider" / None
        # hand ticket.id (issue key) to the orchestrator's routing layer
```

In the normalized ticket record, `ticket.id` (Ticket ID) contains the Jira
issue key (for example, `ENG-123`). Jira's internal issue ID is retained
separately in the record's raw metadata.

The filter accepts:

| Key       | Meaning                                                       |
|-----------|---------------------------------------------------------------|
| `jql`     | Raw JQL override; takes precedence over the other keys.       |
| `project` | Project key override for this call.                           |
| `status`  | Shorthand that becomes `status = "<value>"` when no `jql` set. |

## Status mapping

`status_map` maps canonical Bernstein status names to Jira transition
names so callers do not need to know the workflow numeric ids:

```yaml
trackers:
  jira_data_center:
    status_map:
      todo: To Do
      in_progress: In Progress
      done: Done
```

`transition(ticket_id, status_id)` resolves `status_id` in this order:

1. Treat it as a transition id (opaque).
2. Look it up in `status_map`.
3. Match it against `transition.name` or `transition.to.name` from the
   issue's available transitions list.

## Per-step CLI choice

Configure `cli_choice_field_id` with a Jira custom-field id (for
example `customfield_10100`) and expose it as a single-select field
with values `claude`, `codex`, `aider`. The adapter surfaces the
value as `ticket.routing_hint.cli` so the orchestrator's routing layer
can pin a CLI per ticket.

## Comments and transitions

- `add_comment(ticket_id, body)` posts to
  `/rest/api/2/issue/<key>/comment`. The optional `idempotency_key` is
  passed back as the `X-Bernstein-Idempotency-Key` header so
  operators auditing webhook traffic can reconcile retries.
- `transition(ticket_id, status_id)` GETs the available transitions,
  resolves the target id, then POSTs to
  `/rest/api/2/issue/<key>/transitions`.

## Rate limiting

- A cooperative per-adapter token-bucket throttles outbound calls when
  `rate_limit_min_interval` is set.
- Server-side `429` (and `503` carrying `Retry-After`) is translated
  into `RateLimited(retry_after=...)`.

## Exceptions

| Exception                     | When                                              |
|-------------------------------|---------------------------------------------------|
| `TrackerUnavailable`          | 5xx, missing PAT, unknown transition, malformed JSON. |
| `RateLimited`                 | 429, or 503 with `Retry-After`.                   |
| `OptimisticConcurrencyError`  | 412 Precondition Failed on a write.               |

## Out of scope

- Jira Server (end of life) is not a supported deployment target.
- Cloud-to-Data-Center migration helpers.
- A bundled Jira plugin: the adapter runs alongside Jira, not inside it.
