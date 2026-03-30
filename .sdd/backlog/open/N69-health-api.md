# N69 — Health API

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Kubernetes deployments and load balancers have no way to check if the Bernstein task server is alive and ready to accept work, leading to traffic routed to unhealthy instances.

## Solution
- Add `/healthz` (liveness) endpoint that returns 200 if the process is running
- Add `/readyz` (readiness) endpoint that returns 200 only when all dependencies are healthy
- Readiness checks component status: database connectivity, adapter availability, provider reachability
- Return JSON response with per-component status (e.g., `{"db": "ok", "adapters": "ok", "providers": "degraded"}`)
- Return 503 with failing component details when not ready

## Acceptance
- [ ] `/healthz` returns 200 when the task server process is running
- [ ] `/readyz` returns 200 when all components are healthy
- [ ] `/readyz` returns 503 with details when any component is unhealthy
- [ ] Response JSON includes per-component status: db, adapters, providers
- [ ] Endpoints are compatible with Kubernetes liveness and readiness probes
- [ ] Endpoints respond within 5 seconds to avoid probe timeouts
