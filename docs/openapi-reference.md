# OpenAPI Reference

Bernstein exposes a task-server HTTP API on `http://127.0.0.1:8052` by default. The full OpenAPI 3.1 specification is available at `/openapi.json` when the server is running.

## Generating the spec

Use the included script to regenerate `docs/openapi.json` from the FastAPI app
definition without starting the server:

```bash
uv run python scripts/generate_openapi.py
# Written docs/openapi.json  (216 paths, 87 schemas)
```

Run this after adding or modifying any API route, Pydantic model, or response
schema, then commit the updated `docs/openapi.json`. The hosted
`docs/api-reference.html` page (Redoc) reads the spec at load time, so the
reference updates automatically once the JSON is committed.

**Alternative — fetch from a running server:**

```bash
bernstein run &
curl -s http://127.0.0.1:8052/openapi.json > docs/openapi.json
```

## Core endpoints

### Tasks

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tasks` | Create a new task |
| `GET` | `/tasks` | List tasks (filter by `?status=open\|done\|failed`) |
| `GET` | `/tasks/{id}` | Get task by ID |
| `POST` | `/tasks/{id}/complete` | Mark task completed |
| `POST` | `/tasks/{id}/fail` | Mark task failed |
| `POST` | `/tasks/{id}/progress` | Report progress (files_changed, tests_passing, errors) |

### Bulletin board

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/bulletin` | Post a cross-agent finding or blocker |
| `GET` | `/bulletin` | Read bulletins (filter by `?since={timestamp}`) |

### Status and health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status` | Dashboard summary (agents, tasks, metrics) |
| `GET` | `/health` | Health check (returns 200 when server is up) |
| `GET` | `/health/ready` | Readiness probe |
| `GET` | `/health/live` | Liveness probe |

### Cluster (when enabled)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/cluster/register` | Register a worker node |
| `POST` | `/cluster/heartbeat` | Send node heartbeat |
| `GET` | `/cluster/topology` | Get cluster topology |
| `GET` | `/cluster/nodes` | List registered nodes |

### Authentication

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/token` | Exchange credentials for a JWT |
| `POST` | `/auth/refresh` | Refresh an expired token |
| `GET` | `/auth/callback` | OIDC callback endpoint |

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
curl http://127.0.0.1:8052/tasks?status=open
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

## Authentication

When auth is enabled (`BERNSTEIN_AUTH_ENABLED=true`), all non-public endpoints require a Bearer token:

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8052/tasks
```

Public endpoints (no auth required): `/health`, `/health/ready`, `/health/live`, `/.well-known/agent.json`, `/docs`, `/openapi.json`.

## Error responses

All errors return JSON with a `detail` field:

```json
{
  "detail": "Task not found: task-xyz"
}
```

| Status | Meaning |
|--------|---------|
| 400 | Bad request (validation error) |
| 401 | Unauthorized (missing/invalid token) |
| 403 | Forbidden (IP not in allowlist) |
| 404 | Resource not found |
| 429 | Rate limited (retry after header set) |
| 500 | Internal server error |

## Generating HTML docs

Use any OpenAPI renderer:

```bash
# Redoc
npx @redocly/cli build-docs openapi.json -o docs/api.html

# Swagger UI
docker run -p 8080:8080 -e SWAGGER_JSON=/spec/openapi.json \
  -v $(pwd):/spec swaggerapi/swagger-ui
```

## Webhooks

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

Webhook payloads include an `X-Bernstein-Signature` header with an HMAC-SHA256 signature of the body.
