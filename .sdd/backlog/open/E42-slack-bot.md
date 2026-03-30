# E42 — Slack Bot

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Teams cannot trigger Bernstein runs from Slack, requiring context-switching to a terminal or CI system for every task.

## Solution
- Create a Slack bot at `integrations/slack-bot/` using the Slack Bolt SDK (Python).
- Listen for the `/bernstein <goal>` slash command.
- Forward the goal to the Bernstein task server API and acknowledge the command immediately.
- Post results as a thread reply with a summary card (Block Kit) including: task status, duration, cost, diff summary.
- Include a `manifest.yaml` for Slack app configuration and deployment instructions.

## Acceptance
- [ ] `/bernstein <goal>` slash command triggers a Bernstein run
- [ ] Bot acknowledges the command within 3 seconds
- [ ] Results are posted as a threaded reply with a formatted summary card
- [ ] Deployment instructions cover both local development and production hosting
