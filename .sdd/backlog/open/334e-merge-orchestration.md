# 334e — Merge Orchestration with Conflict Prevention
**Role:** backend  **Priority:** 1 (critical)  **Scope:** medium

## Problem
4 sources: merge chaos when agents collide. No sequential merge-to-branch workflow.

## Design
FIFO merge queue: sequential merge → test → next. File-level locking at spawn. Conflict detection before merge attempt. Auto-create conflict resolution task on conflict.
