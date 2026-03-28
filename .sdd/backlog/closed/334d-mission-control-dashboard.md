# 334d — Mission Control Observability Dashboard
**Role:** frontend  **Priority:** 1 (critical)  **Scope:** large

## Problem
5 sources: "I need a Mission Control for my local AI agents." Terminal logs unmanageable with 5+ agents.

## Design
Web dashboard: agent grid with status/progress/cost cards, task Gantt timeline, live per-agent logs, file lock map, merge queue, cost burn chart, alert panel.


---
**completed**: 2026-03-28 23:22:45
**task_id**: 5c08ace74a2c
**result**: Completed: 334d — Mission Control Observability Dashboard. Rebuilt /dashboard as a full Mission Control SPA with: (1) Agent grid showing status/progress/cost cards with per-task progress bars, (2) Task board + Gantt timeline tab, (3) File lock map tab showing owned_files per agent, (4) Live per-agent log streaming panel with auto-scroll, (5) Cost burn SVG chart + per-role breakdown, (6) Merge queue panel reading orchestrator state, (7) Alert panel for failed/blocked tasks and stale agents. Enhanced /dashboard/data API with file_locks, cost_history, merge_queue, alerts, and detailed agent data. Added TaskStore.metrics_jsonl_path public property. All 116 tests pass, ruff+pyright clean.
