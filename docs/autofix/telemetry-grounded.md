# Telemetry-grounded autofix

Status: MVP. Operator-flagged off per source. Sentry / GlitchTip and
GitHub Actions failure adapters ship with full dispatch wiring. Datadog,
Loki, and custom-JSONL adapters ship as stubs - the audit chain records
the would-be escalation but no agent is spawned.

The telemetry-grounded subsystem extends the autofix daemon so an error
or alert from your observability stack can open a PR through the same
ladder, cost cap, and audit primitives the CI-failure path already
uses. The dispatch loop is identical to the CI flow:

1. Webhook arrives at `/webhooks/telemetry/<source>/`.
2. Adapter parses the payload into a `TelemetryEvent`.
3. Grounding retriever pulls a window of recent log lines around the
   event fingerprint.
4. A grounded goal is built and handed to the existing autofix
   dispatch hook.
5. The outcome is recorded in the audit log.

## Sources

| Source        | Status   | Endpoint                                | Secret env (default)                       |
|---------------|----------|-----------------------------------------|---------------------------------------------|
| `sentry`      | full     | `/webhooks/telemetry/sentry/`           | `BERNSTEIN_SENTRY_WEBHOOK_SECRET`           |
| `gha_failure` | full     | `/webhooks/telemetry/gha_failure/`      | `BERNSTEIN_GHA_WEBHOOK_SECRET`              |
| `datadog`     | stubbed  | `/webhooks/telemetry/datadog/`          | `BERNSTEIN_DATADOG_WEBHOOK_SECRET`          |
| `loki`        | stubbed  | `/webhooks/telemetry/loki/`             | `BERNSTEIN_LOKI_WEBHOOK_SECRET`             |
| `custom_jsonl`| stubbed  | `/webhooks/telemetry/custom_jsonl/`     | `BERNSTEIN_CUSTOM_JSONL_WEBHOOK_SECRET`     |

The `sentry` adapter covers both Sentry SaaS and the Sentry-compatible
self-hosted GlitchTip - the issue-alert webhook envelope is identical.

## Configuration

Telemetry sources live under `autofix.telemetry_sources` in
`bernstein.yaml`. Each entry is opt-in:

```yaml
autofix:
  cost_cap_per_pr: 1.0
  telemetry_sources:
    - source: sentry
      enabled: true
      endpoint: /webhooks/telemetry/sentry/
      secret_env: BERNSTEIN_SENTRY_WEBHOOK_SECRET
      fingerprint_path: ""        # default: data.issue.id
      cost_cap_usd: 0.20
    - source: gha_failure
      enabled: true
      endpoint: /webhooks/telemetry/gha_failure/
      secret_env: BERNSTEIN_GHA_WEBHOOK_SECRET
      cost_cap_usd: 0.20
```

Fields:

| Field              | Purpose |
|--------------------|---------|
| `source`           | One of `sentry`, `gha_failure`, `datadog`, `loki`, `custom_jsonl`. |
| `enabled`          | Master switch. The receiver still accepts the request when disabled, but the dispatcher records `skipped`. |
| `endpoint`         | Mount path the upstream should POST to. |
| `secret_env`       | Env var holding the shared HMAC secret. Empty disables signature checks (test-only). |
| `fingerprint_path` | Optional dotted path into the payload that overrides the adapter's default fingerprint extraction. |
| `cost_cap_usd`     | Hard per-event cap. Zero refuses to spawn. |

## Setup: Sentry / GlitchTip

1. In Sentry / GlitchTip, create an Internal Integration or a
   project-level webhook with the *Issue alert created* trigger.
2. Set the webhook URL to
   `https://<your-host>/webhooks/telemetry/sentry/`.
3. Copy the integration's shared secret into the env var named in
   `secret_env` (default `BERNSTEIN_SENTRY_WEBHOOK_SECRET`).
4. Flip `enabled: true` on the `sentry` entry in `bernstein.yaml`.

The adapter expects the standard issue-alert envelope:

```json
{
  "action": "created",
  "data": {
    "issue": {
      "id": "12345",
      "title": "ZeroDivisionError: division by zero",
      "culprit": "app.handlers.divide in src/app.py",
      "shortId": "BACKEND-1A",
      "permalink": "https://sentry.example/issues/12345/",
      "metadata": {"value": "Specific arithmetic failure"}
    }
  },
  "project_slug": "backend"
}
```

`data.issue.id` becomes the fingerprint by default. Set
`fingerprint_path` to override.

## Setup: GitHub Actions failure

1. In repo settings, add a webhook for the `workflow_run` event.
2. Set the URL to
   `https://<your-host>/webhooks/telemetry/gha_failure/`.
3. Set the GitHub webhook secret and store it in
   `BERNSTEIN_GHA_WEBHOOK_SECRET`. The adapter verifies via the
   `X-Hub-Signature-256` header.
4. Flip `enabled: true` on the `gha_failure` entry.

The adapter only dispatches when `conclusion` is `failure`, `timed_out`,
or `action_required`. Success conclusions are accepted but skipped.

## Setup: stubbed sources (Datadog / Loki / custom JSONL)

The webhook endpoints exist but every event is recorded with outcome
`stubbed` until follow-up PRs land the production wiring. You can still
configure the endpoints today to exercise the audit chain and verify
your upstream signature setup:

```yaml
- source: datadog
  enabled: true
  endpoint: /webhooks/telemetry/datadog/
  secret_env: BERNSTEIN_DATADOG_WEBHOOK_SECRET
  cost_cap_usd: 0.20
```

The stub adapter normalises the top-level `fingerprint`, `title`,
`message`, `repo`, `environment`, and `url` fields, so a hand-rolled
caller can POST a JSON object with those keys and watch the audit log
fill in.

## Grounding retrieval

This MVP ships one retriever: `RecentJsonlLogRetriever`. It tails a
JSONL log file and returns the most-recent lines containing the event
fingerprint. The retriever is bounded:

- `max_lines` defaults to 50 (one pytest traceback's worth).
- `scan_bytes` defaults to 256 KiB; larger files are read from the tail.

Plug the retriever in at bootstrap:

```python
from pathlib import Path
from bernstein.core.autofix.telemetry_grounded import (
    RecentJsonlLogRetriever,
    load_telemetry_settings,
)
from bernstein.core.routes.telemetry_webhooks import configure_receiver

settings = load_telemetry_settings()
retriever = RecentJsonlLogRetriever(Path(".sdd/traces/events.jsonl"))
configure_receiver(
    app_state=app.state,
    settings=settings,
    retriever=retriever,
    dispatch_hook=my_dispatch_hook,
    audit=audit_log,
)
```

Trace and commit retrievers are tracked as follow-up tickets.

## Cost cap

Every event consults its source's `cost_cap_usd` *before* the dispatch
hook fires. A zero cap refuses to spawn. The dispatch hook is also
re-checked post-hoc: if the underlying agent reports spending past the
cap, the dispatcher flips the outcome to `cost_capped` so the operator
can intervene without losing the audit trailer.

## Audit trail

Every terminal outcome emits a single audit event:

```
event_type:   autofix.telemetry.dispatch
actor:        autofix-telemetry
resource_id:  <source>:<fingerprint>
details:      outcome, source, fingerprint, retriever_id,
              cost_usd, commit_sha, reason, log_lines,
              event_url, event_repo, event_environment
```

The `outcome` field is one of:

| Outcome      | Meaning |
|--------------|---------|
| `dispatched` | Hook was invoked, dispatch succeeded under the cap. |
| `stubbed`    | Adapter parsed event but dispatch wiring is deferred. |
| `skipped`    | Source disabled or event had no fingerprint. |
| `cost_capped`| Per-event cap refused the dispatch. |
| `errored`    | Dispatch hook raised; caught for daemon hygiene. |

The audit shape is identical across sources so the existing
`bernstein audit query` tooling consumes it without modification.

## Follow-ups

- Full Datadog Logs Webhook adapter (parses the embedded event body
  and pulls the log query URL).
- Loki Alertmanager adapter (parses the Alertmanager v2 envelope and
  groups on `groupKey`).
- Custom JSONL tail adapter (watches a file rotation instead of taking
  webhook deliveries).
- Multi-source fusion (correlate Sentry + Datadog into one event).
- Trace-store + git-history retrievers to complement the log retriever.
