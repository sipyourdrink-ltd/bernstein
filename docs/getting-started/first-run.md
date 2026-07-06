# First run

**What this page does**: Takes you from "Bernstein is installed" to "I just watched
my first orchestrated task complete." About 5 minutes.

**You'll end up with**: A `.sdd/` workspace, a one-line goal that ran, and a clear
`bernstein recap` summary of what the agents did.

If `bernstein --version` doesn't work yet, finish the [install page](install.md) first.

---

## Step 1: Pick (and authenticate) one CLI agent

Bernstein orchestrates **other** CLI coding agents - it doesn't talk to LLM APIs directly.
You need at least one agent installed and logged in. Most people start with Claude Code:

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

Other one-line installs (pick whichever you have an API key for):

```bash
npm install -g @openai/codex          # Codex CLI (OpenAI)
npm install -g @google/gemini-cli     # Gemini CLI (Google)
pip install aider-chat                # Aider (any provider)
```

Don't have an API key on hand? Skip ahead to **Try the demo first** below - it works
without one.

---

## Step 2: Open a project

`cd` into any git repository. If you don't have one, start from scratch:

```bash
mkdir my-first-bernstein && cd my-first-bernstein
git init
git commit --allow-empty -m "init"
```

Bernstein needs a git repo because each agent works in its own git worktree.

---

## Step 3: `bernstein init`

```bash
bernstein init
```

Expected output:

```
✓ Initialized .sdd/ state directory
✓ Created bernstein.yaml (edit to configure agents and budget)
✓ Ready - run `bernstein -g "your goal"` to start
```

Two things happened:

- **`.sdd/`** - your file-based state directory (backlog, logs, metrics, signals). This is
  the single source of truth. Inspect it, back it up, recover from it.
- **`bernstein.yaml`** - your project config. The defaults are fine for a first run.

A minimal `bernstein.yaml` looks like:

```yaml
internal_llm_provider: claude   # or codex / gemini / aider - whatever you set up
budget:
  per_run_max_cost_usd: 5.00    # hard stop if a run blows past $5
```

---

## Step 4: Run your first goal

```bash
bernstein -g "Add a hello() function to src/greeting.py that returns 'Hello, world!'"
```

What happens:

1. Bernstein starts the task server on port 8052.
2. The manager breaks the goal into one or more tasks.
3. An agent spawns in an isolated git worktree.
4. The janitor runs quality gates (lint, type-check, tests) on the agent's output.
5. Verified work merges back to your branch.

You'll see a live TUI in your terminal. Wait for it to finish - usually under 3 minutes
for a simple goal.

---

## Step 5: Watch progress (optional, in another terminal)

```bash
bernstein status     # one-shot snapshot
bernstein live       # full TUI dashboard (attach to a running session)
bernstein dashboard  # opens http://127.0.0.1:8052/dashboard in your browser
```

`bernstein status` output looks like:

```
Tasks: 0 open · 1 in-progress · 0 done · 0 failed
Agents: 1 running (agent/abc12345 - backend)
Spend:  $0.04 so far
```

---

## Step 6: See what happened

Once it finishes:

```bash
bernstein recap
```

```
Run summary - 1 task completed in 1m 47s

  ✓ backend-abc12345  Add hello() to greeting.py     $0.03  1m 47s

Total: $0.03 · 1 merged · 0 failed
```

The summary card also reports a **Model routing savings** number when the
cascade router downgraded any task off Opus - see
[run savings summary](../operations/cost-optimization.md#run-savings-summary)
for how it is computed and what the caveats are.

Inspect a specific task:

```bash
bernstein diff <task-id>     # the git diff the agent produced
bernstein trace <task-id>    # which decisions fired and why
bernstein logs tail -a <task-id>  # full agent stdout
```

---

## Try the demo first (no API key needed)

If you don't have an API key set up, run the zero-config demo instead. It creates a temp
Flask app with 4 intentional bugs and runs **mock** agents to fix them - no provider calls,
no spend.

```bash
bernstein demo            # mock agents (~30 seconds)
bernstein demo --dry-run  # preview the plan without spawning
bernstein demo --real     # use real agents (requires API key, ~$0.15)
```

This is the fastest way to see the orchestrator move tasks through the lifecycle without
configuring anything.

---

## Common first-run errors

### `No agents available`

`bernstein agents` shows nothing checked. Install at least one CLI agent (Step 1) and run
its login flow. Then:

```bash
bernstein agents discover    # rescan
bernstein doctor             # confirm
```

### `Port 8052 already in use`

Another Bernstein session is still running, or the port is taken:

```bash
bernstein stop --force                   # kill stuck session
BERNSTEIN_PORT=8053 bernstein -g "..."   # use a different port
```

### `bernstein init fails - not a git repository`

```bash
git init && git commit --allow-empty -m "init"
bernstein init
```

### Agent stalls or no output

```bash
bernstein logs tail -f                   # follow live output
bernstein stop --timeout 3               # short-timeout drain
bernstein doctor --fix                   # clear stale locks
```

Most stalls trace back to a missing API key or an expired auth token. `bernstein doctor`
will name the agent that's failing.

---

## Stop cleanly

```bash
bernstein stop          # graceful drain, default 30s
bernstein stop --force  # hard kill, no drain
```

`stop` writes a SHUTDOWN signal under `.sdd/runtime/signals/` so agents finish their
current subtask and persist state before exiting.

---

## Next

You have a working Bernstein loop. Common next stops:

- **[Quickstart tutorial](quickstart-tutorial.md)** - same flow expanded to 10 steps,
  with cost tracking, plan files, and a web dashboard walkthrough.
- **[Configuration reference](../operations/CONFIG.md)** - every `bernstein.yaml` option,
  env vars, and `role_model_policy` for cheaper models on simple tasks.
- **[Architecture overview](../architecture/ARCHITECTURE.md)** - manager / janitor /
  spawner, deterministic scheduling, the `.sdd/` contract.
- **[Cost optimization](../operations/cost-optimization.md)** - mix cheap and heavy models,
  set per-run budgets, cache prompts.
