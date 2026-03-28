# 523 — Docker / Kubernetes deployment for cluster mode

**Role:** devops
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** #519, #521

## Problem

No containerized deployment. Enterprise users with 100+ Claude subscriptions
need to run Bernstein at scale — on cloud, managed infra, not just local laptops.

## Design

### Dockerfile (multi-stage)
```dockerfile
# Stage 1: build
FROM python:3.12-slim as build
COPY . /app
RUN pip install hatchling && hatch build

# Stage 2: runtime
FROM python:3.12-slim
COPY --from=build /app/dist/*.whl /tmp/
RUN pip install /tmp/*.whl
ENTRYPOINT ["bernstein"]
```

### docker-compose.yaml
- `bernstein-server`: task server (port 8052)
- `bernstein-orchestrator`: orchestrator connecting to server
- `postgres`: shared state
- `redis`: distributed locks + bulletin board
- `bernstein-worker-{1..N}`: worker nodes (scalable)

### Kubernetes
- Helm chart in `deploy/helm/bernstein/`
- Server as Deployment + Service (ClusterIP)
- Workers as StatefulSet (each needs git worktree state)
- PostgreSQL via external chart (bitnami/postgresql)
- Redis via external chart (bitnami/redis)
- HPA for workers based on queue depth metric

### Config
- All config via env vars (12-factor)
- `BERNSTEIN_SERVER_URL`, `BERNSTEIN_AUTH_TOKEN`
- `BERNSTEIN_DATABASE_URL`, `BERNSTEIN_REDIS_URL`
- Secret management via K8s Secrets

## Files to create
- `Dockerfile`
- `docker-compose.yaml`
- `deploy/helm/bernstein/` — Helm chart
- `docs/DEPLOYMENT.md` — cloud deployment guide

## Completion signal
- `docker-compose up` starts a working Bernstein cluster
- Helm chart deploys to K8s with `helm install bernstein ./deploy/helm/bernstein`
