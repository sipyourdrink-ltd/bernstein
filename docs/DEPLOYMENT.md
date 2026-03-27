# Bernstein — Deployment Guide

This guide covers deploying Bernstein in cluster mode: Docker Compose for local/dev clusters and Kubernetes (via Helm) for production.

---

## Prerequisites

- Docker 24+ with Compose v2
- (For K8s) kubectl + Helm 3.12+
- At least one LLM provider API key (e.g. `ANTHROPIC_API_KEY`)

---

## Docker Compose

### Quick start

```bash
# 1. Copy and fill in your API keys
cp .env.example .env
$EDITOR .env

# 2. Build the image and start the cluster
docker compose up --build -d

# 3. Check status
curl http://localhost:8052/health
docker compose ps
```

### Scale workers

```bash
# Run 4 parallel workers
docker compose up --scale bernstein-worker=4 -d
```

### Services

| Service | Description | Port |
|---|---|---|
| `bernstein-server` | Task server — shared state coordinator | 8052 |
| `bernstein-orchestrator` | Reads backlog, decomposes goals into tasks | — |
| `bernstein-worker` | Claims and executes tasks via CLI agents | — |
| `postgres` | Persistent task store (set `BERNSTEIN_STORAGE_BACKEND=postgres`) | 5432 |
| `redis` | Distributed locks for multi-node task claiming | 6379 |

### Environment variables

Create `.env` from the table below:

| Variable | Required | Description |
|---|---|---|
| `BERNSTEIN_AUTH_TOKEN` | Yes | Shared secret for inter-node auth (pick any random string) |
| `BERNSTEIN_STORAGE_BACKEND` | No | `memory` (default), `postgres`, or `redis` |
| `BERNSTEIN_DATABASE_URL` | If postgres/redis | PostgreSQL DSN (e.g. `postgresql://user:pass@postgres:5432/bernstein`) |
| `BERNSTEIN_REDIS_URL` | If redis | Redis URL (e.g. `redis://redis:6379/0`) |
| `ANTHROPIC_API_KEY` | If using Claude | Claude API key |
| `OPENAI_API_KEY` | If using Codex | OpenAI API key |
| `GOOGLE_API_KEY` | If using Gemini | Google AI API key |
| `OPENROUTER_API_KEY` | Optional | OpenRouter aggregator key |
| `TAVILY_API_KEY` | Optional | Web search tool key |

### Persistent state

`.sdd/` is mounted as a named volume (`sdd-data`). To back it up:

```bash
docker run --rm -v bernstein_sdd-data:/data -v $(pwd):/backup \
  alpine tar czf /backup/sdd-backup.tar.gz /data
```

---

## Kubernetes (Helm)

### Add Bitnami repo (required for PostgreSQL + Redis sub-charts)

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
```

### Create provider keys secret

```bash
kubectl create secret generic bernstein-provider-keys \
  --from-literal=ANTHROPIC_API_KEY="sk-ant-..." \
  --from-literal=OPENAI_API_KEY="sk-..." \
  --from-literal=GOOGLE_API_KEY="AIza..."
```

### Install

```bash
helm dependency update ./deploy/helm/bernstein

helm install bernstein ./deploy/helm/bernstein \
  --namespace bernstein \
  --create-namespace \
  --set providerKeys.existingSecret=bernstein-provider-keys
```

### Upgrade

```bash
helm upgrade bernstein ./deploy/helm/bernstein \
  --namespace bernstein \
  --set providerKeys.existingSecret=bernstein-provider-keys
```

### Uninstall

```bash
helm uninstall bernstein --namespace bernstein
```

### Common overrides

**Scale workers:**
```bash
helm upgrade bernstein ./deploy/helm/bernstein \
  --namespace bernstein \
  --set worker.replicaCount=8
```

**Disable HPA (fixed worker count):**
```bash
--set worker.autoscaling.enabled=false
```

**Expose the task server via ingress:**
```bash
--set ingress.enabled=true \
--set ingress.className=nginx \
--set "ingress.hosts[0].host=bernstein.example.com" \
--set "ingress.hosts[0].paths[0].path=/" \
--set "ingress.hosts[0].paths[0].pathType=Prefix"
```

**Use external PostgreSQL/Redis (e.g. managed cloud services):**
```bash
--set postgresql.enabled=false \
--set redis.enabled=false \
--set externalDatabase.url="postgresql://user:pass@host:5432/bernstein" \
--set externalRedis.url="redis://host:6379/0"
```

### Architecture

```
                          ┌─────────────────┐
                          │  Ingress (opt.)  │
                          └────────┬────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │      bernstein-server        │
                    │   Deployment + Service       │
                    │   (ClusterIP :8052)          │
                    └──┬──────────────────────┬───┘
                       │                      │
         ┌─────────────▼─────────┐   ┌────────▼────────────┐
         │  bernstein-orchestrat │   │  bernstein-worker    │
         │  Deployment (1 pod)   │   │  StatefulSet (N pods)│
         │  conduct --remote     │   │  conduct --worker    │
         └───────────────────────┘   └─────────────────────┘
                       │                      │
              ┌────────▼────────┐   ┌─────────▼──────────┐
              │   PostgreSQL    │   │       Redis          │
              │  (bitnami chart)│   │  (bitnami chart)    │
              └─────────────────┘   └────────────────────┘
```

### Resource sizing guide

| Role | Replicas | CPU req | Mem req | Notes |
|---|---|---|---|---|
| server | 1 | 100m | 256Mi | Stateful — single replica |
| orchestrator | 1 | 100m | 128Mi | Reads backlog, no heavy compute |
| worker | 2–20 | 500m | 512Mi | Scale based on task throughput |

Workers make outbound calls to LLM APIs and run `claude`/`codex`/`gemini` CLI binaries. They do **not** need GPUs.

### Secrets management

Never put API keys in `values.yaml`. Use one of:

- **Kubernetes Secrets** (`kubectl create secret`) — simplest
- **External Secrets Operator** — sync from AWS Secrets Manager, Vault, GCP Secret Manager
- **Sealed Secrets** — encrypted secrets committed to git

### Health checks

```bash
# Task server health
kubectl exec -n bernstein deploy/bernstein-server -- \
  curl -s http://localhost:8052/health

# Live task queue
kubectl exec -n bernstein deploy/bernstein-server -- \
  curl -s http://localhost:8052/status
```

---

## TLS termination via reverse proxy

Bernstein's task server speaks plain HTTP. For remote/cluster access over the internet,
terminate TLS at a reverse proxy. Two options are shown below.

### Nginx

```nginx
# /etc/nginx/sites-available/bernstein
server {
    listen 80;
    server_name bernstein.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name bernstein.example.com;

    ssl_certificate     /etc/letsencrypt/live/bernstein.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bernstein.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location / {
        proxy_pass         http://127.0.0.1:8052;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Enable and restart:
```bash
ln -s /etc/nginx/sites-available/bernstein /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d bernstein.example.com  # get cert via Let's Encrypt
```

### Caddy (automatic HTTPS)

```caddyfile
# /etc/caddy/Caddyfile
bernstein.example.com {
    reverse_proxy 127.0.0.1:8052
}
```

Caddy automatically obtains and renews a Let's Encrypt certificate. Start:
```bash
systemctl start caddy
```

### Worker nodes connecting over TLS

Once TLS is in place, workers use `https://` in `BERNSTEIN_SERVER_URL`:

```bash
# Central server (bind on all interfaces, auth required)
BERNSTEIN_BIND_HOST=0.0.0.0 BERNSTEIN_AUTH_TOKEN=<secret> bernstein run

# Worker on another machine
BERNSTEIN_SERVER_URL=https://bernstein.example.com \
  BERNSTEIN_AUTH_TOKEN=<secret> \
  bernstein run
```

The bearer token in `BERNSTEIN_AUTH_TOKEN` is validated on every request; always
pair it with TLS so the token is not transmitted in the clear.

---

## CI/CD integration

To build and push the image in CI:

```bash
docker build -t your-registry/bernstein:$GIT_SHA .
docker push your-registry/bernstein:$GIT_SHA

helm upgrade bernstein ./deploy/helm/bernstein \
  --set image.repository=your-registry/bernstein \
  --set image.tag=$GIT_SHA
```

---

## Troubleshooting

**Server health check fails on startup**
The server waits for PostgreSQL to be ready. Check postgres logs:
```bash
docker compose logs postgres
# or
kubectl logs -n bernstein -l app.kubernetes.io/component=postgresql
```

**Workers not claiming tasks**
Verify `BERNSTEIN_AUTH_TOKEN` matches across all nodes:
```bash
docker compose exec bernstein-worker env | grep AUTH
```

**Task server unreachable from workers**
In K8s, check the Service is up:
```bash
kubectl get svc -n bernstein
kubectl describe svc bernstein-server -n bernstein
```
