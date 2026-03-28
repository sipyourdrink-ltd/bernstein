# 334b — Real-Time Cost Dashboard with Per-Agent Tracking
**Role:** frontend  **Priority:** 0 (urgent)  **Scope:** medium

## Problem
6+ sources: cost opacity is #2 pain. $7K depleted in a day, 11% credits gone in 3 prompts.

## Design
TUI + web dashboard: live cost ticker per agent, run total with projection, budget bar, per-model breakdown, alerts at 80% budget, historical trends.


---
**completed**: 2026-03-28 23:22:45
**task_id**: eb03f5696c69
**result**: Completed: [RETRY 2] [RETRY 1] 334b — Real-Time Cost Dashboard with Per-Agent Tracking. Added GET /costs/live endpoint with per-agent and per-model breakdowns. TUI dashboard (BigStats) now shows live cost ticker, budget bar with color-coded thresholds (80%/95%/100%), burn rate ($/min), per-model breakdown. AgentWidget displays per-agent cost. Budget alerts fire as toast notifications at 80%, 95%, 100%. Classic live view (live.py) has cost sparkline, hourly projection, budget depletion ETA. All 92 cost/dashboard tests pass, ruff+pyright clean.
