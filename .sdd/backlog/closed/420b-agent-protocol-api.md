# 420b — Agent Protocol (Runs/Threads API) for External Integration
**Role:** backend  **Priority:** 3 (medium)  **Scope:** large

## Problem
Goose #6282: "No framework-agnostic API for executing agents, managing multi-turn state, or providing long-term memory."

## Design
Implement Agent Protocol spec: /runs (start/stop orchestration), /threads (multi-turn state), /store (memory). Enables external systems to drive Bernstein programmatically.


---
**completed**: 2026-03-29 00:06:01
**task_id**: d9bc8c377f54
**result**: Completed: [DECOMPOSE] [RETRY 2] [RETRY 1] 334b — Real-Time Cost Dashboard with Per-Agent Tracking. Created 5 atomic subtasks: 334b-01 (cost API), 334b-02 (web dashboard), 334b-03 (TUI widget), 334b-04 (history+alerts), 334b-05 (cost model).
