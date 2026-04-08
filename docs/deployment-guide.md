# Deployment Guide

How to run Bernstein in different environments. Each section is self-contained with complete configuration examples.

**Jump to:**
- [Local development](#local-development)
- [CI/CD — GitHub Actions](#cicd--github-actions)
- [CI/CD — GitLab CI](#cicd--gitlab-ci)
- [Docker single-host](#docker-single-host)
- [Docker Compose cluster](#docker-compose-cluster)
- [Kubernetes / Helm](#kubernetes--helm)
- [Team shared server (bare metal)](#team-shared-server)
- [Environment variable reference](#environment-variables)
- [Upgrading](#upgrading)

---

## Local development

### Prerequisites

- Python 3.12+
- Git
- `uv` (recommended) or pip
- At least one supported CLI agent installed (Claude Code, Codex, Gemini CLI, etc.)
- An API key for at least one LLM provider

### Install

```bash
# With uv (recommended — faster, handles virtualenvs automatically)
uv pip install bernstein

# Or install from source
git clone https://github.com/bernstein-ai/bernstein
cd bernstein
uv pip install -e .

# Or with pip
pip install bernstein
```

### First run

```bash
# Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."   # Claude
# export OPENAI_API_KEY="sk-..."        # GPT / Codex
# export GOOGLE_API_KEY="..."           # Gemini

# Initialize a project
cd /path/to/your/project
bernstein init

# Run a plan
bernstein run plans/my-project.yaml

# Or run interactively (type a goal, Bernstein decomposes it)
bernstein run
```

The task server starts on `http://127.0.0.1:8052`. State is stored in `.sdd/` in the working directory. Add `.sdd/` to `.gitignore`.

### Local configuration file

Create `bernstein.yaml` in your project root:

```yaml
# bernstein.yaml
cli: auto               # auto-detect installed agent (claude|codex|gemini|qwen)
model: sonnet           # default model
max_agents: 4           # concurrent agents; tune based on your API tier
budget: 5.00            # hard spending cap in USD (optional)

# Override model per complexity level
model_policy:
  simple: haiku
  medium: sonnet
  complex: opus

# Share context files with all agents
context_files:
  - README.md
  - docs/ARCHITECTURE.md
```

### Verify the install

```bash
bernstein doctor        # checks dependencies, API keys, git setup
bernstein status        # shows task server state
```

---

## CI/CD — GitHub Actions

### Single-shot plan execution

Run a plan file on every push or on demand:

```yaml
# .github/workflows/bernstein.yml
name: Bernstein Agent Run
on:
  workflow_dispatch:
    inputs:
      plan:
        description: "Plan file (relative to repo root)"
        required: true
        default: "plans/ci-tasks.yaml"
      max_agents:
        description: "Max concurrent agents"
        required: false
        default: "2"

jobs:
  bernstein:
    runs-on: ubuntu-latest
    timeout-minutes: 60

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0        # full history needed for worktree operations

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"

      - name: Install Bernstein
        run: pip install bernstein

      - name: Install Claude Code (or your preferred CLI agent)
        run: npm install -g @anthropic-ai/claude-code
        # Alternatively:
        # run: pip install openai-codex

      - name: Run plan
        run: |
          bernstein run ${{ inputs.plan }} \
            --max-agents ${{ inputs.max_agents }} \
            --budget 10.00
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          # OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          BERNSTEIN_LOG_JSON: "true"
          BERNSTEIN_NO_TUI: "true"    # disable interactive TUI in CI

      - name: Upload state artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: bernstein-state-${{ github.run_id }}
          path: .sdd/
          retention-days: 7

      - name: Post summary
        if: always()
        run: bernstein report --format markdown >> $GITHUB_STEP_SUMMARY
```

### Using the official GitHub Action

If the Bernstein GitHub Action is available in the marketplace:

```yaml
# .github/workflows/bernstein-action.yml
name: Bernstein (Action)
on:
  push:
    branches: [main]

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: bernstein-ai/bernstein-action@v1
        with:
          plan: plans/ci-tasks.yaml
          max-agents: 2
          budget: 5.00
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

See `docs/github-action.md` for the full parameter reference.

### Storing secrets

```bash
# Add secrets to your repository
gh secret set ANTHROPIC_API_KEY --body "sk-ant-..."
gh secret set OPENAI_API_KEY --body "sk-..."
```

Never commit API keys to your repository. Use GitHub Secrets for all provider credentials.

---

## CI/CD — GitLab CI

### Basic pipeline stage

```yaml
# .gitlab-ci.yml
bernstein:
  image: python:3.12-slim
  stage: build
  timeout: 60 minutes

  before_script:
    - apt-get update -q && apt-get install -y -q git npm
    - pip install bernstein
    - npm install -g @anthropic-ai/claude-code

  script:
    - bernstein run plans/ci-tasks.yaml --max-agents 2 --budget 10.00

  variables:
    ANTHROPIC_API_KEY: $ANTHROPIC_API_KEY   # set in GitLab CI/CD settings
    BERNSTEIN_LOG_JSON: "true"
    BERNSTEIN_NO_TUI: "true"    # disable interactive TUI in CI

  artifacts:
    paths:
      - .sdd/
    expire_in: 7 days
    when: always
```

### Caching pip packages between runs

```yaml
bernstein:
  image: python:3.12-slim
  stage: build

  cache:
    key: bernstein-pip-$CI_COMMIT_REF_SLUG
    paths:
      - .pip-cache/

  before_script:
    - apt-get update -q && apt-get install -y -q git npm
    - pip install --cache-dir .pip-cache bernstein
    - npm install -g @anthropic-ai/claude-code

  script:
    - bernstein run plans/ci-tasks.yaml --max-agents 2

  variables:
    ANTHROPIC_API_KEY: $ANTHROPIC_API_KEY
    BERNSTEIN_NO_TUI: "true"
    PIP_CACHE_DIR: "$CI_PROJECT_DIR/.pip-cache"
```

### Multi-project pipeline (scheduled)

```yaml
# .gitlab-ci.yml — runs nightly
weekly-refactor:
  image: python:3.12-slim
  stage: maintenance
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
  script:
    - pip install bernstein
    - bernstein run plans/weekly-maintenance.yaml --max-agents 3
  variables:
    ANTHROPIC_API_KEY: $ANTHROPIC_API_KEY
```

### Protected variables (GitLab secrets)

1. Go to **Settings → CI/CD → Variables**.
2. Add `ANTHROPIC_API_KEY` with **Protected** and **Masked** flags enabled.
3. The variable is available in protected branches and tags only.

---

## Docker single-host

### .env file

Copy the example and fill in your keys:

```bash
# .env  (never commit this file)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...
BERNSTEIN_AUTH_TOKEN=change-me-in-production
BERNSTEIN_MAX_AGENTS=4
BERNSTEIN_DASHBOARD_PASSWORD=change-me
```

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /workspace

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install a CLI agent
RUN npm install -g @anthropic-ai/claude-code

# Install Bernstein
RUN pip install --no-cache-dir bernstein

# Non-root user for security
RUN useradd -m -u 1000 bernstein && chown -R bernstein /workspace
USER bernstein

# State directory
VOLUME ["/workspace/.sdd"]

ENV BERNSTEIN_BIND_HOST=0.0.0.0
ENV BERNSTEIN_PORT=8052
ENV BERNSTEIN_NO_TUI=true

EXPOSE 8052

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:8052/health || exit 1

CMD ["bernstein", "conduct"]
```

```bash
docker build -t bernstein:latest .
docker run -d \
  --name bernstein \
  --env-file .env \
  -p 8052:8052 \
  -v bernstein-state:/workspace/.sdd \
  -v $(pwd):/workspace/project \
  bernstein:latest
```

---

## Docker Compose cluster

The included `docker-compose.yaml` runs a full cluster: task server, orchestrator, scalable workers, PostgreSQL, Redis, Prometheus, and Grafana.

### Setup

```bash
# Copy and edit the env file
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY and BERNSTEIN_AUTH_TOKEN

# Start the full stack
docker compose up -d

# Scale workers (each worker claims tasks from the shared server)
docker compose up -d --scale bernstein-worker=4

# View logs
docker compose logs -f bernstein-server
docker compose logs -f bernstein-orchestrator
```

### Service endpoints

| Service | URL | Purpose |
|---|---|---|
| Task server + dashboard | `http://localhost:8052/dashboard` | Web UI, task management |
| Task server API | `http://localhost:8052` | REST API |
| Prometheus | `http://localhost:9090` | Metrics |
| Grafana | `http://localhost:3000` | Agent dashboards (admin/admin) |

### Stopping and cleanup

```bash
docker compose down          # stop containers, keep volumes
docker compose down -v       # stop and delete all data volumes
```

---

## Kubernetes / Helm

### Prerequisites

- Kubernetes 1.24+
- Helm 3.x
- `kubectl` configured for your cluster
- Persistent storage (EBS, NFS, local-path provisioner, etc.)

### Install with Helm

```bash
# From the local chart
helm install bernstein ./deploy/helm/bernstein \
  --namespace bernstein \
  --create-namespace \
  -f my-values.yaml

# Or add the Helm repo (when published)
helm repo add bernstein https://charts.bernstein.dev
helm repo update
helm install bernstein bernstein/bernstein \
  --namespace bernstein \
  --create-namespace
```

### Provider API keys (Kubernetes secret)

```bash
kubectl create secret generic bernstein-provider-keys \
  --namespace bernstein \
  --from-literal=ANTHROPIC_API_KEY="sk-ant-..." \
  --from-literal=OPENAI_API_KEY="sk-..."
```

### `values.yaml` — complete example

```yaml
# my-values.yaml
image:
  repository: bernstein
  tag: latest
  pullPolicy: IfNotPresent

server:
  replicaCount: 1
  resources:
    requests:
      memory: "512Mi"
      cpu: "250m"
    limits:
      memory: "4Gi"
      cpu: "2000m"
  persistence:
    enabled: true
    storageClass: ""     # use cluster default
    size: 10Gi
  service:
    type: ClusterIP
    port: 8052

worker:
  replicaCount: 2
  resources:
    requests:
      memory: "1Gi"
      cpu: "500m"
    limits:
      memory: "8Gi"
      cpu: "4000m"
  persistence:
    enabled: true
    storageClass: ""
    size: 20Gi           # larger: holds git worktrees
  autoscaling:
    enabled: true
    minReplicas: 1
    maxReplicas: 20
    targetQueueDepth: "2"           # scale up when 2+ tasks per worker
    targetCPUUtilizationPercentage: 70

providerKeys:
  existingSecret: bernstein-provider-keys

auth:
  enabled: true
  # existingSecret: my-auth-secret   # use an existing secret instead

config:
  maxAgents: 6
  logLevel: INFO
  clusterEnabled: true

monitoring:
  prometheus:
    enabled: true
  grafana:
    enabled: true
    adminPassword: "change-me"
```

```bash
helm install bernstein ./deploy/helm/bernstein \
  --namespace bernstein \
  --create-namespace \
  -f my-values.yaml

# Verify
kubectl get pods -n bernstein
kubectl port-forward -n bernstein svc/bernstein-server 8052:8052
```

For the full Helm chart parameter reference, see `docs/HELM_DEPLOYMENT.md`.

### Raw Kubernetes manifests (without Helm)

```yaml
# bernstein.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bernstein
  namespace: bernstein
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
            - name: BERNSTEIN_BIND_HOST
              value: "0.0.0.0"
            - name: BERNSTEIN_CLUSTER_ENABLED
              value: "true"
            - name: ANTHROPIC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: bernstein-provider-keys
                  key: ANTHROPIC_API_KEY
          volumeMounts:
            - name: state
              mountPath: /workspace/.sdd
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
apiVersion: v1
kind: Service
metadata:
  name: bernstein
  namespace: bernstein
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
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: bernstein-state
  namespace: bernstein
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
```

```bash
kubectl apply -f bernstein.yaml
kubectl get pods -n bernstein
```

---

## Team shared server

Running Bernstein on a dedicated server that multiple developers share. Each developer points their local tools at the shared task server.

### Server setup (systemd)

```bash
# Create a system user
sudo useradd -r -s /bin/bash -d /opt/bernstein bernstein
sudo mkdir -p /opt/bernstein/workspace /var/lib/bernstein/.sdd
sudo chown -R bernstein:bernstein /opt/bernstein /var/lib/bernstein

# Install into a virtualenv
sudo -u bernstein python3.12 -m venv /opt/bernstein/venv
sudo -u bernstein /opt/bernstein/venv/bin/pip install bernstein

# Install a CLI agent (system-wide or in the venv)
npm install -g @anthropic-ai/claude-code
```

```ini
# /etc/systemd/system/bernstein.service
[Unit]
Description=Bernstein Orchestrator
After=network.target
Wants=network.target

[Service]
Type=simple
User=bernstein
Group=bernstein
WorkingDirectory=/opt/bernstein/workspace

ExecStart=/opt/bernstein/venv/bin/bernstein conduct
ExecStop=/opt/bernstein/venv/bin/bernstein stop --hard

Restart=on-failure
RestartSec=10

# Secrets — set via EnvironmentFile in production
EnvironmentFile=/etc/bernstein/env
Environment=BERNSTEIN_SDD_DIR=/var/lib/bernstein/.sdd
Environment=BERNSTEIN_BIND_HOST=0.0.0.0
Environment=BERNSTEIN_PORT=8052
Environment=BERNSTEIN_LOG_JSON=true
Environment=BERNSTEIN_MAX_AGENTS=8

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true
ReadWritePaths=/var/lib/bernstein /opt/bernstein/workspace

[Install]
WantedBy=multi-user.target
```

```bash
# /etc/bernstein/env  (mode 0600, owned by bernstein)
ANTHROPIC_API_KEY=sk-ant-...
BERNSTEIN_AUTH_TOKEN=strong-random-secret
BERNSTEIN_DASHBOARD_PASSWORD=another-strong-password
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable bernstein
sudo systemctl start bernstein
sudo journalctl -u bernstein -f
```

### Connecting as a team member

Each developer configures their local `bernstein.yaml` to point at the shared server:

```yaml
# bernstein.yaml (developer's local project)
server_url: http://bernstein.internal:8052
# or via env: BERNSTEIN_SERVER_URL=http://bernstein.internal:8052
```

```bash
# Submit tasks to the shared server without running a local orchestrator
bernstein task add "Implement login page" --role frontend --priority 2
bernstein status   # see what the shared server is running
bernstein ps       # list active agents
```

### Reverse proxy (nginx)

Expose the dashboard behind TLS:

```nginx
# /etc/nginx/sites-enabled/bernstein
server {
    listen 443 ssl;
    server_name bernstein.internal;

    ssl_certificate     /etc/ssl/certs/bernstein.crt;
    ssl_certificate_key /etc/ssl/private/bernstein.key;

    # Dashboard
    location / {
        proxy_pass http://127.0.0.1:8052;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        # Required for SSE (live dashboard streaming)
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
    }
}
```

### Multi-project setup

Run separate orchestrators on different ports for project isolation:

```bash
# /etc/systemd/system/bernstein@.service  (template unit)
[Unit]
Description=Bernstein Orchestrator — %i
After=network.target

[Service]
Type=simple
User=bernstein
WorkingDirectory=/opt/bernstein/projects/%i
EnvironmentFile=/etc/bernstein/%i.env
ExecStart=/opt/bernstein/venv/bin/bernstein conduct

[Install]
WantedBy=multi-user.target
```

```bash
# Start project-a on port 8052 and project-b on port 8053
sudo systemctl start bernstein@project-a
sudo systemctl start bernstein@project-b
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude API key |
| `OPENAI_API_KEY` | — | OpenAI / Codex API key |
| `GOOGLE_API_KEY` | — | Gemini API key |
| `BERNSTEIN_SERVER_URL` | `http://127.0.0.1:8052` | Task server URL (for remote workers) |
| `BERNSTEIN_BIND_HOST` | `127.0.0.1` | Server bind address |
| `BERNSTEIN_PORT` | `8052` | Server port |
| `BERNSTEIN_MAX_AGENTS` | `6` | Max concurrent agents |
| `BERNSTEIN_AUTH_TOKEN` | — | Inter-node auth secret (cluster mode) |
| `BERNSTEIN_DASHBOARD_PASSWORD` | — | Dashboard HTTP auth password |
| `BERNSTEIN_STORAGE_BACKEND` | `memory` | `memory`, `postgres`, or `redis` |
| `BERNSTEIN_DATABASE_URL` | — | PostgreSQL DSN (e.g. `postgresql://user:pass@host/db`) |
| `BERNSTEIN_REDIS_URL` | — | Redis URL (e.g. `redis://localhost:6379/0`) |
| `BERNSTEIN_CLUSTER_ENABLED` | `false` | Enable multi-node cluster mode |
| `BERNSTEIN_LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |
| `BERNSTEIN_LOG_JSON` | `false` | Emit JSON log lines (for log aggregators) |
| `BERNSTEIN_BUDGET` | — | Hard spending cap in USD |
| `BERNSTEIN_TICK_INTERVAL` | `5` | Orchestrator tick interval in seconds |
| `BERNSTEIN_SKIP_GATES` | — | Skip quality gates (requires `BERNSTEIN_SKIP_GATE_REASON`) |
| `BERNSTEIN_NO_TUI` | — | Disable interactive TUI (useful in CI) |
| `BERNSTEIN_QUIET` | — | Suppress all non-error output |

---

## Upgrading

1. Stop the running instance: `bernstein stop`
2. Back up state: `cp -r .sdd .sdd.backup-$(date +%Y%m%d)`
3. Install the new version: `pip install --upgrade bernstein`
4. Start: `bernstein run`

State format is forward-compatible between minor versions. For major version upgrades, check `docs/migration-guides.md` for breaking changes.

To roll back: `pip install bernstein==<previous-version>` and restore `.sdd.backup/`.
