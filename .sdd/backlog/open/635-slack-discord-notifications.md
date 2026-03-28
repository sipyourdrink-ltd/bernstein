# 635 — Slack/Discord Notifications

**Role:** backend
**Priority:** 4 (low)
**Scope:** small
**Depends on:** none

## Problem

There is no way to receive notifications when agents complete tasks or runs finish. Enterprise teams expect Slack or Discord notifications for automated processes. Without notifications, users must manually check `bernstein status` or watch the terminal.

## Design

Build Slack and Discord notification integration for agent completion events. Use simple webhook-based delivery — no OAuth or bot complexity. Configure webhook URLs in `.sdd/config.toml` under `[notifications]`. Notify on: run started, task completed, task failed, run completed, and budget warning. Each notification includes: run ID, task summary, agent role, model used, cost, and duration. Format messages using Slack Block Kit / Discord embeds for rich display. Support filtering: notify only on failures, or on all events. Add a `bernstein notify test` command to verify webhook configuration. Keep the implementation minimal — webhook POST with JSON payload, no external dependencies.

## Files to modify

- `src/bernstein/core/notifications.py` (new)
- `src/bernstein/core/orchestrator.py`
- `.sdd/config.toml`
- `src/bernstein/cli/notify.py` (new)
- `tests/unit/test_notifications.py` (new)

## Completion signal

- Slack webhook receives formatted notifications on task completion
- Discord webhook receives formatted notifications on task completion
- `bernstein notify test` sends a test notification
