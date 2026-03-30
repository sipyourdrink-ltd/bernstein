# N70 — Grafana Dashboard Bundle

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Operations teams using Grafana must build Bernstein dashboards from scratch, duplicating effort across organizations and delaying observability for production deployments.

## Solution
- Create pre-built Grafana dashboard JSON files covering key Bernstein metrics
- Dashboards include: task throughput, cost burn rate, agent utilization, error rate, latency percentiles (p50/p95/p99)
- Implement `bernstein dashboard export --grafana` to output the dashboard JSON
- Dashboards use Prometheus-compatible metric names
- Include variables for environment, workspace, and time range filtering

## Acceptance
- [ ] Pre-built Grafana dashboard JSON files exist for all key metrics
- [ ] Dashboards cover: task throughput, cost burn, agent utilization, error rate, latency p50/p95/p99
- [ ] `bernstein dashboard export --grafana` outputs importable JSON
- [ ] Dashboards use Prometheus-compatible metric names
- [ ] Dashboard variables support filtering by environment, workspace, and time range
