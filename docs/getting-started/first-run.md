---
search:
  boost: 2
---

# First run

**What this page does**: Takes you from "Bernstein is installed" to "I just watched
my first orchestrated task complete." About 5 minutes.

**You'll end up with**: A `.sdd/` workspace, a one-line goal that ran, and a clear
`bernstein recap` summary of what the agents did.

If `bernstein --version` doesn't work yet, finish the [install page](install.md) first.

On a terminal, the installed CLI prints a preparation message while it
checks the run. The dashboard becomes available after local preflight and
server startup. The first agent can take longer. The npm launcher can also
provision a Python environment on first use, before Bernstein starts.
Refresh the optional specialist catalog with `bernstein agents sync` before
a run that needs it. An active run keeps the local catalog it loaded.

---

## Step 1: Pick (and authenticate) one CLI agent for this walkthrough

This walkthrough uses a CLI coding agent; Bernstein also supports other adapter types. See the [adapter catalog](../adapters/index.md) for the other options.

Install and sign in to one worker. For example, if your Claude account includes Claude Code access, run:

```bash
npm install -g @anthropic-ai/claude-code
claude auth login
```

Other worker installs:

```bash
npm install -g @openai/codex          # Codex CLI (OpenAI)
npm install -g @google/gemini-cli     # Gemini CLI (Google)
pip install aider-chat                # Aider (any provider)
```

For Codex, run `codex login` to use your eligible account. API billing is a separate option: configure the provider key for the worker you selected. To try a local fixture without a provider account, see **Try the demo first** below.

---

## Step 2: Open a project

`cd` into any git repository. If you don't have one, start from scratch:

```bash
mkdir my-first-bernstein && cd my-first-bernstein
git init
git commit --allow-empty -m "init"
```

Bernstein needs a git repo because each coding agent works in its own git worktree.

---

## Step 3: `bernstein init`

```bash
bernstein init
```

Expected output:

```
Created .sdd/config.yaml
Created bernstein.yaml
Created templates/ (default roles & prompts)
Created .gitignore (added .sdd/runtime/)

Done. Next steps:
Project setup: action needed
Working branch: main
Worker: auto  |  Internal planner: openrouter_free
  Create a working branch with git switch -c bernstein/first-run.
  Edit bernstein.yaml: set a goal for bernstein run.
```

This excerpt shows a new repository on `main`. The branch, worker, planner and remaining steps depend on the checkout and configuration. Four things happened:

- **`.sdd/`** - your file-based state directory (backlog, logs, metrics, signals). This is
  the single source of truth. Inspect it, back it up, recover from it.
- **`bernstein.yaml`** - your project config. Set the goal and worker before starting.
- **`templates/`** - the default role prompts, copied in so you can edit them per project.
- **`.gitignore`** - created or appended to, so `.sdd/runtime/` stays out of git.

A minimal `bernstein.yaml` looks like:

```yaml
goal: "Add a hello() function to src/greeting.py that returns Hello, world!"
cli: codex                      # choose the worker you installed and signed in to
model: gpt-5.4                  # use a model supported by the chosen worker
internal_llm_provider: none     # leave optional internal LLM calls disabled
budget: "$5"                    # spending cap for the run (accepts "$5", 5, or 5.0)
```

`cli` selects the installed coding agent used for worker tasks. The separate
`internal_llm_provider` setting configures Bernstein's internal LLM calls, such as
planning and reviews; `internal_llm_model` selects the model for those calls.
These internal settings do not select the worker adapter or replace `cli`.

---

## Step 4: Run your first goal

Run `bernstein doctor` to check your agent setup. Its `Project setup` section reports the current checkout and selected configuration. In `doctor --json`, the optional `project_setup` object has `version` (1), `ready` (boolean), `branch`, `worker`, and `planner` (strings or null), and `next_steps` (a list of actions). Installation checks have their own results. If you're on the project's
default branch, create and switch to a working branch before running the goal.
Bernstein merges verified changes into the branch you start from and refuses
merge-capable runs from the default branch.

```bash
git switch -c bernstein/first-run  # skip if already on a working branch
bernstein run --worker backend
```

What happens:

1. Bernstein starts the task server on port 8052.
2. The configured goal becomes one task for the backend worker.
3. An agent spawns in an isolated git worktree.
4. The janitor runs quality gates (lint, type-check, tests) on the agent's output.
5. Verified work merges back to your branch. Review it with `git diff <default-branch>...HEAD` and open a pull request when you want to merge it.

You'll see a live TUI in your terminal. Wait for it to finish - usually under 3 minutes
for a simple goal.

---

## Step 5: Watch progress (optional, in another terminal)

```bash
bernstein status     # one-shot snapshot
bernstein live       # full TUI dashboard (attach to a running session)
bernstein gui serve  # opens http://127.0.0.1:8052/ui/ in your browser
```

`bernstein status` prints a banner, the task counts, and a **Bernstein Agents**
table with one row per running session — session, role, CLI, model, worker,
skills, worker PID, agent PID and runtime — followed by the spend so far. Use
`bernstein status --json` when you want a shape to parse rather than to read.

---

## Step 6: See what happened

While the server is running, use `bernstein recap` for its current totals. After it exits, read the local completion journal:

```bash
bernstein recap --archive .sdd/runtime/tasks.jsonl
```

```
  Metric        Value
  Total tasks   1
  Completed     1
  Failed        0
  Success rate  100.0%
```

The local journal supplies the task totals. With a running server, `bernstein recap` also reports the git diff, quality scores and cost breakdown.

The summary card also reports a **Model routing savings** number when the
cascade router downgraded any task off Opus - see
[run savings summary](../operations/cost-optimization.md#run-savings-summary)
for how it is computed and what the caveats are.

Inspect a specific task:

```bash
bernstein diff <task-id>     # the git diff the agent produced
bernstein trace <task-id>    # which decisions fired and why
bernstein logs tail -a <session-id>  # agent stdout for one session
```

---

## Try the demo first (no API key needed)

If you don't have an API key set up, run the zero-config demo instead. It creates a temp
Flask app with 4 intentional bugs and runs **mock** agents to fix them - no provider calls,
no spend.

```bash
bernstein demo            # mock agents (about a minute or two)
bernstein demo --dry-run  # preview the plan without spawning
bernstein demo --real     # use real agents (requires API key, ~$0.15)
```

This is the fastest way to see the orchestrator move tasks through the lifecycle without
configuring anything.

> **Preview.** Two known first-run failures: a cold run can exceed the task-server
> readiness budget and exit 1 (tracked from
> [#3825](https://github.com/sipyourdrink-ltd/bernstein/issues/3825)), and on a
> Windows console using a legacy code page the command aborts with a
> `UnicodeEncodeError` on the first `✓` it prints
> ([#3901](https://github.com/sipyourdrink-ltd/bernstein/issues/3901)). A second
> run on a warm cache starts in under a second.

---

## Common first-run errors

- **Installation**. If `bernstein --version` fails, complete the [install guide](install.md).
- **Worker**. If the worker is missing, install the CLI named by `cli`. If it is installed but the login check fails, sign in to that worker and rerun `bernstein doctor`.
- **Planner**. If you enabled an internal planner, check its own provider configuration. For the single-worker example above, leave it disabled.
- **Branch**. Create a working branch from a committed checkout. A fresh repository needs an initial commit. With a detached HEAD, create a branch for the resulting changes.
- **Run failure**. If setup passes but a task fails, inspect its status with `bernstein recap --archive .sdd/runtime/tasks.jsonl` and read the session output with `bernstein logs tail -a <session-id>`.

### `No agents available`

`bernstein doctor` shows every `Adapter:` row red. Install at least one CLI agent
(Step 1) and run its login flow. Then:

```bash
bernstein agents discover    # rescan agent-role catalogs
bernstein doctor             # confirm adapters + auth are green
```

### `Port 8052 already in use`

Another Bernstein session is still running, or the port is taken:

```bash
bernstein stop --force                   # kill stuck session
bernstein run --goal "..." --port 8053   # use a different port
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
