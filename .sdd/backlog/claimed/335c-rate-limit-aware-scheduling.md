# 335c — Rate-Limit-Aware Agent Scheduling
**Role:** backend  **Priority:** 1 (critical)  **Scope:** medium

## Problem
"Rate limits are the product." When one provider hits limits, agents stall.

## Design
Track rate limit status per provider. On 429: mark throttled for N seconds, rotate to next available. Spread agents across providers. Auto-recover when throttle period ends.
