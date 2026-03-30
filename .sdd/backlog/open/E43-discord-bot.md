# E43 — Discord Bot

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Teams using Discord for communication cannot trigger or monitor Bernstein runs without leaving their chat workspace.

## Solution
- Create a Discord bot at `integrations/discord-bot/` using `discord.py`.
- Listen for `!bernstein <goal>` messages in configured channels.
- Forward the goal to the Bernstein task server API.
- Reply with an embed containing: task status, duration, cost, and diff summary.
- Run as a lightweight standalone service with a single `bot.py` entry point.
- Include a `Dockerfile` and environment variable documentation for deployment.

## Acceptance
- [ ] `!bernstein <goal>` triggers a Bernstein run
- [ ] Bot replies with a formatted embed containing run results
- [ ] Bot runs as a standalone service via Docker
- [ ] README documents required environment variables and Discord app setup
