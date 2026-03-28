# 420c — AGENTS.md-Aware Task Routing
**Role:** backend  **Priority:** 3 (medium)  **Scope:** small

## Problem
Nx/Datadog: "Nested AGENTS.md are the default for monorepos but quite limited." Agents don't read project-level agent configs.

## Design
Read AGENTS.md / .cursorrules / CLAUDE.md per subdirectory. Auto-inject relevant rules into agent prompt based on which files the task touches. Monorepo-aware routing.


---
**completed**: 2026-03-29 00:10:45
**task_id**: 413e8aa4594d
**result**: [fast-path] ruff format: 1 file(s) reformatted in 0.1s
