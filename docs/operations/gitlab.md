# GitLab integration

Audience: operators wiring Bernstein to a GitLab project on
`gitlab.com` or a self-managed install. Mirrors the existing
`github_app/` surface for feature parity.

## Overview

`src/bernstein/gitlab_app/` provides the same set of building blocks
the GitHub App ships:

| Module | Purpose |
|--------|---------|
| `app.py` | Auth + base URL config; PAT / project access token |
| `webhooks.py` | Webhook parsing + constant-time token compare |
| `mapper.py` | Event-to-task conversion for MR + Note hooks |
| `slash_commands.py` | `/bernstein` slash command parser for MR / note comments |
| `ci_router.py` | CI pipeline routing (pipeline failures stay on their existing path) |
| `cost_reporter.py` | MR cost annotation (mirror of GitHub PR cost reporter) |
| `pipelines.py` | Commit-status client |

Webhook ingress lives at `/webhooks/gitlab` (already part of the task
server) and is wired by
`src/bernstein/core/routes/webhooks.py`.

## Required environment

| Variable | Purpose |
|----------|---------|
| `GITLAB_TOKEN` (or `GITLAB_PAT` fallback) | API token for the global default identity |
| `BERNSTEIN_GITLAB_URL` | Override base URL (default `https://gitlab.com`) - required for self-managed |
| `GITLAB_WEBHOOK_TOKEN` | Shared token registered with the GitLab project; required to enable `/webhooks/gitlab` |

Without `GITLAB_TOKEN` the app raises at construction time:
`GITLAB_TOKEN (or GITLAB_PAT) environment variable is required`.

Without `GITLAB_WEBHOOK_TOKEN` the webhook endpoint replies `503` with
a clear setup message - unauthenticated webhooks are never accepted.

## Self-managed installs

Set `BERNSTEIN_GITLAB_URL` to the instance base URL. Scheme rules:

- `https://` is required for production.
- `http://` is permitted only for localhost / loopback (so unit tests
  can spin up a fake server) and self-signed dev installs.

Invalid URLs log a warning and fall back to `https://gitlab.com`.

## Webhook auth flow

1. GitLab sends the configured shared token in `X-Gitlab-Token`.
2. Bernstein compares it against `GITLAB_WEBHOOK_TOKEN` with
   `hmac.compare_digest` (constant-time).
3. The optional `X-Bernstein-Timestamp` header is honoured for
   bernstein-internal relays. Real GitLab deliveries never send it, so
   real traffic is unaffected. When the header is present and skew
   exceeds the configured window the request fails closed with `401`.

## Supported events

| Hook | Mapped to |
|------|-----------|
| `Merge Request Hook` (opened / updated / merged / closed) | Task creation / lifecycle through `mapper.py` |
| `Note Hook` (MR comments) | Slash command parsing through `slash_commands.py` |
| `Pipeline Hook` (failure path) | Stays on the existing `_handle_gitlab_pipeline` route in `webhooks.py` |

## Slash commands

Parsed from MR / note bodies by `slash_commands.py`. The grammar is
the same `/bernstein <verb> <args>` shape the GitHub App uses. The
parser returns a normalised command struct that the dispatcher hands
to the orchestrator.

## Cost reporting

`cost_reporter.py` posts a cost summary as an MR comment using the
same shape as the GitHub PR cost reporter. The summary is a Markdown
table; rendering is snapshot-tested against fixtures under
`tests/integration/`.

## Examples

Smoke-test the webhook from a host that already has the env set:

```bash
curl -X POST http://localhost:8052/webhooks/gitlab \
  -H "X-Gitlab-Event: Merge Request Hook" \
  -H "X-Gitlab-Token: $GITLAB_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  --data @tests/integration/gitlab_fixtures/mr_opened.json
```

Hit a self-managed instance:

```bash
export BERNSTEIN_GITLAB_URL=https://gitlab.example.com
export GITLAB_TOKEN=...
```

## Troubleshooting

**`503` from `/webhooks/gitlab`.** `GITLAB_WEBHOOK_TOKEN` is unset. The
endpoint refuses to run unauthenticated.

**`401: Invalid GitLab webhook token`.** Token mismatch. Confirm the
exact token (no trailing newline) is registered both in GitLab's
project hook settings and in the orchestrator's env.

**`401: Stale or malformed X-Bernstein-Timestamp header`.** A relay
sent a stale timestamp. Either fix the relay's clock or stop sending
the header for real GitLab traffic.

**`GITLAB_TOKEN ... is required` at startup.** The app config is
strict; export `GITLAB_TOKEN` or `GITLAB_PAT` before launching the
task server.
