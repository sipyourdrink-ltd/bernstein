# Bernstein Helm Chart

This Helm chart deploys a complete Bernstein multi-agent orchestration system on Kubernetes.

## Chart Contents

The chart sets up:
- **Task Server** — FastAPI server managing task lifecycle and orchestration
- **Orchestrator** — Spawner/janitor responsible for task execution and cleanup
- **Workers** — Stateful agents running tasks (scalable via HPA)
- **PostgreSQL** — State database (optional, bitnami/postgresql sub-chart)
- **Redis** — Task queue and caching (optional, bitnami/redis sub-chart)
- **Monitoring** — ServiceMonitor and PrometheusRules for Prometheus integration (optional)
- **Ingress** — HTTP access to task server (optional)

## Prerequisites

- Kubernetes 1.20+
- Helm 3.0+
- (Optional) Prometheus Operator for monitoring
- (Optional) Ingress Controller (e.g., nginx-ingress)

## Quick Start

### 1. Add the Bernstein Helm repository (when available)

```bash
helm repo add bernstein https://charts.bernstein.dev
helm repo update
```

### 2. Install from local chart

```bash
cd deploy/helm
helm dependency update bernstein
helm install bernstein bernstein --namespace bernstein-system --create-namespace
```

### 3. Check installation

```bash
kubectl get pods -n bernstein-system
kubectl logs -n bernstein-system -l app=bernstein --tail=50 -f
```

### 4. Port-forward to access the server

```bash
kubectl port-forward -n bernstein-system svc/bernstein 8052:8052
curl http://localhost:8052/status
```

## Configuration

All configuration is in `values.yaml`. Key sections:

### Task Server (`server`)
- `replicaCount` — Number of server replicas
- `resources` — CPU/memory limits and requests
- `persistence` — PVC for `.sdd/` state directory
- `service.port` — Task server port (default: 8052)

### Workers (`worker`)
- `replicaCount` — Initial worker count (actual count controlled by HPA)
- `resources` — CPU/memory per worker
- `persistence` — PVC for git worktree state
- `autoscaling` — HPA configuration
  - `targetQueueDepth` — Tasks per replica before scaling (default: "2")
  - `targetCPUUtilizationPercentage` — CPU threshold for scaling

### Auth (`auth`)
- `existingSecret` — Reference an existing K8s Secret with auth token
- `secretKey` — Key name in the secret (default: "auth-token")

If `existingSecret` is empty, a random token is generated at install time.

### LLM Provider Keys (`providerKeys`)
- `existingSecret` — Reference a Secret containing:
  - `ANTHROPIC_API_KEY`
  - `OPENAI_API_KEY`
  - `GOOGLE_API_KEY`
  - `OPENROUTER_API_KEY`
  - `TAVILY_API_KEY`

### PostgreSQL (`postgresql`)
Sub-chart for database. Set `enabled: false` to use external database.

### Redis (`redis`)
Sub-chart for caching/queue. Set `enabled: false` to use external Redis.

### Ingress (`ingress`)
Enable HTTP/HTTPS access to the task server:

```yaml
ingress:
  enabled: true
  className: nginx
  hosts:
    - host: bernstein.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: bernstein-tls
      hosts:
        - bernstein.example.com
```

### Monitoring (`prometheus`)
Enable Prometheus metrics export:

```yaml
prometheus:
  enabled: true
```

This creates a `ServiceMonitor` for Prometheus Operator and `PrometheusRule` for alerting.

## Common Tasks

### Scale workers manually

```bash
kubectl scale statefulset bernstein-worker -n bernstein-system --replicas=5
```

### View task server logs

```bash
kubectl logs -n bernstein-system -l app=bernstein,component=server -f
```

### Create a task via API

```bash
AUTH_TOKEN=$(kubectl get secret -n bernstein-system bernstein-auth -o jsonpath='{.data.auth-token}' | base64 -d)

curl -X POST http://localhost:8052/tasks \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -d '{
    "goal": "Implement feature X",
    "model": "claude-opus-4.5",
    "effort": "medium"
  }'
```

### Check orchestrator status

```bash
kubectl logs -n bernstein-system -l app=bernstein,component=orchestrator -f
```

## Troubleshooting

### Task server won't start
- Check PVC: `kubectl get pvc -n bernstein-system`
- Check logs: `kubectl logs -n bernstein-system -l app=bernstein,component=server`
- Verify `.sdd/` directory permissions (must be writable)

### Workers not claiming tasks
- Check if workers are running: `kubectl get pods -n bernstein-system -l component=worker`
- Check worker logs: `kubectl logs -n bernstein-system -l component=worker -f`
- Verify Redis/PostgreSQL connectivity: `kubectl logs -n bernstein-system -l app=bernstein,component=server -f`

### High memory usage in workers
- Increase `worker.resources.limits.memory` in values.yaml
- Check for git worktree accumulation: `kubectl exec -it <worker-pod> -- ls /workspace/.git/worktrees/`

### HPA not scaling
- Verify Prometheus is connected and scraping metrics
- Check HPA status: `kubectl describe hpa bernstein-worker -n bernstein-system`
- Ensure `targetQueueDepth` is set appropriately (not empty string to disable)

## Uninstall

```bash
helm uninstall bernstein --namespace bernstein-system
kubectl delete namespace bernstein-system  # if desired
```

## Development

To modify the chart:

1. Update `values.yaml` with new defaults
2. Update template files in `templates/`
3. Test: `helm lint ./bernstein && helm template ./bernstein`
4. Package: `helm package ./bernstein`

## Support

For issues, questions, or contributions, visit the [Bernstein repository](https://github.com/anthropics/bernstein).
