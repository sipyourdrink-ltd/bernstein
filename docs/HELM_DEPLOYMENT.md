# Helm Deployment Guide for Bernstein

Deploy Bernstein to Kubernetes using Helm with automatic worker scaling based on task queue depth.

## Prerequisites

- Kubernetes 1.24+ cluster with persistent storage (EBS, NFS, local-path provisioner, etc.)
- Helm 3.x
- `kubectl` configured for your cluster
- (Optional) Prometheus Operator for metrics and alerting

## Basic Installation

### 1. Add the Helm Repository (when published)

```bash
helm repo add bernstein https://charts.bernstein.dev
helm repo update
```

### 2. Install the Chart

```bash
# Basic installation with default values
helm install bernstein ./deploy/helm/bernstein \
  --namespace bernstein \
  --create-namespace

# Or with custom values
helm install bernstein ./deploy/helm/bernstein \
  --namespace bernstein \
  --create-namespace \
  -f my-values.yaml
```

## Configuration Options

### Worker Scaling

By default, workers scale based on **task queue depth**. Configure in `values.yaml`:

```yaml
worker:
  replicaCount: 2  # Initial replica count
  autoscaling:
    enabled: true
    minReplicas: 1
    maxReplicas: 20
    targetQueueDepth: "2"        # Scale up when 2+ tasks per replica
    targetCPUUtilizationPercentage: 70  # Fallback CPU-based scaling
```

### Persistent Storage

Both the task server and workers require persistent storage for `.sdd/` state:

```yaml
server:
  persistence:
    enabled: true
    storageClass: ""  # Leave empty for default, or specify "ebs", "local-path", etc.
    size: 10Gi

worker:
  persistence:
    enabled: true
    storageClass: ""
    size: 20Gi        # Larger for git worktrees
```

### Authentication

Generate an auth token at install time or provide an existing secret:

```bash
# Auto-generate token
helm install bernstein ./deploy/helm/bernstein

# Use existing secret
helm install bernstein ./deploy/helm/bernstein \
  --set auth.existingSecret=my-auth-secret \
  --set auth.secretKey=token
```

### LLM Provider Keys

Pass API keys for Claude, OpenAI, Google, etc.:

```bash
kubectl create secret generic bernstein-provider-keys \
  --from-literal=ANTHROPIC_API_KEY="sk-..." \
  --from-literal=OPENAI_API_KEY="sk-..." \
  -n bernstein

helm install bernstein ./deploy/helm/bernstein \
  -n bernstein \
  --set providerKeys.existingSecret=bernstein-provider-keys
```

## Advanced: Custom Metrics for HPA

To enable scaling based on **task queue depth**, you need Prometheus and Prometheus Adapter:

### 1. Install Prometheus Stack (if not present)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
```

### 2. Install Prometheus Adapter

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus-adapter prometheus-community/prometheus-adapter \
  --namespace monitoring \
  -f deploy/helm/prometheus-adapter-values.yaml
```

**Note:** Update `prometheus.url` in `prometheus-adapter-values.yaml` if your Prometheus instance is at a different address.

### 3. Enable ServiceMonitor and Queue-Depth Scaling

```bash
helm install bernstein ./deploy/helm/bernstein \
  --namespace bernstein \
  --create-namespace \
  --set prometheus.enabled=true \
  --set worker.autoscaling.targetQueueDepth="2"
```

### 4. Verify Metrics

```bash
# Port-forward to the server
kubectl port-forward -n bernstein svc/bernstein-server 8052:8052

# Check metrics endpoint
curl http://localhost:8052/metrics | grep bernstein_task_queue_depth

# Check HPA status
kubectl get hpa -n bernstein
kubectl describe hpa -n bernstein bernstein-worker
```

## Multi-Instance Setup (Service Mesh)

Bernstein automatically supports multi-instance deployments with:

- **StatefulSet for workers**: Each worker has a persistent git worktree state
- **Service for task server**: ClusterIP for internal communication
- **Distributed locks via Redis**: Coordination across instances
- **PostgreSQL for state**: Shared task storage and event log

No additional configuration needed for service mesh; the chart includes network policies and security contexts for Pod-to-Pod communication.

## Monitoring & Alerting

Enable Prometheus monitoring for dashboards and alerts:

```bash
helm install bernstein ./deploy/helm/bernstein \
  --set prometheus.enabled=true
```

This creates:
- **ServiceMonitor**: Configures Prometheus to scrape `/metrics`
- **PrometheusRule**: Alerting rules for high queue depth, failures, server downtime

Access Prometheus:
```bash
kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090
# Visit http://localhost:9090
```

## Troubleshooting

### Check pod status
```bash
kubectl get pods -n bernstein
kubectl logs -n bernstein deployment/bernstein-server
```

### Verify persistent volumes
```bash
kubectl get pvc -n bernstein
kubectl describe pvc -n bernstein bernstein-server-sdd
```

### Check HPA scaling decisions
```bash
kubectl describe hpa -n bernstein bernstein-worker
```

### Inspect metrics
```bash
kubectl port-forward -n bernstein svc/bernstein-server 8052:8052
curl -s http://localhost:8052/metrics | grep bernstein_task_queue_depth
```

## Cleanup

```bash
helm uninstall bernstein -n bernstein
kubectl delete namespace bernstein
```

## Next Steps

- Check [DESIGN.md](../docs/DESIGN.md) for architecture details
- Review [README.md](../README.md) for usage patterns
- Enable observability: [Prometheus setup guide](#advanced-custom-metrics-for-hpa)
