# Deployment Guide

How to run Bernstein in different environments: local development, Docker, Kubernetes, bare metal, and CI/CD.

## Prerequisites

- Python 3.12+
- Git
- At least one supported CLI agent installed (Claude Code, Codex, Gemini CLI, etc.)

## Local development

```bash
# Install with uv
uv pip install -e .

# Or with pip
pip install -e .

# Run
bernstein run
```

The task server starts on `http://127.0.0.1:8052` by default. State is stored in `.sdd/` in the current working directory.

## Docker

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install bernstein
COPY . .
RUN pip install --no-cache-dir .

# Create state directory
RUN mkdir -p /data/.sdd

ENV BERNSTEIN_SDD_DIR=/data/.sdd
ENV BERNSTEIN_HOST=0.0.0.0
ENV BERNSTEIN_PORT=8052

EXPOSE 8052

VOLUME ["/data/.sdd"]

CMD ["bernstein", "run"]
```

### Docker Compose

```yaml
version: "3.8"

services:
  bernstein:
    build: .
    ports:
      - "8052:8052"
      - "9090:9090"  # Prometheus metrics
    volumes:
      - bernstein-state:/data/.sdd
      - ./workspace:/workspace
    environment:
      - BERNSTEIN_SDD_DIR=/data/.sdd
      - BERNSTEIN_HOST=0.0.0.0
      - BERNSTEIN_MAX_AGENTS=4
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8052/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  bernstein-state:
```

```bash
docker compose up -d
docker compose logs -f bernstein
```

## Kubernetes

### Helm chart values

Bernstein provides a Helm chart for Kubernetes deployment. See `docs/HELM_DEPLOYMENT.md` for detailed chart documentation.

### Basic manifests

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bernstein
  labels:
    app: bernstein
spec:
  replicas: 1
  selector:
    matchLabels:
      app: bernstein
  template:
    metadata:
      labels:
        app: bernstein
    spec:
      containers:
        - name: bernstein
          image: bernstein:latest
          ports:
            - containerPort: 8052
              name: http
            - containerPort: 9090
              name: metrics
          env:
            - name: BERNSTEIN_SDD_DIR
              value: /data/.sdd
            - name: BERNSTEIN_HOST
              value: "0.0.0.0"
          volumeMounts:
            - name: state
              mountPath: /data/.sdd
          livenessProbe:
            httpGet:
              path: /health/live
              port: http
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health/ready
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              memory: "512Mi"
              cpu: "250m"
            limits:
              memory: "4Gi"
              cpu: "2000m"
      volumes:
        - name: state
          persistentVolumeClaim:
            claimName: bernstein-state
---
# service.yaml
apiVersion: v1
kind: Service
metadata:
  name: bernstein
spec:
  selector:
    app: bernstein
  ports:
    - port: 8052
      targetPort: http
      name: http
    - port: 9090
      targetPort: metrics
      name: metrics
---
# pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: bernstein-state
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
```

```bash
kubectl apply -f deployment.yaml -f service.yaml -f pvc.yaml
kubectl get pods -l app=bernstein
```

### Cluster mode

For multi-node cluster deployment:

```yaml
# Add to deployment env
- name: BERNSTEIN_CLUSTER_ENABLED
  value: "true"
- name: BERNSTEIN_CLUSTER_NODE_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: BERNSTEIN_CLUSTER_CENTRAL_URL
  value: "http://bernstein-central:8052"
```

Scale worker nodes separately from the central coordinator.

## Bare metal

### systemd service

```ini
# /etc/systemd/system/bernstein.service
[Unit]
Description=Bernstein Orchestrator
After=network.target

[Service]
Type=simple
User=bernstein
Group=bernstein
WorkingDirectory=/opt/bernstein/workspace
ExecStart=/opt/bernstein/venv/bin/bernstein run
Restart=on-failure
RestartSec=10

Environment=BERNSTEIN_SDD_DIR=/var/lib/bernstein/.sdd
Environment=BERNSTEIN_HOST=0.0.0.0
Environment=BERNSTEIN_PORT=8052

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/bernstein /opt/bernstein/workspace

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable bernstein
sudo systemctl start bernstein
sudo journalctl -u bernstein -f
```

### Multiple instances

Run multiple orchestrators on different ports with separate state directories:

```bash
BERNSTEIN_SDD_DIR=/var/lib/bernstein/project-a BERNSTEIN_PORT=8052 bernstein run &
BERNSTEIN_SDD_DIR=/var/lib/bernstein/project-b BERNSTEIN_PORT=8053 bernstein run &
```

## CI/CD integration

### GitHub Actions

```yaml
name: Bernstein Task Run
on:
  workflow_dispatch:
    inputs:
      plan:
        description: "Plan file to execute"
        required: true

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install Bernstein
        run: pip install bernstein

      - name: Run plan
        run: bernstein run plans/${{ inputs.plan }}
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}

      - name: Upload results
        uses: actions/upload-artifact@v4
        with:
          name: bernstein-results
          path: .sdd/
```

### GitLab CI

```yaml
bernstein:
  image: python:3.12
  stage: build
  script:
    - pip install bernstein
    - bernstein run plans/ci-plan.yaml
  artifacts:
    paths:
      - .sdd/
    expire_in: 7 days
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BERNSTEIN_SDD_DIR` | `.sdd` | State directory path |
| `BERNSTEIN_HOST` | `127.0.0.1` | Server bind address |
| `BERNSTEIN_PORT` | `8052` | Server port |
| `BERNSTEIN_MAX_AGENTS` | `6` | Max concurrent agents |
| `BERNSTEIN_TICK_INTERVAL` | `5` | Orchestrator tick interval (seconds) |
| `BERNSTEIN_AUTH_ENABLED` | `false` | Enable authentication |
| `BERNSTEIN_CLUSTER_ENABLED` | `false` | Enable cluster mode |
| `BERNSTEIN_LOG_LEVEL` | `INFO` | Log verbosity |
| `BERNSTEIN_DASHBOARD_PASSWORD` | (none) | Dashboard auth password |

## Upgrading

1. Stop the running instance: `bernstein stop`
2. Back up state: `cp -r .sdd .sdd.backup`
3. Install new version: `pip install --upgrade bernstein`
4. Start: `bernstein run`

State is forward-compatible. Rollback by restoring the `.sdd.backup` directory.
