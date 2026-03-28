# 420b — Agent Protocol (Runs/Threads API) for External Integration
**Role:** backend  **Priority:** 3 (medium)  **Scope:** large

## Problem
Goose #6282: "No framework-agnostic API for executing agents, managing multi-turn state, or providing long-term memory."

## Design
Implement Agent Protocol spec: /runs (start/stop orchestration), /threads (multi-turn state), /store (memory). Enables external systems to drive Bernstein programmatically.
