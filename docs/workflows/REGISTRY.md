# Workflow Registry

This registry maps workflow specs in `docs/workflows/` to current implementation status in the codebase.

Last updated: 2026-04-08

---

## Canonical workflow specs

| Workflow spec | File | Runtime status | Notes |
|---|---|---|---|
| CI failure routing | `WORKFLOW-ci-failure-routing.md` | Partial | Core plumbing exists; depends on webhook/app setup |
| Event-driven triggers | `WORKFLOW-event-driven-triggers.md` | Partial | TriggerManager + source adapters are implemented |
| Env var isolation | `WORKFLOW-env-var-isolation.md` | Shipped | Adapter env filtering is active |
| Rate-limit-aware scheduling | `WORKFLOW-rate-limit-aware-scheduling.md` | Partial | Tracking/routing logic exists; tuning remains workload-specific |
| Self review before PR | `WORKFLOW-self-review-before-pr.md` | Partial | Reviewer/verifier paths exist; policy depends on config |
| Extension publishing | `WORKFLOW-extension-publishing.md` | Spec-only | Process spec; not orchestration core |
| Extension UX | `WORKFLOW-extension-ux.md` | Spec-only | Product UX spec |
| Protocol compatibility matrix | `WORKFLOW-protocol-compatibility-matrix.md` | Partial | Docs and checks exist; matrix should not be treated as source-of-truth for runtime health |
| Compatibility table generation | `WORKFLOW-compatibility-table-generation.md` | Partial | Documentation/support workflow |
| Release breaking-change detection | `WORKFLOW-release-breaking-change-detection.md` | Partial | CI/release process workflow |
| Context collapse with drain retry | `WORKFLOW-context-collapse-drain-retry.md` | Draft | T493 — bounded drain retry loop for spawn prompt context overflow |
| Permission mode hierarchy | `WORKFLOW-permission-mode-hierarchy.md` | Shipped | bypass→plan→auto→default mode hierarchy with severity relaxation + hook resolution |
| Verification nudge | `WORKFLOW-verification-nudge.md` | Shipped | Tracks unverified task completions and alerts when threshold exceeded |
| Event-sourced task transitions (CQRS) | `WORKFLOW-event-sourced-task-transitions.md` | Draft | Append-only event log per task; state derived by replaying events, not mutable status field |
| Multi-tenant task isolation (ENT-001) | `WORKFLOW-multi-tenant-task-isolation.md` | Approved | v1.2 — tenant-scoped CRUD, backlog, metrics. Implementation guidance for WAL scoping, tenant audit, quota wiring. Open Qs resolved. |
| Cluster node auth hardening (ENT-002) | `WORKFLOW-cluster-node-auth.md` | Approved | v1.2 — JWT auth for node reg/heartbeats. Implementation guidance for persistent revocation, user_id bypass fix, dead code cleanup, auth failure rate limiting. Open Qs resolved. |
| Cluster task stealing (ENT-007) | `WORKFLOW-cluster-task-stealing.md` | Draft | Pull-based task stealing with CAS locking — missing assigned_node/pinned_node fields, cooldown not persisted |
| Per-tenant rate limiting & quotas (ENT-008) | `WORKFLOW-tenant-rate-limiting-quota.md` | Draft | API rate limits, task/hour, agent concurrency, cost budget — TenantRateLimiter exists but not wired to middleware |

Archived/deprecated reference docs remain under `docs/workflows/archive/`.

---

## Code ownership map (current)

### Trigger and event workflows

- `src/bernstein/core/trigger_manager.py`
- `src/bernstein/core/trigger_sources/github.py`
- `src/bernstein/core/trigger_sources/slack.py`
- `src/bernstein/core/trigger_sources/file_watch.py`
- `src/bernstein/core/trigger_sources/webhook.py`
- `src/bernstein/core/routes/webhooks.py`
- `src/bernstein/core/routes/slack.py`

Trigger config path: `.sdd/config/triggers.yaml`

### CI and GitHub workflows

- `src/bernstein/core/ci_fix.py`
- `src/bernstein/core/ci_log_parser.py`
- `src/bernstein/github_app/mapper.py`
- `src/bernstein/github_app/webhooks.py`

### Context collapse and prompt budget workflows

- `src/bernstein/core/context_collapse.py` — 3-stage collapse pipeline (truncate, drop, strip)
- `src/bernstein/core/context_compression.py` — PromptCompressor fallback
- `src/bernstein/core/spawn_prompt.py` — prompt assembly + collapse integration
- `src/bernstein/core/tick_pipeline.py` — tick-level collapse entry point
- `src/bernstein/core/auto_compact.py` — circuit breaker for runtime compaction

### Retry, scheduling, and lifecycle workflows

- `src/bernstein/core/task_lifecycle.py`
- `src/bernstein/core/task_completion.py`
- `src/bernstein/core/lifecycle.py` — deterministic FSM, transition validation, event emission
- `src/bernstein/core/task_store.py` — in-memory task store, JSONL persistence, status indices
- `src/bernstein/core/models.py` — `TaskStatus` enum, `Task` dataclass, `LifecycleEvent`
- `src/bernstein/core/audit.py` — HMAC-chained audit log (overlaps with CQRS event store)
- `src/bernstein/core/rate_limit_tracker.py`
- `src/bernstein/core/router.py`
- `src/bernstein/core/cascade_router.py`

### Permission and approval workflows

- `src/bernstein/core/permission_mode.py` — mode enum, compatibility matrix, resolution
- `src/bernstein/core/permission_rules.py` — rule engine with severity-based evaluation
- `src/bernstein/core/permission_matrix.py` — hook-permission resolution matrix
- `src/bernstein/core/verification_nudge.py` — unverified-completion tracking and alerts

### Multi-tenant isolation workflows

- `src/bernstein/core/tenanting.py`
- `src/bernstein/core/tenant_isolation.py`
- `src/bernstein/core/tenant_rate_limiter.py`
- `src/bernstein/core/task_store.py` (tenant-scoped backlog/archive mirroring)
- `src/bernstein/core/routes/tasks.py` (tenant scope resolution, quota checks)
- `src/bernstein/core/routes/costs.py` (tenant-scoped cost queries)
- `src/bernstein/core/metric_collector.py` (tenant metrics mirroring)

Tenant config path: `bernstein.yaml` → `tenants:` section
Tenant data path: `.sdd/{tenant_id}/`

### Cluster auth and node registration workflows

- `src/bernstein/core/cluster.py`
- `src/bernstein/core/cluster_auth.py`
- `src/bernstein/core/jwt_tokens.py`
- `src/bernstein/core/routes/tasks.py` (cluster endpoints: /cluster/nodes/*)

Cluster config path: `ClusterAuthConfig` (code-level config, no file)

### Cluster task stealing workflows

- `src/bernstein/core/cluster_task_stealing.py` — TaskStealingEngine, cooldowns, steal history
- `src/bernstein/core/cluster.py` — TaskStealPolicy (find_steal_pairs), NodeRegistry
- `src/bernstein/core/routes/tasks.py` — POST /cluster/steal route
- `src/bernstein/core/task_store.py` — force_claim(), claim_next(), CAS versioning
- `src/bernstein/cli/worker_cmd.py` — WorkerLoop (claim/spawn cycle)

Config path: `cluster.steal` in `bernstein.yaml` (not yet parsed — hardcoded thresholds in route)

### Tenant rate limiting and quota workflows

- `src/bernstein/core/tenant_rate_limiter.py` — TenantRateLimiter (sliding-window checks, usage snapshots)
- `src/bernstein/core/tenanting.py` — TenantConfig, TenantRegistry, request_tenant_id()
- `src/bernstein/core/tenant_isolation.py` — TenantIsolationManager, TenantQuota
- `src/bernstein/core/rate_limiter.py` — RateLimitBucketConfig, endpoint-scoped limits
- `src/bernstein/core/auth_rate_limiter.py` — RequestRateLimitMiddleware (per-IP, not per-tenant)
- `src/bernstein/core/routes/tasks.py` — tenant-scoped task CRUD, quota check in POST /tasks
- `src/bernstein/core/routes/costs.py` — tenant-scoped cost queries

Config path: `tenants:` and `rate_limit:` sections in `bernstein.yaml`

### Review and quality workflows

- `src/bernstein/core/cross_model_verifier.py`
- `src/bernstein/core/reviewer.py`
- `src/bernstein/core/quality_gates.py`
- `src/bernstein/core/approval.py`
- `src/bernstein/core/janitor.py`

---

## Workflow maturity model

Use this interpretation when reading workflow specs:

- `Shipped`: implementation exists and is expected in normal runs.
- `Partial`: implementation exists, but behavior depends on config/environment and may require operator setup.
- `Spec-only`: workflow is documented for process/roadmap alignment, not guaranteed as turnkey runtime behavior.

---

## Maintenance rules

When updating this registry:

1. Prefer code paths and route modules as source of truth.
2. Do not mark a workflow `Shipped` unless it is active without one-off patches.
3. For trigger-related docs, use current source adapter names (`github`, `slack`, `file_watch`, `webhook`).
