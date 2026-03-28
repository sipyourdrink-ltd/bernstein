# 703 — Slack/Discord/Telegram Notifications

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

When Bernstein runs headless (CI, overnight), there's no way to know it finished without checking. GolemBot has Slack/Telegram/Discord/Feishu adapters. Every serious automation tool has webhook notifications. Users want a ping when agents finish, fail, or need approval.

## Design

### Webhook system
Generic webhook notification on events:
- `run.started` — agents spawning
- `task.completed` — individual task done
- `task.failed` — individual task failed
- `run.completed` — all tasks done, summary
- `budget.warning` — approaching budget cap
- `approval.needed` — waiting for human review

### Built-in formatters
- **Slack**: Block Kit message with task summary, cost, links
- **Discord**: Embed with color-coded status
- **Telegram**: Markdown message via bot API
- **Generic webhook**: JSON POST to any URL

### Configuration
```yaml
# bernstein.yaml
notifications:
  - type: slack
    webhook_url: ${SLACK_WEBHOOK_URL}
    events: [run.completed, task.failed, approval.needed]
  - type: webhook
    url: https://my-api.com/bernstein-events
    events: [run.completed]
```

## Files to modify

- `src/bernstein/core/notifications.py` (new)
- `src/bernstein/core/orchestrator.py`
- `tests/unit/test_notifications.py` (new)

## Completion signal

- Slack notification fires on run completion
- Discord notification fires on task failure
- Generic webhook works with any URL
- Tests pass for all formatters
