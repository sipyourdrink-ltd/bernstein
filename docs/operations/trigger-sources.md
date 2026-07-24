# Trigger sources: normalizing external events into tasks

`core/trigger_sources/` is the package of adapter modules that turn a raw
external event — a GitHub webhook, a Slack message, an OData row change, a
schedule fire — into a common `TriggerEvent` shape. Everything downstream
(rule matching, task creation, audit anchoring) works against that one
normalized shape instead of against N different payload formats.

```python
@dataclass
class TriggerEvent:
    source: str                       # "github", "slack", "schedule", ...
    timestamp: float
    raw_payload: dict[str, Any]
    repo: str = ""
    branch: str = ""
    sha: str = ""
    sender: str = ""
    changed_files: tuple[str, ...] = ()
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
```

(`src/bernstein/core/tasks/models.py`)

## Two independent paths from event to task

Bernstein does not route every event through one central dispatcher. Two
separate mechanisms consume `TriggerEvent`s, and which one applies depends
on the source:

**1. Direct task creation** — a handful of production HTTP routes normalize
the incoming payload and create a task immediately, bypassing the generic
trigger-rule pipeline entirely:

| Source | Route | Normalizer used |
|---|---|---|
| Slack Events API | `POST /webhooks/slack/events` | `trigger_sources/slack.py` (`normalize_slack_message`, `verify_slack_signature`) — a message that @-mentions the bot becomes a task directly. |
| SLA breach | internal (`schedule_supervisor.py` tick) | `trigger_sources/sla.py` (`normalize_sla_violation`) — feeds the schedule supervisor's own trigger sink. |
| Schedule fire | internal (`schedule_supervisor.py` tick) | `trigger_sources/schedule.py` (`normalize_schedule_fire`) — see [Recurring schedules](schedule.md). |
| OData row change | poll loop | `trigger_sources/odata_poll.py` — see [OData integration](odata.md). |

**2. The generic `TriggerManager` pipeline** — operator-authored rules in
`.sdd/config/triggers.yaml` match against `TriggerEvent.source` (plus
filters/conditions) and produce a task from a configurable template. This
is what `bernstein triggers list/history/fire` inspects and drives:

```yaml
# .sdd/config/triggers.yaml
defaults:
  max_tasks_per_minute: 5     # global rate limit across all triggers

triggers:
  - name: ci-failure-fix
    source: github_push
    enabled: true
    filters:
      branch: main
    conditions:
      cooldown_s: 300          # suppress refires within 5 minutes
    task:
      title: "Fix CI failure"
      role: backend
      priority: 1
      description_template: "Investigate: {message}"
```

`TriggerManager` (`core/orchestration/trigger_manager.py`) loads this file,
matches an incoming `TriggerEvent` against each enabled rule's `source` and
`filters` (glob matching on branch/file patterns), enforces the global
`defaults.max_tasks_per_minute` rate limit, per-trigger `conditions`
(`cooldown_s`, `max_retries`, dedup, and more), and a default excluded-sender
list (`bernstein[bot]`, `github-actions[bot]`), then returns task payloads
for the caller to submit.

## CLI

```
bernstein triggers list              # configured triggers + last-fired status
bernstein triggers history [-n N]    # recent fire log (default 20 entries)
bernstein triggers fire NAME         # synthesize a test event and dry-run it
```

`bernstein triggers fire` builds a synthetic `TriggerEvent` for the named
rule's `source`, runs it through `TriggerManager.evaluate()`, shows the task(s)
that would be created, and asks for confirmation before actually posting them
to the task server.

(`src/bernstein/cli/commands/triggers_cmd.py`)

## Adapter inventory and wiring status

The Notes on this feature name eight adapters. Not all of them sit on a live
event path in this codebase today — this table is the ground truth, not the
aspiration:

| Adapter | Module | Normalizes | Wired into a live path? |
|---|---|---|---|
| Slack | `trigger_sources/slack.py` | Events API message payloads | **Yes** — `POST /webhooks/slack/events` calls `normalize_slack_message` directly and creates a task. |
| Schedule | `trigger_sources/schedule.py` | An in-project schedule fire | **Yes** — via `schedule_supervisor.py`. See [Recurring schedules](schedule.md). |
| SLA | `trigger_sources/sla.py` | A signed SLA-violation receipt | **Yes** — via `schedule_supervisor.py`'s SLA monitor. |
| OData | `trigger_sources/odata_poll.py` | A polled system-of-record row change | **Yes**, own poll loop. See [OData integration](odata.md). |
| Generic webhook | `trigger_sources/receipt.py` (`automation_platforms.py`) | n8n / Zapier / Workato-style inbound payloads | **Yes** — `POST /webhook`. See [Automation bridge](../integrations/automation-bridge.md). |
| Discord | `trigger_sources/discord.py` | Slash-command interactions | **Partial** — `POST /webhooks/discord/interactions` uses `verify_discord_signature` from this module for request verification; command handling builds its response directly rather than through `normalize_discord_interaction`, which is unused in production. |
| GitHub | `trigger_sources/github.py` | Push / workflow_run / issues webhooks | **No** — the production route (`POST /webhooks/github`) maps events to tasks through the separate `bernstein.github_app.mapper` module instead. `github.py`'s `normalize_push` / `normalize_workflow_run` / `normalize_issues` are not called from any route; they are available to a custom `TriggerManager`-based rule if you feed them events yourself. |
| GitLab | `trigger_sources/gitlab.py` | Pipeline / job webhooks | **No** — same situation as GitHub: `POST /webhooks/gitlab` builds tasks directly, not through this module. |
| Generic HTTP | `trigger_sources/webhook.py` (`normalize_webhook`) | Arbitrary path/method/headers/payload | **No** — not called from any route in this codebase; the live generic-webhook endpoint (`POST /webhook`) creates a task from a task-shaped payload instead of normalizing through this function. |
| File watch | `trigger_sources/file_watch.py` (`FileWatchSource`) | Debounced filesystem change events (via `watchdog`) | **No** — the class is defined but never instantiated outside its own module; no orchestrator loop drains its queue. The unrelated `bernstein watch` CLI command does not use it either. |
| Claude Code Routine | `trigger_sources/routine.py` | A Routine session webhook | **No** — `normalize_routine_webhook` is defined but not called from any route. |

## Limitations

Roughly half of the named adapters (GitHub, GitLab, generic HTTP webhook,
file watch, Routine) are normalization functions with no production caller
in this codebase — they are usable if you wire them into a custom route or
a `TriggerManager` rule yourself, but out of the box nothing invokes them.
Treat the "wired" column above as authoritative over the module list; it
will drift as routes change, so re-check the call sites (`grep -rn
"from bernstein.core.trigger_sources"`) before depending on one of the
"No" rows.

## Source

`src/bernstein/core/trigger_sources/` (adapters); `src/bernstein/core/tasks/models.py`
(`TriggerEvent`, `TriggerConfig`, `TriggerTaskTemplate`); `src/bernstein/core/orchestration/trigger_manager.py`
(`TriggerManager`, `.sdd/config/triggers.yaml` loader); `src/bernstein/cli/commands/triggers_cmd.py`
(`bernstein triggers` CLI).
