# Quickstart Tutorial: Your First Multi-Agent Run

**What you'll build**: Three AI agents work in parallel to add input validation, error handling, and a test suite to a Flask TODO API — all orchestrated by Bernstein.

**What you'll learn**:
- How to install Bernstein and verify the setup
- How to run `bernstein quickstart` — a zero-config demo that requires no project setup
- How to read the live task dashboard and understand agent output
- How to inspect the results
- How to apply the same workflow to your own project

**Time**: 5–10 minutes

**Prerequisites**:
- [ ] Python 3.12+ (`python --version`)
- [ ] At least one supported CLI coding agent installed (see below)
- [ ] An API key for that agent's LLM provider

> **No agent yet?** The quickstart runs in mock mode without one. You won't see real code changes, but you'll see the full orchestration flow.

---

## Step 1: Install Bernstein

Choose the method that matches your workflow:

```bash
# Fastest (recommended)
uv tool install bernstein

# pip / pipx
pip install bernstein
pipx install bernstein

# Homebrew
brew tap chernistry/bernstein && brew install bernstein
```

Verify the install:

```bash
bernstein --version
```

You should see something like `bernstein 1.x.x`.

> **Trouble installing?** Run `bernstein doctor` after install — it checks Python version, agent availability, API keys, and port conflicts.

---

## Step 2: Install a CLI agent (skip if you have one)

Bernstein works with any of these. Install whichever you prefer:

| Agent | Install |
|-------|---------|
| Claude Code | `npm install -g @anthropic-ai/claude-code && claude login` |
| Codex CLI | `npm install -g @openai/codex && codex login` |
| Gemini CLI | `npm install -g @google/gemini-cli && gemini login` |
| Aider | `pip install aider-chat` |

After installing, set your API key:

```bash
# Claude (Anthropic)
export ANTHROPIC_API_KEY="sk-ant-..."

# Codex (OpenAI)
export OPENAI_API_KEY="sk-..."

# Gemini (Google)
export GOOGLE_API_KEY="..."
```

> Add the export to your `.zshrc` / `.bashrc` so you don't have to repeat it.

---

## Step 3: Run the quickstart demo

```bash
bernstein quickstart
```

That's the entire command. Bernstein:

1. Creates a temporary Flask TODO API project with deliberate gaps
2. Seeds three backlog tasks: input validation, error handling, and a pytest suite
3. Auto-detects your installed agent
4. Starts a task server and spawner
5. Agents claim and complete tasks in parallel
6. Prints a summary when all tasks finish

**Expected output** (real agent mode):

```
  ____                        _        _
 |  _ \  ___  _ __ _ __  ___| |_ ___(_)_ __
 | |_) |/ _ \| '__| '_ \/ __| __/ _ \ | '_ \
 |  _ <  (_) | |  | | | \__ \ |_  __/ | | | |
 |_| \_\___/|_|  |_| |_|___/\__\___|_|_| |_|

Cost estimate: ~$0.20 (3 tasks)
Adapter: claude  |  Timeout: 300s

Creating quickstart project in /tmp/bernstein-quickstart-abc123…
✓ Flask TODO API project created
✓ 3 tasks seeded: input validation · error handling · pytest suite

Starting orchestration…
  ✓ [backend] Add input validation to TODO API
  ✓ [backend] Add 404 error handling for missing TODO items
  ✓ [qa] Write pytest test suite for the TODO API
✓ Orchestration finished
```

**In mock mode** (no agent installed), the output is the same but no real code is written — you see the task lifecycle without spending API credits.

---

## Step 4: Read the output

After orchestration finishes, Bernstein prints a summary table:

```
── Quickstart Summary ────────────────────
 Metric              Value
 Tasks completed     3 / 3
 Elapsed             47s
 Python files        3
 API cost            $0.1823
```

What each field means:

| Field | What it tells you |
|-------|-------------------|
| **Tasks completed** | How many tasks agents finished successfully |
| **Elapsed** | Wall-clock time from start to last task |
| **Python files** | Files created or modified in the project |
| **API cost** | Approximate spend with your LLM provider |

If any tasks failed, a "Tasks failed" row appears in red. Check `bernstein logs -f` to see what went wrong.

---

## Step 5: Inspect the results

By default the temporary directory is deleted after the run. To keep it:

```bash
bernstein quickstart --keep
```

Then inspect what the agents produced:

```bash
# See the generated files
ls /tmp/bernstein-quickstart-*/

# The Flask app with validation and error handling added
cat /tmp/bernstein-quickstart-*/app.py

# The generated test suite
cat /tmp/bernstein-quickstart-*/tests/test_api.py

# Run the tests yourself
cd /tmp/bernstein-quickstart-*/
pip install -r requirements.txt
pytest tests/ -q
```

The test suite should pass — agents wrote the implementation and the tests in parallel, and the orchestrator verified each task before marking it done.

---

## Step 6: What just happened

Here's what Bernstein did under the hood:

```
bernstein quickstart
│
├── Creates temp project (Flask TODO API with gaps)
├── Starts task server at http://127.0.0.1:8056
├── Syncs 3 backlog YAML files → task server
│
├── Spawns backend agent #1
│   └── Claims "Add input validation"
│   └── Edits app.py, marks task done
│
├── Spawns backend agent #2 (parallel)
│   └── Claims "Add 404 error handling"
│   └── Edits app.py, marks task done
│
├── Spawns qa agent (after backend tasks complete)
│   └── Claims "Write pytest test suite"
│   └── Creates tests/test_api.py, marks task done
│
└── Prints summary, cleans up temp dir
```

The task server at `http://127.0.0.1:8056` is the coordinator. Every agent polls it for work, reports progress, and marks tasks done. Nothing happens inside an agent's memory — all state lives in files.

---

## Step 7: Apply this to your own project

The quickstart is a demo. To use Bernstein on your actual codebase:

```bash
# 1. Go to your project
cd /path/to/your-project

# 2. Initialise Bernstein
bernstein init
```

This creates `.sdd/` — a lightweight state directory. Add it to `.gitignore`:

```bash
echo ".sdd/" >> .gitignore
```

**Option A: Describe a goal (Bernstein plans the tasks)**

```bash
bernstein -g "Add rate limiting to the API with Redis, tests, and docs"
```

Bernstein spawns a manager agent, decomposes the goal into tasks, then executes them in parallel.

**Option B: Write a plan file (you define the tasks)**

```yaml
# plan.yaml
goal: "Add rate limiting"

stages:
  - name: implementation
    steps:
      - goal: "Implement Redis-backed rate limiter middleware"
        role: backend
        complexity: medium

      - goal: "Write integration tests for rate limiting"
        role: qa
        complexity: low

      - goal: "Update API docs with rate limit headers"
        role: docs
        complexity: low
```

```bash
bernstein run plan.yaml
```

Plan files skip the LLM planning step and go straight to execution — deterministic, repeatable, CI-friendly.

---

## Monitoring a run

While agents are working:

```bash
# TUI dashboard (default — blocks until done)
bernstein live

# Web dashboard
bernstein dashboard   # opens http://localhost:8052/dashboard

# Task status
bernstein status

# Follow agent output
bernstein logs -f

# Cost so far
bernstein cost
```

---

## Stopping and resuming

```bash
# Graceful stop (agents finish current subtask, save state)
bernstein stop

# Hard stop (immediate)
bernstein stop --force

# Snapshot current progress
bernstein checkpoint

# Resume from snapshot
bernstein run --from-plan .sdd/checkpoint-latest.yaml
```

---

## Common first-run issues

### "No agents detected"

```bash
bernstein doctor          # see which agents Bernstein found
bernstein agents discover # re-scan for installed agents
```

Install at least one agent (see Step 2) and run its login command.

### "Port 8052 already in use"

A previous session is still running:

```bash
bernstein stop --force
```

### Tasks stay in "claimed" forever

An agent crashed without reporting. Run:

```bash
bernstein doctor --fix    # clears stale task locks
```

### Cost is higher than expected

Set a spending cap:

```bash
bernstein -g "Refactor auth module" --budget 3.00
```

Agents stop spawning when the budget is reached.

---

## Next steps

- **[Getting Started guide](GETTING_STARTED.md)** — full command reference, monitoring, and configuration
- **[Deployment guide](deployment-guide.md)** — run Bernstein in CI, Docker, Kubernetes, or on a shared team server
- **[Plan file format](plans.md)** — write multi-stage plans with dependencies
- **[Configuration reference](CONFIG.md)** — tune models, budgets, and role policies
- **[Architecture](ARCHITECTURE.md)** — understand how the task server, spawner, and agents work together
