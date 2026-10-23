# Notifications

**Audience:** SREs wiring Bernstein into existing chat/alert systems.

**What:** Outbound, fire-and-forget notification drivers - Slack, Telegram,
Discord, Email, generic webhook, PagerDuty (via webhook), Jira (via webhook),
and shell command. Lifecycle events (`pre_task`, `post_task`, `pre_merge`,
`post_merge`, `pre_spawn`) are fanned out to every configured sink whose
filters match.

**Why:** Bernstein's chat-control bridges (Telegram/Slack/Discord drivers
under `core/chat/`) are *inbound* - a human attaches and drives a run
interactively. Long-running unattended runs need a *symmetric outbound* path
to push "task failed", "merge landed", "budget exceeded" back to humans
without hijacking the same chat. The notifications subsystem is that path.
See `core/notifications/protocol.py:1-22`.

---

## Architecture (5-second mental model)

```
LifecycleEvent  ->  NotifyLifecycleBridge  ->  NotificationDispatcher  ->  Sinks
                    (subscribes hooks)         (dedup + retry +            (Slack,
                                                dead-letter + audit)       Telegram,
                                                                           Email, ...)
```

Code anchors:

- Schema: `core/notifications/config.py:118-156`
- Bridge wiring: `core/lifecycle/notify_bridge.py:75-202`
- Retry/dedup/dead-letter: `core/notifications/bridge.py:46-67, 232-396`
- Driver registry: `core/notifications/registry.py:42-282`
- CLI: `cli/commands/notify_cmd.py:63-160`

State directory layout:

```
.sdd/runtime/notifications/
  dedup.jsonl          # event_id LRU window (default 6h, 2048 entries)
  dead_letter.jsonl    # rotated at 5 MB; rename suffix .<unix-ts>
```

---

## Drivers

Six first-party kinds ship in the wheel; third-party drivers register via
the `bernstein.notification_sinks` entry-point group
(`core/notifications/registry.py:37-49`).

### Common config shape (every sink)

```yaml
notifications:
  enabled: true
  retry:
    max_attempts: 4
    initial_delay_ms: 250
    backoff_factor: 2.0
    max_delay_ms: 30000
  sinks:
    - id: slack-ops          # unique per sink; required
      kind: slack            # driver kind; required
      enabled: true          # default true
      events: [post_task, post_merge]   # null = all
      severities: [warning, error]      # null = all
      labels: {team: platform}          # free-form tags
      # ... driver-specific keys ...
```

`${ENV_VAR}` substitution is supported on every string-typed driver field
(`core/notifications/sinks/slack.py:73-79`, etc.).

### `slack`

Incoming Webhook flavour - bot tokens / Block Kit are out of scope. Source:
`core/notifications/sinks/slack.py`.

```yaml
- id: slack-ops
  kind: slack
  webhook_url: ${SLACK_WEBHOOK_URL}     # required
  username: bernstein                   # optional
  icon_emoji: ":robot_face:"            # optional
  channel: "#ops-alerts"                # optional, overrides webhook default
  timeout_s: 10.0
```

### `telegram`

Reuses the in-process `TelegramBridge` so chat-mode and notify-mode share
one rate limiter. Source: `core/notifications/sinks/telegram.py`.

```yaml
- id: tg-oncall
  kind: telegram
  chat_id: "-1001234567890"             # required (negative for groups)
  token: ${BERNSTEIN_TG_TOKEN}          # required (or pass live bridge in code)
```

### `discord`

Incoming Webhook flavour, embed-formatted with severity colour mapping
(info → blue, warning → yellow, error → red). Source:
`core/notifications/sinks/discord.py:16-21`.

```yaml
- id: discord-ci
  kind: discord
  webhook_url: ${DISCORD_WEBHOOK_URL}   # required
  username: bernstein                   # optional
  avatar_url: https://...               # optional
```

### `email_smtp`

stdlib `smtplib` on a background thread; STARTTLS by default. Source:
`core/notifications/sinks/email_smtp.py`.

```yaml
- id: email-onfailure
  kind: email_smtp
  host: smtp.example.com                # required
  from_addr: bernstein@example.com      # required
  to_addrs:                             # required, non-empty
    - alice@example.com
    - ops@example.com
  port: 587                             # default 587
  username: ${SMTP_USER}                # optional
  password: ${SMTP_PASS}                # optional
  starttls: true
  ssl: false
  timeout_s: 15.0
```

### `webhook`

Generic JSON POST - body is `NotificationEvent.to_payload()`. Drop-in for
PagerDuty Events v2, Jira REST, Opsgenie, custom routers. Source:
`core/notifications/sinks/webhook.py`.

```yaml
# PagerDuty:
- id: pd-critical
  kind: webhook
  url: https://events.pagerduty.com/v2/enqueue
  headers:
    Content-Type: application/json
  events: [post_task]
  severities: [error]

# Jira:
- id: jira-bug
  kind: webhook
  url: ${JIRA_BASE_URL}/rest/api/3/issue
  headers:
    Authorization: Basic ${JIRA_BASIC_AUTH_B64}
    Content-Type: application/json
```

For PagerDuty's exact Events API v2 envelope, the legacy formatter
`core/communication/notifications.py:287-308` (`format_pagerduty`) renders
the routing-key/dedup-key payload - wrap your own webhook handler around
it if you need v2 schema compliance instead of the raw event dump.

### `shell`

Spawns `argv` with the JSON event payload on stdin and a pinned env. No
shell interpolation - safe by construction. Source:
`core/notifications/sinks/shell.py`.

```yaml
- id: pager-script
  kind: shell
  command: ["/usr/local/bin/page", "--severity"]
  env:
    EXTRA_KEY: literal-value
  timeout_s: 30
  non_zero_exit_is_permanent: false   # true = no retry on rc != 0
```

The child process gets `PATH`, `HOME`, `USER`, every `BERNSTEIN_*` var,
plus `BERNSTEIN_NOTIFY_{EVENT_ID,KIND,SEVERITY,SINK_ID}` and any `env:` keys
declared above (`core/notifications/sinks/shell.py:34, 110-124`).

---

## Event types

Defined in `core/notifications/protocol.py:40-55` and aligned 1:1 with
`bernstein.core.lifecycle.hooks.LifecycleEvent`:

| Kind          | Trigger                                                |
|---------------|--------------------------------------------------------|
| `pre_task`    | A task is about to be claimed and dispatched.          |
| `post_task`   | A task finished (success or failure; check severity).  |
| `pre_merge`   | Drain-merge cycle is about to start.                   |
| `post_merge`  | Drain-merge cycle finished.                            |
| `pre_spawn`   | Agent process is about to be spawned.                  |
| `post_spawn`  | Agent spawned (not subscribed by default).             |
| `synthetic`   | Test events from `bernstein notify test`.              |

Severity is derived from `BERNSTEIN_TASK_OUTCOME` in the lifecycle context
(`core/lifecycle/notify_bridge.py:332-344`):

- `failed` / `error`  -> `error`
- `warning` / `warn`  -> `warning`
- `rolled_back` on `post_task`/`post_merge`  -> `warning`
- otherwise          -> `info`

Each event carries a stable `event_id` derived from
`sha256(event|task|session|timestamp)[:32]` so a restart loop dedups
correctly (`core/lifecycle/notify_bridge.py:347-365`). Pre-compute and
inject via `ctx.env['BERNSTEIN_NOTIFY_EVENT_ID']` if you need explicit
control.

---

## `bernstein notify` group

Source: `cli/commands/notify_cmd.py`.

### `notify list`

Print every configured sink as JSON-per-line. No live state - purely
reflects the parsed YAML.

```console
$ bernstein notify list --config bernstein.yaml
{"id": "slack-ops", "kind": "slack", "enabled": true, "events": ["post_task"], "severities": null}
{"id": "tg-oncall", "kind": "telegram", "enabled": true, "events": null, "severities": ["error"]}
```

### `notify test --sink <id>`

Fire a synthetic event end-to-end through one driver. Useful before a
production run to verify webhooks resolve and tokens authorise.

```console
$ bernstein notify test --sink slack-ops --event synthetic
{"sink_id": "slack-ops", "event_id": "synthetic-slack-ops-1714857600000", "outcome": "dispatched"}
```

Flags:

- `--sink <id>` - required; matches `bernstein.yaml::notifications.sinks[*].id`.
- `--event <kind>` - defaults to `synthetic`. One of `pre_task`, `post_task`,
  `pre_merge`, `post_merge`, `pre_spawn`, `post_spawn`, `synthetic`.
- `--config <path>` - defaults to `./bernstein.yaml`.

The command builds an *isolated* `NotificationsConfig` containing only the
named sink, so a smoke test never accidentally spams every sink
(`cli/commands/notify_cmd.py:107-118`).

---

## Severity, filtering, dedup, retry

### Severity / event filtering

Per-sink `events` and `severities` arrays are AND-combined. `null`
(YAML omitted) means "all". Validation lives in
`core/notifications/config.py:95-115`:

- Allowed events: `pre_task`, `post_task`, `pre_merge`, `post_merge`,
  `pre_spawn`, `post_spawn`, `synthetic`.
- Allowed severities: `info`, `warning`, `error`.

The bridge filters in `core/lifecycle/notify_bridge.py:186-197` *before*
calling the dispatcher.

### Deduplication (rate-limit by content)

The dispatcher keeps an LRU + on-disk window keyed by `event_id`
(`core/notifications/bridge.py:94-174`). Defaults:

- `dedup_lru_size`: 2048 entries (in-memory)
- `dedup_window_seconds`: 21600 (6 hours)

Both knobs live under `notifications:` at top level in `bernstein.yaml`. A
restart re-seeds the LRU from `.sdd/runtime/notifications/dedup.jsonl` so a
crash loop is throttled too.

### Retry policy

Exponential backoff per sink, capped by `max_delay_ms`
(`core/notifications/bridge.py:70-91`):

```yaml
notifications:
  retry:
    max_attempts: 4         # 1 disables retry
    initial_delay_ms: 250
    backoff_factor: 2.0
    max_delay_ms: 30000
```

Drivers raise `NotificationDeliveryError` for transient failures (the
dispatcher retries) and `NotificationPermanentError` for non-retryable
ones (skip backoff, write straight to dead-letter)
(`core/notifications/protocol.py:77-92`).

### Dead-lettering

Permanent failures (and exhausted retries) append to
`.sdd/runtime/notifications/dead_letter.jsonl` as one JSON record per line
(`core/notifications/bridge.py:177-229`). Rotated to
`dead_letter.jsonl.<unix-ts>` past 5 MB.

### Audit

Every terminal outcome (`delivered` / `deduplicated` /
`failed_retrying` / `failed_permanent`) flows through an optional
`audit_hook` (`core/notifications/bridge.py:368-396`) so the orchestrator
can append a tamper-evident record to the HMAC chain shared with
`security.audit.AuditLog`. Wired by default when the bridge is constructed
via `build_bridge_from_config`
(`core/lifecycle/notify_bridge.py:229-297`).

---

## Registration diagnostics

The notification registry has separate namespaces for driver kinds and live
sink IDs. Duplicate registrations raise `ValueError` without replacing the
first value. This slice brings its duplicate diagnostics onto the shared
`DuplicateGuard`, naming the modules responsible for both registrations.
Unregistering a live sink permits a later registration of the same ID.
Built-in and entry-point loading keep their existing first-registration
precedence.

## Code pointers

- `src/bernstein/core/notifications/protocol.py:1-22` - design rationale
- `src/bernstein/core/notifications/protocol.py:40-92` - event/outcome enums + error classes
- `src/bernstein/core/notifications/protocol.py:95-187` - `NotificationEvent` + `NotificationSink` protocol
- `src/bernstein/core/notifications/config.py:46-156` - pydantic schema, validators
- `src/bernstein/core/notifications/bridge.py:94-229` - `DedupCache`, `DeadLetter`
- `src/bernstein/core/notifications/bridge.py:232-396` - `NotificationDispatcher`
- `src/bernstein/core/notifications/registry.py:42-282` - first-party kinds + entry-point loader
- `src/bernstein/core/notifications/sinks/slack.py` - Slack
- `src/bernstein/core/notifications/sinks/telegram.py` - Telegram
- `src/bernstein/core/notifications/sinks/discord.py` - Discord
- `src/bernstein/core/notifications/sinks/email_smtp.py` - SMTP email
- `src/bernstein/core/notifications/sinks/webhook.py` - generic JSON POST
- `src/bernstein/core/notifications/sinks/shell.py` - shell command
- `src/bernstein/core/lifecycle/notify_bridge.py` - lifecycle hook glue
- `src/bernstein/cli/commands/notify_cmd.py` - `bernstein notify {test,list}`
- `src/bernstein/core/communication/notifications.py:97-308` - legacy formatters
  (`format_slack`, `format_discord`, `format_telegram`, `format_webhook`,
  `format_pagerduty`) re-exported via `bernstein.core.notifications` for
  backwards compatibility
