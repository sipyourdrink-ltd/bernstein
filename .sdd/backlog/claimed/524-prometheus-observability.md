# 524 — Prometheus metrics + Grafana dashboard

**Role:** devops
**Priority:** 3 (medium)
**Scope:** small
**Depends on:** #519

## Problem

Metrics are local JSONL only. No way to monitor a running cluster, set alerts,
or visualize performance over time. Enterprise users expect standard observability.

## Design

### /metrics endpoint (Prometheus format)
```
# HELP bernstein_tasks_total Total tasks by status
# TYPE bernstein_tasks_total counter
bernstein_tasks_total{status="completed"} 150
bernstein_tasks_total{status="failed"} 22

# HELP bernstein_agent_active Active agents
# TYPE bernstein_agent_active gauge
bernstein_agent_active{role="backend"} 3

# HELP bernstein_task_duration_seconds Task completion time
# TYPE bernstein_task_duration_seconds histogram
bernstein_task_duration_seconds_bucket{le="60"} 45

# HELP bernstein_evolve_proposals_total Evolution proposals
# TYPE bernstein_evolve_proposals_total counter
bernstein_evolve_proposals_total{verdict="approve"} 12

# HELP bernstein_cost_usd_total Total API cost
# TYPE bernstein_cost_usd_total counter
bernstein_cost_usd_total 42.50
```

### Grafana dashboard
- Bundled JSON dashboard in `deploy/grafana/`
- Panels: task throughput, agent utilization, cost over time, evolve acceptance rate
- Importable via Grafana dashboard ID

### Implementation
- Use `prometheus_client` library (already standard in FastAPI ecosystem)
- Add metrics middleware to task server
- Export evolution cycle metrics from loop.py

## Files to modify
- `src/bernstein/core/server.py` — /metrics endpoint
- `pyproject.toml` — add prometheus_client dependency
- New: `deploy/grafana/dashboard.json`
- New: `deploy/prometheus/prometheus.yml` — scrape config example

## Completion signal
- `/metrics` returns valid Prometheus format
- Grafana dashboard loads and shows live data
