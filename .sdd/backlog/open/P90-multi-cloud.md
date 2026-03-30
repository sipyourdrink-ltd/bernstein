# P90 — Multi-Cloud Execution

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Locking agent execution to a single cloud provider limits flexibility, increases vendor risk, and prevents users from leveraging the best pricing or capabilities across clouds.

## Solution
- Build a cloud provider adapter abstraction layer with a common interface: `deploy_agent()`, `invoke_task()`, `get_status()`, `teardown()`
- Implement adapters for AWS Lambda, GCP Cloud Run, and Azure Container Instances
- Add cloud routing configuration in `bernstein.yaml`: specify preferred cloud per agent or let the scheduler choose based on cost/latency
- Scheduler selects cloud provider based on: explicit preference, cost estimate, latency SLA, and availability
- Credentials managed via environment variables or cloud-specific credential helpers
- Add `bernstein cloud status` showing agent deployments across all configured clouds
- Include fallback logic: if primary cloud fails, retry on next available provider

## Acceptance
- [ ] Cloud adapter interface defined with `deploy_agent`, `invoke_task`, `get_status`, `teardown`
- [ ] AWS Lambda adapter functional for agent deployment and task invocation
- [ ] GCP Cloud Run adapter functional for agent deployment and task invocation
- [ ] Azure Container Instances adapter functional for agent deployment and task invocation
- [ ] `bernstein.yaml` supports per-agent cloud preference configuration
- [ ] Scheduler routes to provider based on cost, latency, and availability
- [ ] `bernstein cloud status` shows cross-cloud deployment state
- [ ] Fallback to alternative provider on primary failure
