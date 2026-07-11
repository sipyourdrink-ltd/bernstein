# REST API Reference

Bernstein exposes a task-server HTTP API on `http://127.0.0.1:8052` by default. The full OpenAPI 3.1 specification is available at `/openapi.json` when the server is running.

This page is the canonical hand-maintained reference. It covers ~216 HTTP/WebSocket endpoints across 58 route files, plus 13 MCP tools. Endpoints requiring authentication are marked with `Y` in the **Auth** column.

## Generating the spec

Use the included script to regenerate `docs/openapi.json` from the FastAPI app definition without starting the server:

```bash
uv run python scripts/generate_openapi.py
# Written docs/reference/openapi.json  (216 paths, 72 schemas)
```

Run this after adding or modifying any API route, Pydantic model, or response schema, then commit the updated JSON. The hosted Redoc page reads the spec at load time, so the rendered reference updates automatically once the JSON is committed.

**Alternative -- fetch from a running server:**

```bash
bernstein run &
curl -s http://127.0.0.1:8052/openapi.json > docs/reference/openapi.json
```

> The OpenAPI JSON declares each route at both its bare path (e.g. `/tasks`) and an `/api/v1/`-prefixed alias (e.g. `/api/v1/tasks`). Both are live; pick one prefix per client.

## Authentication

When auth is enabled (`BERNSTEIN_AUTH_ENABLED=true`), endpoints marked `Auth: Y` require a Bearer token:

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8052/tasks
```

Public endpoints (no auth required): `/health`, `/health/ready`, `/health/live`, `/.well-known/agent.json`, `/.well-known/acp.json`, `/docs`, `/openapi.json`, plus loopback connections from `127.0.0.1` (for local CLI use).

Bearer tokens are issued via the CLI device flow (`/auth/cli/device`, `/auth/cli/authorize`, `/auth/cli/token`).

---

## Health and lifecycle

Source: `core/routes/status_lifecycle.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/health` | `health` | N | Server liveness probe; returns 200 when up |
| GET | `/health/ready` | `readiness` | N | Readiness probe; checks dependencies |
| GET | `/ready` | `readiness` | N | Alias for `/health/ready` |
| GET | `/health/live` | `liveness` | N | Liveness probe |
| GET | `/alive` | `liveness` | N | Alias for `/health/live` |
| GET | `/health/deps` | `dependency_health` | N | Per-dependency health (DB, Redis, providers) |
| POST | `/config` | `update_config` | Y | Hot-reload server configuration |
| POST | `/shutdown` | `shutdown` | Y | Graceful server shutdown |
| GET | `/cache-stats` | `cache_stats` | N | Prompt-cache hit/miss stats |
| GET | `/metrics` | `prometheus_metrics` | N | Prometheus scrape endpoint |

---

## Status and dashboard

Source: `core/routes/status_dashboard.py`, `core/routes/status_events.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/status` | `status_summary` | N | Dashboard snapshot (agents, tasks, metrics) |
| GET | `/status/duration-predictions` | `duration_predictions` | N | Predicted completion time per task |
| GET | `/routing/bandit` | `bandit_state` | N | Cascade-router bandit state inspection |
| GET | `/dashboard` | `dashboard_html` | N | Web dashboard HTML page |
| GET | `/dashboard/data` | `dashboard_data` | N | JSON payload for the dashboard |
| GET | `/events` | `events_stream` | N | Server-Sent Events stream for live updates |
| GET | `/badge.json` | `badge_json` | N | Shields.io-compatible status badge |
| GET | `/memory/audit` | `memory_audit` | Y | Inspect memory store for audit |
| POST | `/broadcast` | `broadcast_message` | Y | Push a message to all connected clients |

---

## Tasks

Source: `core/routes/task_crud.py`, `core/routes/paginated_tasks.py`, `core/routes/batch_ops.py`, `core/routes/task_detail.py`.

### CRUD and search

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| POST | `/tasks` | `create_task` | N | Create a new task |
| POST | `/tasks/batch` | `create_tasks_batch` | N | Create many tasks in one call |
| POST | `/tasks/import` | `import_tasks` | N | Import tasks from a YAML/JSON file |
| GET | `/tasks` | `list_tasks` | N | List tasks (filter by `status`, `role`, etc.) |
| GET | `/tasks/counts` | `task_counts` | N | Counts per status (open/claimed/done/failed) |
| GET | `/tasks/archive` | `archived_tasks` | N | List archived tasks |
| GET | `/tasks/graph` | `task_graph` | N | Dependency graph between tasks |
| GET | `/tasks/search` | `search_tasks` | N | Full-text search over task corpus |
| GET | `/tasks/{task_id}` | `get_task` | N | Fetch a single task by ID |
| GET | `/tasks/{task_id}/logs` | `task_logs` | N | Static log dump for a task |
| GET | `/tasks/{task_id}/snapshots` | `task_snapshots` | N | Persistence snapshots taken during the run |
| GET | `/tasks/{task_id}/partial-merge` | `partial_merge` | N | Partial-merge diff for a long-running task |
| PATCH | `/tasks/{task_id}` | `update_task` | N | Update task fields (priority, scope, etc.) |

### Lifecycle operations

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| POST | `/tasks/{task_id}/approve` | `approve_task` | N | Mark a task as approved for execution |
| POST | `/tasks/{task_id}/reject` | `reject_task` | N | Reject a task; halts execution |
| POST | `/tasks/{task_id}/progress` | `report_progress` | N | Heartbeat with files/tests/errors |
| POST | `/tasks/{task_id}/claim` | `claim_task` | N | Claim a task for an agent session |
| POST | `/tasks/{task_id}/complete` | `complete_task` | N | Mark task completed (success) |
| POST | `/tasks/{task_id}/fail` | `fail_task` | N | Mark task failed |
| POST | `/tasks/{task_id}/cancel` | `cancel_task` | N | Cancel a queued or in-flight task |
| POST | `/tasks/{task_id}/requeue` | `requeue_task` | N | Re-queue a failed task |
| POST | `/tasks/{task_id}/archive` | `archive_task` | N | Archive a closed task |
| POST | `/tasks/{task_id}/split` | `split_task` | N | Split into subtasks |
| POST | `/tasks/{task_id}/retry` | `retry_task` | N | Retry a failed task |

### Batch operations

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| POST | `/tasks/claim-batch` | `claim_batch` | N | Claim multiple tasks atomically |
| POST | `/tasks/batch-ops` | `batch_ops` | N | Mixed lifecycle ops in one request |

### Streaming and dashboard views

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/dashboard/tasks/{task_id}` | `dashboard_task_detail` | N | Dashboard task detail JSON |
| GET | `/dashboard/tasks/{task_id}/logs/stream` | `task_log_stream` | N | SSE log stream for a task |
| GET | `/dashboard/file_locks` | `file_locks_view` | N | Cross-task file-lock state for the dashboard |

---

## Agents and team

Source: `core/routes/agents.py`, `core/routes/team.py`, `core/routes/agent_comparison.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/agents` | `list_agents` | N | List active agent sessions |
| POST | `/agents/{session_id}/kill` | `kill_agent` | Y | Force-terminate an agent process |
| GET | `/agents/{session_id}/stream` | `agent_stream` | N | SSE stream of agent stdout/stderr |
| GET | `/agents/{session_id}/logs` | `agent_logs` | N | Static log dump for an agent |
| POST | `/agents/{agent_id}/heartbeat` | `agent_heartbeat` | N | Liveness ping from the agent process |
| GET | `/agents/comparison` | `agent_comparison` | N | A/B-test comparison view |
| GET | `/team` | `team_overview` | N | Logical "team" of agents working on a goal |
| GET | `/team/active` | `team_active` | N | Currently-active team sessions |
| GET | `/team/{team_id}` | `team_by_id` | N | Single team detail |
| GET | `/team/dashboard` | `team_dashboard` | N | Dashboard JSON for team view |

---

## WebSocket

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| WS | `/ws` | `websocket_endpoint` | N | Primary streaming surface for agent + task events |

The `/ws` endpoint is the recommended subscription channel for live UI; use SSE endpoints (`/events`, `/agents/{id}/stream`) for unidirectional consumers.

---

## Plans and graph

Source: `core/routes/plans.py`, `core/routes/graph.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/plans` | `list_plans` | N | List known plan files |
| GET | `/plans/active` | `active_plan` | N | The currently-executing plan |
| POST | `/plans` | `create_plan` | N | Create a new plan from goal + scope |
| POST | `/plans/validate` | `validate_plan` | N | Validate plan YAML against schema |
| GET | `/graph/impact` | `impact_graph` | N | Dependency-impact analysis for a change |

---

## Quality

Source: `core/routes/quality.py`, `core/routes/file_health.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/quality` | `quality_overview` | N | Top-level quality metrics |
| GET | `/quality/budget-forecast` | `quality_budget_forecast` | N | Predicted quality-budget burn |
| GET | `/quality/trend` | `quality_trend` | N | Trend over time |
| GET | `/quality/models` | `quality_per_model` | N | Quality breakdown per model |
| GET | `/quality/file-health` | `file_health_overview` | N | Per-file health snapshot |
| GET | `/quality/file-health/flagged` | `flagged_files` | N | Files flagged for review |
| GET | `/quality/file-health/{path}` | `file_health_detail` | N | Drill-down for one file |

---

## Observability and costs

Source: `core/routes/observability.py`, `core/routes/costs.py`, `core/routes/provider_latency.py`, `core/routes/predictive.py`, `core/routes/custom_metrics.py`, `core/routes/grafana.py`, `core/routes/slo.py`.

### Observability

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/observability/agents` | `obs_agents` | N | Per-agent metrics |
| GET | `/observability/effectiveness` | `obs_effectiveness` | N | Effectiveness scoring |
| GET | `/observability/recommendations` | `obs_recommendations` | N | Tuning recommendations from telemetry |
| GET | `/observability/budget` | `obs_budget` | N | Budget burn-down view |
| GET | `/observability/deps` | `obs_deps` | N | External dependency health timeline |
| GET | `/observability/token-histogram` | `obs_token_histogram` | N | Token-usage histogram |
| GET | `/observability/queue-depth` | `obs_queue_depth` | N | Task-queue depth over time |
| GET | `/observability/timeline` | `obs_timeline` | N | Combined event timeline |
| GET | `/observability/incidents` | `obs_incidents` | N | List incidents detected by anomaly detector |
| GET | `/observability/token-breakdown` | `obs_token_breakdown` | N | Tokens broken down by role/agent/task |
| GET | `/observability/incident-timeline/{incident_id}` | `obs_incident_timeline` | N | Per-incident timeline |
| GET | `/recap` | `daily_recap` | N | Daily/weekly recap |
| GET | `/changelog` | `auto_changelog` | N | Auto-generated changelog from runs |
| GET | `/events/cost` | `cost_events_stream` | N | SSE stream of cost events |

### Costs

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/costs` | `costs_overview` | N | Aggregate cost view |
| GET | `/costs/live` | `costs_live` | N | Live cost gauge |
| GET | `/costs/current` | `costs_current` | N | Current run costs |
| GET | `/costs/alerts` | `costs_alerts` | N | Active cost alerts |
| GET | `/costs/history` | `costs_history` | N | Historical cost series |
| GET | `/costs/{run_id}` | `costs_for_run` | N | Cost detail for a run |
| GET | `/costs/export` | `costs_export` | N | CSV export for external billing |
| GET | `/costs/forecast` | `costs_forecast` | N | Budget-forecast predictions |
| GET | `/costs/compare` | `costs_compare` | N | Compare two runs |
| GET | `/costs/cache-stats` | `costs_cache_stats` | N | Prompt-cache hit value (USD) |
| GET | `/costs/model-comparison` | `costs_model_comparison` | N | Cross-model cost comparison |
| GET | `/costs/token-efficiency` | `costs_token_efficiency` | N | Tokens per accepted change |

### Provider latency and predictions

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/metrics/provider-latency` | `provider_latency` | N | Latency per provider |
| GET | `/metrics/provider-latency/history` | `provider_latency_history` | N | Time series |
| GET | `/metrics/predictions` | `metric_predictions` | N | Predictive metric output |
| GET | `/metrics/custom` | `custom_metrics` | N | User-defined metrics |
| GET | `/metrics/custom/schema` | `custom_metrics_schema` | N | Custom-metric schema introspection |

### Grafana and SLO

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/grafana/dashboard` | `grafana_dashboard` | N | Grafana JSON model for the Bernstein dashboard |
| GET | `/slo` | `slo_overview` | N | SLO status |
| GET | `/slo/budget` | `slo_budget` | N | Error-budget remaining |
| GET | `/slo/burndown` | `slo_burndown` | N | Burndown chart data |
| POST | `/slo/reset` | `slo_reset` | Y | Reset SLO budgets after an incident |

---

## Webhooks and chat

Source: `core/routes/webhooks.py`, `core/routes/notifications.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| POST | `/webhook` | `generic_webhook` | N | Generic inbound webhook receiver |
| POST | `/webhooks/github` | `github_webhook` | N | GitHub events (signature-verified) |
| POST | `/webhooks/gitlab` | `gitlab_webhook` | N | GitLab events |
| POST | `/webhooks/slack/commands` | `slack_command` | N | Slack slash-command receiver |
| POST | `/webhooks/slack/events` | `slack_event` | N | Slack Events API receiver |
| POST | `/webhooks/discord/interactions` | `discord_interaction` | N | Discord interaction receiver |
| GET | `/alerts` | `list_alerts` | N | Active alerts emitted to chat sinks |

---

## Workspace, GraphQL, hooks, identities

Source: `core/routes/workspace.py`, `core/routes/graphql.py`, `core/routes/hooks.py`, `core/routes/identities.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/workspace` | `get_workspace` | N | Read workspace state |
| POST | `/workspace` | `update_workspace` | N | Update workspace state |
| POST | `/graphql` | `graphql_endpoint` | N | GraphQL query interface |
| POST | `/hooks/{session_id}` | `session_hook` | N | Per-session hook receiver |
| GET | `/identities` | `list_identities` | Y | List configured identities |
| GET | `/identities/{id}` | `get_identity` | Y | Fetch identity by ID |
| POST | `/identities/{id}/revoke` | `revoke_identity` | Y | Revoke an identity |
| GET | `/identities/{id}/audit` | `identity_audit` | Y | Audit trail for an identity |

---

## ACP and A2A protocols

Source: `core/routes/acp.py`, `core/routes/a2a.py`.

### ACP (Agent Client Protocol)

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/.well-known/acp.json` | `acp_well_known` | N | ACP service discovery |
| GET | `/acp/v0/agents` | `acp_list_agents` | N | List ACP agents |
| GET | `/acp/v0/agents/{id}` | `acp_get_agent` | N | ACP agent detail |
| POST | `/acp/v0/runs` | `acp_create_run` | N | Start an ACP run |
| GET | `/acp/v0/runs/{id}` | `acp_get_run` | N | Get ACP run status |
| DELETE | `/acp/v0/runs/{id}` | `acp_cancel_run` | N | Cancel ACP run |

### A2A (Agent-to-Agent)

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/.well-known/agent.json` | `a2a_well_known` | N | A2A service discovery |
| GET | `/a2a/agents` | `a2a_list_agents` | N | List A2A agents |
| POST | `/a2a/agents/{id}/tasks` | `a2a_post_task` | N | Submit a task to an A2A agent |
| POST | `/a2a/tasks/send` | `a2a_send_task` | N | Top-level task-send entry |
| GET | `/a2a/tasks/{id}` | `a2a_get_task` | N | Fetch A2A task status |
| POST | `/a2a/tasks/{id}/subscribe` | `a2a_subscribe` | N | Subscribe to A2A task updates |

---

## Auth

Source: `core/routes/auth.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/auth/providers` | `list_providers` | N | List configured auth providers |
| GET | `/auth/oidc/callback` | `oidc_callback` | N | OIDC redirect callback |
| POST | `/auth/saml/acs` | `saml_acs` | N | SAML Assertion Consumer Service |
| GET | `/auth/saml/metadata` | `saml_metadata` | N | SAML SP metadata |
| POST | `/auth/cli/device` | `cli_device_init` | N | Initiate CLI device-flow login |
| POST | `/auth/cli/token` | `cli_device_token` | N | Exchange device code for token |
| POST | `/auth/cli/authorize` | `cli_authorize` | N | Authorize a pending device |
| GET | `/auth/login` | `login` | N | Interactive login |
| GET | `/auth/me` | `current_user` | Y | Authenticated-user profile |
| POST | `/auth/logout` | `logout` | Y | Revoke current session |
| GET | `/auth/group-mappings` | `group_mappings` | Y | OIDC/SAML group -> role mappings |
| GET | `/auth/users` | `list_users` | Y | List users (admin) |

---

## Approvals

Source: `core/routes/approvals.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/approvals` | `list_approvals` | Y | List pending approvals |
| POST | `/approvals/{id}/approve` | `approve` | Y | Approve a pending request |
| POST | `/approvals/{id}/reject` | `reject` | Y | Reject a pending request |
| GET | `/approvals/queue` | `approvals_queue` | Y | Approvals queue view |
| POST | `/approvals/queue/{id}/resolve` | `resolve_queue_item` | Y | Resolve a queued approval |
| GET | `/approvals/live-fragment` | `live_fragment` | N | HTMX live-fragment for the dashboard |

---

## Audit, drain, export, SBOM

Source: `core/routes/audit_log.py`, `core/routes/drain.py`, `core/routes/export.py`, `core/routes/sbom.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/audit` | `audit_log` | Y | HMAC-chained audit log |
| POST | `/drain` | `start_drain` | Y | Begin a graceful drain |
| GET | `/drain` | `drain_status` | Y | Drain status |
| POST | `/drain/cancel` | `cancel_drain` | Y | Cancel an in-progress drain |
| GET | `/export/tasks` | `export_tasks` | Y | Export task history |
| GET | `/export/agents` | `export_agents` | Y | Export agent history |
| POST | `/sbom` | `generate_sbom` | Y | Generate Software Bill of Materials |
| GET | `/sbom` | `get_sbom` | Y | Retrieve last-generated SBOM |

---

## Sandbox and cluster

Source: `core/routes/sandbox.py`, `core/routes/task_cluster.py`.

### Sandbox sessions

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| GET | `/packs` | `list_packs` | N | List installed sandbox packs |
| POST | `/packs/{id}/sessions` | `create_session` | N | Spawn a session from a pack |
| GET | `/sessions` | `list_sessions` | N | List active sandbox sessions |
| GET | `/sessions/{id}` | `get_session` | N | Session detail |
| POST | `/sessions/{id}/exec` | `exec_in_session` | N | Run a command in a session |
| GET | `/sessions/{id}/output` | `session_output` | N | Stream session output |

### Cluster

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| POST | `/cluster/nodes` | `register_node` | Y | Register a worker node (replaces legacy `/cluster/register`) |
| GET | `/cluster/nodes` | `list_nodes` | Y | List registered nodes |
| POST | `/cluster/nodes/{node_id}/heartbeat` | `node_heartbeat` | Y | Per-node heartbeat (replaces legacy `/cluster/heartbeat`) |
| POST | `/cluster/nodes/{node_id}/cordon` | `cordon_node` | Y | Mark node unschedulable |
| POST | `/cluster/nodes/{node_id}/uncordon` | `uncordon_node` | Y | Mark node schedulable |
| POST | `/cluster/nodes/{node_id}/drain` | `drain_node` | Y | Drain in-flight work off a node |
| DELETE | `/cluster/nodes/{node_id}` | `deregister_node` | Y | Remove a node from the cluster |
| GET | `/cluster/status` | `cluster_status` | Y | Cluster-wide status (replaces legacy `/cluster/topology`) |
| POST | `/cluster/steal` | `steal_tasks` | Y | Re-balance by stealing tasks from another node |

> **Note on legacy paths.** Earlier versions of this reference listed `POST /cluster/register`, `POST /cluster/heartbeat`, and `GET /cluster/topology`. None of those exist in the current codebase. Use the paths above.

---

## Bulletin and channel

Source: `core/routes/bulletin.py`, `core/routes/channel.py`.

| Method | Path | Handler | Auth | Purpose |
|--------|------|---------|------|---------|
| POST | `/bulletin` | `post_bulletin` | N | Post a cross-agent finding or blocker |
| GET | `/bulletin` | `read_bulletin` | N | Read bulletins (filter by `since`) |
| POST | `/channel/query` | `channel_query` | N | One-shot query on a channel |
| POST | `/channel/subscribe` | `channel_subscribe` | N | Subscribe to a channel |
| GET | `/channel/queries` | `list_channel_queries` | N | List recent queries |
| GET | `/channel/query/{id}` | `get_channel_query` | N | Fetch a specific query result |

---

## MCP tools

The MCP tools below are exposed via Bernstein's MCP server (`mcp/server.py`), not over HTTP. They are callable from any MCP-aware client (Claude Desktop, Cursor, etc.) once the MCP server is registered.

| Tool name | Purpose |
|-----------|---------|
| `bernstein_run` | Start an orchestration run from a goal |
| `bernstein_status` | Fetch current task/agent status |
| `bernstein_tasks` | List tasks with filtering |
| `bernstein_cost` | Cost summary for the current run |
| `bernstein_stop` | Stop the running orchestrator |
| `bernstein_approve` | Approve a pending task or request |
| `bernstein_create_subtask` | Create a subtask under an existing task |
| `bernstein_health` | Health check |
| `bernstein_scenarios` | List available scenarios |
| `bernstein_scenario` | Run a scenario |
| `bernstein_scenario_status` | Fetch scenario run status |
| `verify_chain` | Verify an artefact's lineage chain |
| `load_skill` | Load a skill pack at runtime |

These are **MCP tools**, not HTTP endpoints. They consume tool-call payloads matching each tool's MCP schema (see `mcp/server.py` for `inputSchema` definitions).

---

## Request/response examples

### Create a task

```bash
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Implement user authentication",
    "role": "backend",
    "priority": 2,
    "scope": ["src/auth/"],
    "complexity": "medium"
  }'
```

Response:

```json
{
  "id": "task-a1b2c3d4",
  "goal": "Implement user authentication",
  "role": "backend",
  "status": "open",
  "priority": 2,
  "created_at": 1712345678.0
}
```

### List open tasks

```bash
curl 'http://127.0.0.1:8052/tasks?status=open'
```

### Complete a task

```bash
curl -X POST http://127.0.0.1:8052/tasks/task-a1b2c3d4/complete \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "Added JWT auth with refresh tokens",
    "files_changed": ["src/auth/jwt.py", "tests/test_jwt.py"]
  }'
```

### Report progress

```bash
curl -X POST http://127.0.0.1:8052/tasks/task-a1b2c3d4/progress \
  -H "Content-Type: application/json" \
  -d '{
    "files_changed": 3,
    "tests_passing": true,
    "errors": []
  }'
```

### Register a worker node (cluster)

```bash
curl -X POST http://127.0.0.1:8052/cluster/nodes \
  -H "Authorization: Bearer ${BERNSTEIN_AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "node_id": "worker-eu-1",
    "address": "10.0.0.4:8052",
    "labels": {"region": "eu", "tier": "spot"}
  }'
```

---

## Error responses

All errors return JSON with a `detail` field:

```json
{ "detail": "Task not found: task-xyz" }
```

| Status | Meaning |
|--------|---------|
| 400 | Bad request (validation error) |
| 401 | Unauthorized (missing/invalid token) |
| 403 | Forbidden (IP not in allowlist; insufficient role) |
| 404 | Resource not found |
| 409 | Conflict (e.g., claim race) |
| 422 | Pydantic validation error |
| 429 | Rate limited (`Retry-After` header set) |
| 500 | Internal server error |
| 503 | Drain in progress; not accepting new work |

---

## Rendering full HTML docs

Use any OpenAPI renderer:

```bash
# Redoc
npx @redocly/cli build-docs docs/reference/openapi.json -o docs/api.html

# Swagger UI (Docker)
docker run -p 8080:8080 -e SWAGGER_JSON=/spec/openapi.json \
  -v "$(pwd)/docs/reference":/spec swaggerapi/swagger-ui
```

---

## Webhooks (outbound)

Bernstein can send webhook notifications for task lifecycle events. Configure in `bernstein.yaml`:

```yaml
webhooks:
  url: "https://your-app.example.com/bernstein-events"
  events:
    - task.created
    - task.completed
    - task.failed
    - agent.spawned
    - agent.completed
  secret: "your-hmac-secret"
```

Webhook payloads include an `X-Bernstein-Signature` header containing an HMAC-SHA256 signature of the request body, computed with the configured `secret`.
