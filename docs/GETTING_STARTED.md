# Getting Started with Bernstein

Bernstein orchestrates short-lived CLI coding agents (Claude Code, Codex, Gemini CLI, etc.)
around a central task server.  One command starts the whole orchestra.

---

## Install

Requires Python 3.12+.

```bash
# From the repo root
pip install -e ".[dev]"

# Verify
bernstein --version
```

---

## Quick start

### 1. Initialise a workspace

Run this once inside your project directory:

```bash
cd my-project
bernstein init
```

This creates `.sdd/` — a lightweight file-based state directory:

```
.sdd/
  backlog/open/      task files waiting to be claimed
  backlog/done/      completed task files
  agents/            per-agent state & heartbeat files
  runtime/           PID files and log files
  docs/              project notes
  decisions/         architecture decision records
  config.yaml        server port, model defaults, worker limits
```

### 2. Start orchestration

Pass a plain-English goal:

```bash
bernstein start "Build a legal RAG system with hybrid retrieval and typed answers"
```

Or point at a YAML seed file:

```bash
# bernstein.yaml in the current directory
bernstein start --seed-file bernstein.yaml
```

Bernstein will:
1. Start the task server on `localhost:8052`
2. Inject an initial manager task
3. Spawn a manager agent (Claude Code, Opus, max effort)
4. The manager decomposes the goal and spawns specialist workers automatically

### 3. Monitor progress

```bash
bernstein status
```

Example output:

```
Tasks
──────────────────────────────────────────────────────────
 ID          Title                          Role    Status
──────────────────────────────────────────────────────────
 TSK-001     Plan and decompose goal        manager done
 TSK-002     Implement hybrid retrieval     backend in_progress
 TSK-003     Write unit tests               qa      open
──────────────────────────────────────────────────────────

Active Agents
───────────────────────────────────────────
 ID          Role     Status   Model  Tasks
───────────────────────────────────────────
 AGT-a1b2    backend  working  opus   1
───────────────────────────────────────────

Tasks: 3 total  1 done  1 in progress  0 failed
```

### 4. Add a task manually

Inject a task at any time while the server is running:

```bash
bernstein add-task "Add BM25 fallback to retriever" \
  --role backend \
  --description "Implement a BM25 sparse index alongside the dense vector index." \
  --priority 1 \
  --scope medium \
  --complexity medium
```

### 5. Filter tasks

```bash
# Show only open tasks
bernstein list-tasks --status-filter open

# Show only backend tasks
bernstein list-tasks --role backend

# Dump raw JSON (useful for scripting)
bernstein list-tasks --json
```

### 6. Check logs

```bash
bernstein logs --component server --lines 50
bernstein logs --component spawner --lines 30
```

### 7. Stop everything

```bash
bernstein stop
```

Sends a graceful shutdown signal to the task server, waits up to 10 s for
in-flight agents to finish, then kills the server and spawner processes.

```bash
# Shorter timeout if you need to stop quickly
bernstein stop --timeout 3
```

---

## Seed file format

`bernstein.yaml` lets you pre-define goals and initial tasks:

```yaml
goal: "Build a legal RAG system with hybrid retrieval and typed answers"

tasks:
  - title: "Implement vector store"
    role: backend
    priority: 1
    scope: medium
    complexity: medium

  - title: "Add BM25 sparse index"
    role: backend
    priority: 2
    scope: small
    complexity: low
    depends_on: ["TSK-001"]

  - title: "Write integration tests"
    role: qa
    priority: 2
    scope: medium
    complexity: medium
    depends_on: ["TSK-001", "TSK-002"]
```

---

## Configuration

`.sdd/config.yaml` (created by `bernstein init`):

| Key | Default | Description |
|-----|---------|-------------|
| `server_port` | `8052` | Task server port |
| `max_workers` | `6` | Max simultaneous worker agents |
| `default_model` | `sonnet` | Default model for workers |
| `default_effort` | `high` | Default effort level |

---

## How agents are selected

| Task property | Model | Effort |
|--------------|-------|--------|
| `role=manager` | opus | max |
| `role=security` | opus | max |
| `scope=large, complexity=high` | opus | high |
| `complexity=medium` (any scope) | sonnet | high |
| `complexity=low` (any scope) | sonnet | normal |

---

## Task lifecycle

```
open → claimed → in_progress → done
                              ↘ failed
                 blocked (dependency not met)
```

When a task moves to `done`, the janitor checks completion signals
(file exists, test passes, etc.) before finalising.

---

## Headless mode

Run without the live dashboard — useful for overnight runs or CI pipelines:

```bash
bernstein --headless
```

Output is written to `.sdd/runtime/` logs instead of the terminal.  Combine
with a goal or seed file as normal:

```bash
bernstein --headless -g "Refactor the auth module"
bernstein --headless --seed-file bernstein.yaml
```

---

## Continuous self-improvement (evolve mode)

`--evolve` runs a continuous loop where Bernstein analyses its own metrics,
proposes improvements, validates them in a sandbox, and applies the safe ones
automatically.

```bash
# Run indefinitely
bernstein --evolve

# Stop after 10 cycles
bernstein --evolve --max-cycles 10

# Stop after spending $5
bernstein --evolve --budget 5

# Run cycles every 2 minutes instead of the default 5
bernstein --evolve --interval 120
```

| Flag | Default | Description |
|------|---------|-------------|
| `--evolve` / `-e` | — | Enable continuous self-improvement mode |
| `--max-cycles N` | `0` (unlimited) | Stop after N evolve cycles |
| `--budget N` | `0` (unlimited) | Stop after $N spent |
| `--interval N` | `300` | Seconds between cycles |

Combine `--headless` with `--evolve` for unattended overnight runs:

```bash
bernstein --evolve --max-cycles 20 --budget 10 --headless
```

Low-risk proposals (L0/L1) are applied automatically.  Higher-risk proposals
(L2+) are saved to `.sdd/evolution/deferred.jsonl` for human review — see
`bernstein evolve review` below.

---

## Managing evolution proposals

The `bernstein evolve` subcommand lets you inspect and approve pending
proposals.

### List proposals awaiting review

```bash
bernstein evolve review
```

### Approve a proposal

```bash
bernstein evolve approve <PROPOSAL_ID>

# Attribute the approval to a specific reviewer
bernstein evolve approve <PROPOSAL_ID> --reviewer alice
```

### Run the autoresearch loop manually

```bash
# Default: 2-hour window, up to 24 proposals
bernstein evolve run

# Short exploratory session
bernstein evolve run --window 30m

# Longer session with more experiments
bernstein evolve run --window 4h --max-proposals 48
```

| Flag | Default | Description |
|------|---------|-------------|
| `--window` | `2h` | Duration (e.g. `30m`, `1h30m`, `4h`) |
| `--max-proposals N` | `24` | Max proposals evaluated per session |
| `--cycle N` | `300` | Seconds per experiment cycle |

Results are logged to `.sdd/evolution/experiments.jsonl`.

---

## Benchmark suite

Run the golden benchmark suite to measure Bernstein's capabilities:

```bash
# Run all tiers (smoke, capability, stretch)
bernstein benchmark run

# Run only the smoke tier (fast sanity check)
bernstein benchmark run --tier smoke

# Run only stretch benchmarks
bernstein benchmark run --tier stretch

# Run without saving results
bernstein benchmark run --no-save
```

Results are saved to `.sdd/benchmarks/YYYY-MM-DD.jsonl` by default.  The
suite is organised into three tiers:

| Tier | Purpose |
|------|---------|
| `smoke` | Fast sanity checks — should always pass |
| `capability` | Core feature validation |
| `stretch` | Aspirational targets, expected to fail until the system matures |

---

## Next steps

- See `docs/DESIGN.md` for the full architecture
- Add role templates in `templates/roles/` to customise agent prompts
- Use `bernstein add-task` to inject hotfix tasks mid-sprint
- Review `.sdd/runtime/server.log` if anything behaves unexpectedly
