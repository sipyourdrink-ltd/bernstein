# Interactive Quickstart Tutorial

Get Bernstein running and orchestrating agents in under 10 minutes.

**What you'll build**: A working multi-agent setup that reads a goal, spawns agents in
isolated git worktrees, and merges verified results back to your branch automatically.

**What you'll need**:
- [ ] Python 3.12 or later
- [ ] Git (any recent version)
- [ ] At least one CLI coding agent (Claude Code, Codex, or Gemini — see Step 2)
- [ ] An API key for your chosen agent

---

## Step 1: Install Bernstein

```bash
# Recommended — uv installs into an isolated tool environment
uv tool install bernstein

# Alternatives
pip install bernstein
pipx install bernstein
```

Verify the install:

```bash
bernstein --version
```

You should see something like:

```
bernstein 1.5.3
```

> **If you see "command not found"**: Make sure your tool bin directory is on `$PATH`.
> For uv: `export PATH="$HOME/.local/bin:$PATH"`. For pip: check `pip show -f bernstein`.

---

## Step 2: Check for CLI agents

Bernstein does not run models directly — it orchestrates CLI coding agents that you install
separately. Check which ones are available on your system:

```bash
bernstein agents
```

Example output:

```
Available agents:
  claude    ✓  Claude Code (claude)
  codex     ✓  Codex CLI (codex)
  gemini    ✗  Not found — install: npm i -g @google/gemini-cli
  aider     ✗  Not found — install: pip install aider-chat
```

You need at least one `✓`. If none are installed:

```bash
# Claude Code (Anthropic)
npm install -g @anthropic/claude-code

# Codex (OpenAI)
npm install -g @openai/codex

# Gemini CLI (Google)
npm install -g @google/gemini-cli
```

---

## Step 3: Set your API key

Each CLI agent authenticates with its own provider. Bernstein passes the environment
through to the agents — set the key for whichever agent you installed:

```bash
# Claude Code
export ANTHROPIC_API_KEY="sk-ant-..."

# Codex
export OPENAI_API_KEY="sk-..."

# Gemini
export GEMINI_API_KEY="..."
```

Add the export to your shell profile (`~/.zshrc`, `~/.bashrc`) so it persists across sessions.

---

## Step 4: Initialize a project

Navigate to any git repository (or create one) and initialize Bernstein's state directory:

```bash
cd your-project        # Must be a git repository
bernstein init
```

Expected output:

```
✓ Initialized .sdd/ state directory
✓ Created bernstein.yaml (edit to configure agents and budget)
✓ Ready — run `bernstein -g "your goal"` to start
```

This creates:
- `.sdd/` — file-based state (backlog, logs, metrics, signals)
- `bernstein.yaml` — project configuration

> **No git repository?** Run `git init && git commit --allow-empty -m "init"` first.
> Bernstein requires git for worktree isolation.

---

## Step 5: Run your first orchestration

Give Bernstein a goal:

```bash
bernstein -g "Add a hello() function to src/utils.py that returns 'Hello, world!'"
```

Bernstein will:
1. Start the task server on port 8052
2. Break the goal into tasks
3. Spawn agents in isolated git worktrees
4. Monitor agent progress via heartbeats
5. Run quality gates (lint, type-check, tests) on the output
6. Merge verified results back to your branch

Watch it work in real time:

```bash
# In another terminal — live TUI dashboard
bernstein live

# Or a quick status snapshot
bernstein status
```

Example `bernstein status` output:

```
Tasks: 3 open · 1 in-progress · 0 done · 0 failed
Agents: 1 running (agent/abc12345 — backend)
Spend:  $0.04 so far
```

---

## Step 6: Understand the output

When a task completes, Bernstein merges the changes and shows a summary:

```bash
bernstein recap
```

Example output:

```
Run summary — 3 tasks completed in 4m 12s

  ✓ backend-abc12345  Add hello() to utils.py          $0.03  2m 10s
  ✓ qa-def67890       Write tests for hello()           $0.01  1m 45s
  ✗ docs-ghi11111     Update README                     $0.00  failed (retrying)

Total: $0.04 · 2 merged · 1 retrying
```

Inspect a specific task's changes:

```bash
bernstein diff <task-id>     # Git diff produced by the agent
bernstein trace <task-id>    # Decision trace (which rules fired, what was approved)
bernstein logs -a <task-id>  # Full agent output
```

---

## Step 7: Run a plan file

For deterministic, repeatable execution, describe your work in a YAML plan file instead
of a natural language goal. Create `plans/hello.yaml`:

```yaml
name: "Hello Bernstein"
description: "A simple two-stage plan"

stages:
  - name: implementation
    steps:
      - goal: "Create src/greeting.py with a greet(name: str) -> str function that returns 'Hello, {name}!'"
        role: backend
        priority: 1
        scope: ["src/greeting.py"]
        complexity: simple

  - name: tests
    depends_on: [implementation]    # Waits for implementation stage to finish
    steps:
      - goal: "Write pytest tests for the greet() function in tests/test_greeting.py"
        role: qa
        priority: 2
        scope: ["tests/test_greeting.py"]
        complexity: simple
```

Run it:

```bash
bernstein run plans/hello.yaml
```

The `tests` stage waits for `implementation` to finish. Bernstein manages the dependency
automatically — you do not need to sequence the commands yourself.

---

## Step 8: Check cost and token usage

```bash
bernstein cost
```

Example output:

```
Cost breakdown — last run

  claude (backend)    2,341 tokens   $0.012
  claude (qa)         1,102 tokens   $0.006

  Total:              3,443 tokens   $0.018
  Budget remaining:   $19.98 / $20.00
```

Set a per-run budget limit in `bernstein.yaml`:

```yaml
budget:
  per_task_max_tokens: 100000
  per_run_max_cost_usd: 5.00     # Hard stop if exceeded
```

---

## Step 9: Open the web dashboard

While Bernstein is running, open the dashboard in your browser:

```
http://127.0.0.1:8052/dashboard
```

The dashboard shows:
- Active agents and their current tasks
- Task queue (open, in progress, completed, failed)
- Token usage and cost estimate
- Recent activity timeline
- Agent logs (live streaming)

---

## Step 10: Stop Bernstein

```bash
bernstein stop
```

This gracefully drains in-progress tasks (10-second timeout by default), then shuts down
the task server and all agents.

```bash
bernstein stop --force   # Hard kill without draining
```

---

## What next?

You have a working Bernstein setup. Here are common next steps:

- **Add more agents**: Run `bernstein agents` to see what else is installable
- **Configure model routing**: Set `model_policy` in `bernstein.yaml` to use cheaper models for simple tasks
- **Write a plan file**: For real project work, a plan file gives you more control than an inline goal
- **Set up guardrails**: Add `.bernstein/rules.yaml` to control what agents are allowed to do

Useful references:

- [Configuration reference](CONFIG.md) — full `bernstein.yaml` options
- [Security Hardening Guide](security-hardening.md) — permission modes, sandboxing, audit logging
- [Architecture guide](ARCHITECTURE.md) — how the orchestrator, spawner, and janitor work
- [Deployment guide](deployment-guide.md) — Docker, Kubernetes, CI/CD
- [Cost optimization](cost-optimization.md) — reduce API spend

---

## Troubleshooting

### "No agents available"

```bash
bernstein agents    # See which agents are installed and which are missing
```

Install at least one:

```bash
npm install -g @anthropic/claude-code    # Claude Code
npm install -g @openai/codex             # Codex
```

### "Port 8052 already in use"

Another Bernstein instance is running, or another process has the port:

```bash
lsof -i :8052                    # Find what's using the port
BERNSTEIN_PORT=8053 bernstein run # Use a different port
```

### "Task failed: permission denied"

The agent tried to modify a file outside its role's allowed paths. Check which file caused the violation:

```bash
bernstein trace <task-id>   # Shows which permission rule fired
```

To allow it, add the path to the role's allowed paths in `bernstein.yaml`:

```yaml
roles:
  backend:
    allowed_paths:
      - "src/**"
      - "config/**"   # Add this
```

### "Agent stalled / no heartbeat"

Bernstein detects stalled agents automatically and retries the task. To check status manually:

```bash
bernstein status --agents   # Show agent heartbeat times
bernstein stop --agent <agent-id>   # Manually kill a specific agent
```

### "bernstein init fails — not a git repository"

```bash
git init
git commit --allow-empty -m "init"
bernstein init
```

### API key errors from the agent

Bernstein passes your shell environment to agents unchanged. Verify the key is set:

```bash
echo $ANTHROPIC_API_KEY    # Should show your key (not empty)
```

If it's empty, set it and restart:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
bernstein run
```
