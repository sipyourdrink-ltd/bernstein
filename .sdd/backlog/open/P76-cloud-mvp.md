# P76 — Bernstein Cloud MVP

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Users who want to run bernstein workflows without local setup or from CI environments have no hosted option, limiting adoption and making team collaboration harder.

## Solution
- Build a FastAPI web app that exposes a `/run` endpoint accepting `bernstein.yaml` workflows via POST
- Authenticate requests with API key passed in `Authorization` header
- Execute workflows using server-side bernstein runtime in a sandboxed subprocess
- Return results asynchronously via webhook URL provided in the request payload
- Provide a `/status/{run_id}` polling endpoint as fallback
- Package as a Docker container with `Dockerfile` and `docker-compose.yml`
- Include health check endpoint at `/health`

## Acceptance
- [ ] FastAPI app accepts workflow YAML via `POST /run` with API key auth
- [ ] Workflows execute server-side using bernstein runtime
- [ ] Results delivered to caller-specified webhook URL on completion
- [ ] `/status/{run_id}` returns current run state (queued, running, completed, failed)
- [ ] `/health` endpoint returns 200 when service is ready
- [ ] Docker container builds and runs with `docker compose up`
- [ ] Invalid API keys return 401; missing workflow returns 422
