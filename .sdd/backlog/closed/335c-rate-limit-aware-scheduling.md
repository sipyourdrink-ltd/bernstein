# 335c — Rate-Limit-Aware Agent Scheduling
**Role:** backend  **Priority:** 1 (critical)  **Scope:** medium

## Problem
"Rate limits are the product." When one provider hits limits, agents stall.

## Design
Track rate limit status per provider. On 429: mark throttled for N seconds, rotate to next available. Spread agents across providers. Auto-recover when throttle period ends.


---
**completed**: 2026-03-28 23:33:18
**task_id**: 3dd26de0d8ab
**result**: Completed: 335c — Rate-Limit-Aware Agent Scheduling. Delivered WORKFLOW-rate-limit-aware-scheduling.md with 4 sub-workflows (A: 429 detection, B: throttle-aware spawn, C: recovery, D: spreading), 12 test cases, 6 Reality Checker findings (2 critical gaps in router.py and metrics.py), RateLimitTracker interface spec, handoff contracts, state transition map, and 5 open questions including retry-after semantics and throttle state persistence.
