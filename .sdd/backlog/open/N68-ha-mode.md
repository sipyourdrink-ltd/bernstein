# N68 — High Availability Mode

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
The task server is a single point of failure — if it goes down, all in-flight orchestration stops with no automatic recovery, which is unacceptable for production enterprise workloads.

## Solution
- Implement active-passive HA for the task server
- Leader election via file lock (local) or Redis (distributed)
- On leader failure, standby instance detects loss and promotes itself
- In-flight tasks are recovered from persisted state on promotion
- Add `/health` endpoint for load balancer health checks
- Configurable heartbeat interval and failover timeout

## Acceptance
- [ ] Task server supports active-passive deployment with two instances
- [ ] Leader election works via file lock or Redis
- [ ] Standby promotes to leader on primary failure
- [ ] In-flight tasks are recovered after failover
- [ ] `/health` endpoint returns leader/standby status for load balancers
- [ ] Heartbeat interval and failover timeout are configurable
