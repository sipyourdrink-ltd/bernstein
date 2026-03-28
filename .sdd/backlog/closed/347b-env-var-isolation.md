# 347b — Environment Variable Isolation for Agents
**Role:** backend  **Priority:** 1 (critical)  **Scope:** small

## Problem
NVIDIA: "env variable leakage is the biggest blind spot." Agents read all secrets.

## Design
Filtered environment per agent: ALLOW only PATH, HOME, LANG + agent-specific API key. DENY all other secrets. Configurable allowlist per role.


---
**completed**: 2026-03-28 23:58:50
**task_id**: 5b6fbfc0011c
**result**: Completed: 347b — Environment Variable Isolation for Agents
