# Interactive Quickstart Tutorial

Get Bernstein running and orchestrating agents in under 10 minutes.

## Step 1: Install

```bash
# Using uv (recommended)
uv pip install bernstein

# Or pip
pip install bernstein

# Verify installation
bernstein --version
```

## Step 2: Check your environment

Bernstein needs at least one CLI coding agent installed. Check which ones are available:

```bash
bernstein agents
```

Example output:

```
Available agents:
  claude    ✓  Claude Code (claude)
  codex     ✓  Codex CLI (codex)
  gemini    ✗  Not found (install: npm i -g @google/gemini-cli)
```

You need at least one agent with a checkmark. If you have Claude Code:

```bash
# Verify Claude Code works
claude --version
```

## Step 3: Set up API keys

The CLI agents need their own API keys. Bernstein does not handle provider auth directly -- each agent uses its own credentials.

```bash
# For Claude Code
export ANTHROPIC_API_KEY="sk-ant-..."

# For Codex
export OPENAI_API_KEY="sk-..."
```

## Step 4: Run your first orchestration

Navigate to any git repository and run:

```bash
cd your-project
bernstein run
```

Bernstein will:
1. Start the task server on port 8052
2. Analyze the codebase
3. Plan improvements
4. Spawn agents to execute tasks
5. Merge results back to your branch

Watch the progress:

```bash
# In another terminal
bernstein status
```

## Step 5: Run a plan file

For deterministic execution, use a plan file. Create `plans/hello.yaml`:

```yaml
name: "Hello Bernstein"
description: "A simple test plan"

stages:
  - name: greeting
    steps:
      - goal: "Create a file called hello.txt with the text 'Hello from Bernstein'"
        role: backend
        priority: 1
        scope: ["hello.txt"]
        complexity: simple
```

Run it:

```bash
bernstein run plans/hello.yaml
```

## Step 6: Multi-stage plan

Plans can have stages with dependencies. Create `plans/feature.yaml`:

```yaml
name: "Add user greeting feature"
description: "Backend + test in two stages"

stages:
  - name: implementation
    steps:
      - goal: "Create src/greeting.py with a greet(name: str) -> str function that returns 'Hello, {name}!'"
        role: backend
        priority: 1
        scope: ["src/greeting.py"]
        complexity: simple

  - name: testing
    depends_on: [implementation]
    steps:
      - goal: "Write tests for the greet function in tests/test_greeting.py"
        role: qa
        priority: 2
        scope: ["tests/test_greeting.py"]
        complexity: simple
```

Run it:

```bash
bernstein run plans/feature.yaml
```

The `testing` stage waits for `implementation` to finish before starting.

## Step 7: Monitor via the dashboard

Open the web dashboard:

```
http://127.0.0.1:8052/dashboard
```

The dashboard shows:
- Active agents and their current tasks
- Task queue (open, in progress, completed, failed)
- Token usage and cost estimate
- Recent activity timeline

## Step 8: Use the API directly

The task server has a REST API. Try it:

```bash
# List all tasks
curl http://127.0.0.1:8052/tasks

# Get server status
curl http://127.0.0.1:8052/status

# Create a task manually
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Add type hints to utils.py",
    "role": "backend",
    "priority": 3,
    "scope": ["src/utils.py"]
  }'
```

## Step 9: Configure Bernstein

Create `bernstein.yaml` in your project root:

```yaml
# Maximum concurrent agents
max_agents: 4

# Model selection by complexity
model_policy:
  simple: "haiku"
  medium: "sonnet"
  complex: "opus"

# Orchestrator tick interval
tick_interval: 5

# Budget limits
budget:
  per_task_max_tokens: 100000
  per_run_max_cost_usd: 20.00
```

## Step 10: Stop Bernstein

```bash
bernstein stop
```

This gracefully terminates all running agents and shuts down the task server.

## What next?

- [Performance Tuning Guide](performance-tuning.md) -- optimize throughput and resource usage
- [Deployment Guide](deployment-guide.md) -- run in Docker, Kubernetes, or CI/CD
- [Hook System Guide](hook-system.md) -- integrate with your workflow
- [Security Hardening Guide](security-hardening.md) -- lock down for production
- [Cost Optimization Guide](cost-optimization.md) -- minimize API spend

## Troubleshooting

### "No agents available"

Install at least one CLI agent:

```bash
# Claude Code
npm install -g @anthropic/claude-code

# Codex
npm install -g @openai/codex
```

### "Port 8052 already in use"

Another Bernstein instance is running, or something else is using the port:

```bash
# Check what's using the port
lsof -i :8052

# Use a different port
BERNSTEIN_PORT=8053 bernstein run
```

### "Task failed: permission denied"

The agent does not have permission to modify a file. Check file ownership and permissions:

```bash
ls -la src/
```

### "Agent stalled"

An agent stopped responding. Bernstein auto-detects stalls and retries:

```bash
# Check agent status
bernstein status --agents

# Manually kill a stuck agent
bernstein stop --agent agent-xyz
```
