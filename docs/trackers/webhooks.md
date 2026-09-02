# Tracker webhook ingestion

Bernstein can ingest tracker events via inbound webhooks instead of (or
alongside) the polling loop. Polling stays the default; webhook
ingestion is opt-in per adapter via `bernstein.yaml`.

The receiver verifies the per-tracker HMAC signature, deduplicates
deliveries using a bounded in-memory + on-disk ledger, and produces the
same normalised `Ticket` objects the polling path emits. The result
feeds into the orchestrator's existing task queue without code changes
downstream.

## Why webhooks

Polling intervals between 30 seconds and five minutes cap the time
between a tracker change and a Bernstein response. Webhooks remove that
floor and free a portion of the tracker's rate-limit budget that polling
spent on no-op fetches.

## Configuration

Add a `webhook` block under the tracker entry in `bernstein.yaml`:

```yaml
trackers:
  jira_cloud:
    webhook:
      enabled: true
      secret_env: JIRA_CLOUD_WEBHOOK_SECRET
      public_url_base: https://bernstein.example.com
```

| Key               | Required | Description |
|-------------------|----------|-------------|
| `enabled`         | yes      | When `false`, the route returns 503 so the tracker stops retrying. |
| `secret_env`      | yes      | Environment variable holding the shared HMAC secret. Resolved at request time so secret rotation does not require a restart. |
| `public_url_base` | advisory | Reverse-proxy URL operators register with the tracker. Documented here so the operator can paste it into the tracker UI. |

The webhook endpoint is `POST /webhooks/trackers/<adapter_name>` where
`<adapter_name>` is the short tracker name (`jira_cloud`, `github`,
`gitlab`, `linear`, `plane`, ...).

## Supported trackers

Built-in handlers ship for:

| Adapter      | Verification header               | Delivery-id header                  |
|--------------|-----------------------------------|-------------------------------------|
| `jira_cloud` | `X-Hub-Signature-256` (HMAC-SHA256, `sha256=` prefix) | `X-Atlassian-Webhook-Identifier` |
| `github`     | `X-Hub-Signature-256` (HMAC-SHA256, `sha256=` prefix) | `X-GitHub-Delivery`              |
| `gitlab`     | `X-Gitlab-Token` (constant-time compare)              | `X-Gitlab-Event-UUID`            |
| `linear`     | `Linear-Signature` (HMAC-SHA256, raw hex)             | `Linear-Delivery`                |
| `plane`      | `X-Plane-Signature` (HMAC-SHA256, raw hex)            | `X-Plane-Delivery`               |

Adapters that ship outside the core package can register handlers by
calling
`bernstein.core.trackers.webhook_receiver.register_handler(WebhookHandler(...))`
during import.

## Replay protection

Each delivery is keyed by the tracker-provided delivery id (header above)
or, when the tracker omits one, a SHA-256 of the raw body. The receiver
keeps the last 4096 ids in memory and appends an entry to
`.sdd/runtime/tracker_webhook_ledger.jsonl` so restarts do not lose
replay state. Re-delivery returns HTTP 200 with `status: replay` so the
tracker treats the duplicate as accepted without writing it through
again.

## Startup-poll recovery

On boot the orchestrator may call
`bernstein.core.trackers.webhook_receiver.replay_recent_via_poll` to
catch events that the tracker tried to deliver while Bernstein was
down. The helper runs a single poll, skips tickets at or below the
source's watermark, and feeds the rest into the same sink the webhook
route uses. Adapters that do not populate `raw["updated_at"]` simply
replay all open tickets, which is the safe default.

The watermark lives in a `PollWatermarks` store: an append-only JSONL
file, same shape as the replay ledger, read on construction and written
back after a poll that ran to completion. A restart therefore resumes
where the last poll stopped instead of replaying the same window. A
poll cut short by the wall-clock bound leaves the watermark alone, so
records it never reached are not skipped.

Pass `newest_first=True` for adapters whose `pull_open_tickets` yields
newest records first; the scan then stops at the first record at or
below the watermark instead of paginating the whole open-ticket set.
Left at the default the helper filters every record in Python, which is
correct for any ordering but costs a full scan.

`RateLimited` is retried with jittered backoff capped at
`backoff_cap_s`; the retry restarts the poll from the top, which is
safe when the sink upserts on the ticket id. `TicketUpsertSink` is the
supplied implementation: it keys on `Ticket.id` so a retried poll
replaces a record rather than appending a duplicate.

`replay_recent_via_poll_all` drives several sources in one sweep. Each
source polls inside its own `try`/`except`, so one unreachable resource
type is recorded in `PollSweepResult.errors` and the sweep continues;
`deadline_s` bounds the whole sweep so a single hung source cannot
starve the ones behind it.

## Reverse-proxy setup

Most trackers require an HTTPS endpoint. Two patterns work today:

### nginx / caddy

Forward `https://bernstein.example.com/webhooks/trackers/<adapter>` to
the bernstein server's `POST /webhooks/trackers/<adapter>` route. The
receiver consumes the raw body, so any proxy that preserves bytes will
work.

### ngrok / cloudflared

Operators running Bernstein on a laptop can use the existing tunnel
subsystem. Start the tunnel (`bernstein preview`, or any tunnel
provider registered under `bernstein.core.tunnels`) and paste the
resulting public URL plus `/webhooks/trackers/<adapter>` into the
tracker's webhook configuration UI.

## Verifying a delivery locally

```bash
curl -sX POST http://localhost:8000/webhooks/trackers/github \
  -H "x-github-event: issues" \
  -H "x-github-delivery: $(uuidgen)" \
  -H "x-hub-signature-256: sha256=$(python -c \
       'import hmac,hashlib,sys; print(hmac.new(b"shh", sys.stdin.buffer.read(), hashlib.sha256).hexdigest())' \
       < payload.json)" \
  --data-binary @payload.json
```

A 200 response with `status: accepted` confirms verification, parsing,
and dedup all succeeded. A 401 means the signature did not match; 503
means the endpoint is disabled or the `secret_env` variable is empty.
