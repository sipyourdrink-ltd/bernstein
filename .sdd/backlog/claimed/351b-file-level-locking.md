# 351b — File-Level Locking System
**Role:** backend  **Priority:** 1 (critical)  **Scope:** small

## Problem
Stoneforge: "File locking makes conflicts structurally impossible." Currently agents can edit same files.

## Design
Agents declare owned files at spawn (from task.owned_files or auto-detected). Orchestrator maintains lock table. Other agents see lock → work on something else. Locks release on task completion.
