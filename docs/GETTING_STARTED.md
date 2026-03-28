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

Pass a plain-English goal inline:

```bash
bernstein -g "Build a legal RAG system with hybrid retrieval and typed answers"
```

Or point at a YAML seed file (Bernstein looks for `bernstein.yaml` automatically):

```bash
bernstein
```

Bernstein will:
1. Start the task server on `localhost:8052`
2. Inject an initial manager task
3. Spawn a manager agent (Claude Code, Opus, max effort)
4. The manager decomposes the goal and spawns specialist workers automatically

### 3. Monitor progress

**Built-in TUI dashboard** — `bernstein` blocks in a live terminal dashboard by default after starting
orchestration.  Press `Ctrl+C` to exit the dashboard (the agents keep running).

**Standalone dashboard** — attach the dashboard to an already-running session without starting a new one:

```bash
bernstein live
```

**HTTP API** — query the task server directly:

```bash
# Dashboard summary (counts by status and role)
curl http://127.0.0.1:8052/status

# All tasks (optionally filter by status)
curl http://127.0.0.1:8052/tasks
curl "http://127.0.0.1:8052/tasks?status=open"
curl "http://127.0.0.1:8052/tasks?status=in_progress"
```

### 4. Add a task manually

Inject a task while the server is running via the HTTP API:

```bash
curl -s -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Add BM25 fallback to retriever",
    "role": "backend",
    "description": "Implement a BM25 sparse index alongside the dense vector index.",
    "priority": 1,
    "scope": "medium",
    "complexity": "medium"
  }'
```

### 5. Check logs

Log files are written to `.sdd/runtime/`:

```bash
tail -f .sdd/runtime/server.log
tail -f .sdd/runtime/spawner.log
```

### 6. Stop everything

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
bernstein --headless -g "Refactor the auth module"
bernstein --headless   # auto-discovers bernstein.yaml
```

Output is written to `.sdd/runtime/` logs instead of the terminal.

---

## Dry-run mode

Preview the task plan that Bernstein would create without actually spawning any
agents or writing state:

```bash
bernstein --dry-run -g "Build a REST API with auth"
bernstein --dry-run   # preview plan from bernstein.yaml
```

The output shows the tasks, roles, priorities, and dependencies that the manager
agent would generate.  Nothing is written to disk and no agents are spawned.
Useful for validating a goal or seed file before committing to a full run.

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
```

### Run the autoresearch loop manually

```bash
bernstein evolve run
```

---

## Agent spend

```bash
bernstein cost
```

Shows cost, tokens, and duration per model from `.sdd/metrics/`.

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
```

Results are saved to `.sdd/benchmarks/YYYY-MM-DD.jsonl` by default.

| Tier | Purpose |
|------|---------|
| `smoke` | Fast sanity checks — should always pass |
| `capability` | Core feature validation |
| `stretch` | Aspirational targets, expected to fail until the system matures |

---

## Task API reference

The task server runs on `http://127.0.0.1:8052`.  All request and response bodies are JSON.

### POST /tasks — create a task

**Required fields**

| Field | Type | Description |
|-------|------|-------------|
| `title` | `string` | Short name shown in the dashboard |
| `description` | `string` | Full instructions given to the agent |
| `role` | `string` | Role tag used for agent selection (e.g. `backend`, `qa`, `security`, `manager`) |

**Optional fields**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `priority` | `int` | `2` | Lower number = higher priority; `1` is urgent, `3` is low |
| `scope` | `string` | `"medium"` | Size hint: `"small"`, `"medium"`, `"large"` |
| `complexity` | `string` | `"medium"` | Difficulty hint: `"low"`, `"medium"`, `"high"` — used by the router to pick model |
| `estimated_minutes` | `int` | `30` | Rough time budget; informational only |
| `depends_on` | `list[string]` | `[]` | Task IDs that must be `done` before this task becomes claimable |
| `owned_files` | `list[string]` | `[]` | File paths the agent is expected to own / modify |
| `cell_id` | `string \| null` | `null` | Optional cell grouping identifier |
| `task_type` | `string` | `"standard"` | One of `"standard"`, `"upgrade"` |
| `model` | `string \| null` | `null` | Override model selection: `"opus"`, `"sonnet"`, `"haiku"` |
| `effort` | `string \| null` | `null` | Override effort selection: `"max"`, `"high"`, `"medium"`, `"low"` |
| `completion_signals` | `list[object]` | `[]` | Conditions the janitor checks before marking a task done (see below) |

**`completion_signals` object**

Each entry has exactly two fields:

| Field | Type | Description |
|-------|------|-------------|
| `type` | `string` | One of: `path_exists`, `glob_exists`, `test_passes`, `file_contains`, `llm_review`, `llm_judge` |
| `value` | `string` | The path, glob pattern, shell command, or prompt depending on `type` |

Example — require a file to exist and tests to pass before the task is finalised:

```json
{
  "title": "Implement vector store",
  "role": "backend",
  "description": "Write src/store.py with a FAISS-backed vector store.",
  "completion_signals": [
    {"type": "path_exists", "value": "src/store.py"},
    {"type": "test_passes", "value": "uv run pytest tests/test_store.py -x -q"}
  ]
}
```

---

### POST /tasks/{id}/complete — mark a task done

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `result_summary` | `string` | yes | Short summary of what was done; stored in the archive |

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/TASK_ID/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "Implemented FAISS store in src/store.py, all tests pass."}'
```

---

### POST /tasks/{id}/fail — mark a task failed

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | `string` | yes | Why the task failed; stored in the archive |

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/TASK_ID/fail \
  -H "Content-Type: application/json" \
  -d '{"reason": "FAISS not available in the sandbox environment."}'
```

---

### POST /tasks/{id}/cancel — cancel a task

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | `string` | yes | Why the task was cancelled; stored in the archive |

Cannot cancel a task that is already in a terminal state (`done`, `failed`, `cancelled`).

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/TASK_ID/cancel \
  -H "Content-Type: application/json" \
  -d '{"reason": "Requirements changed — no longer needed."}'
```

---

### Other endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | List all tasks; optional `?status=open\|in_progress\|done\|failed` filter |
| `GET` | `/tasks/next/{role}` | Claim the highest-priority open task for the given role |
| `POST` | `/tasks/{id}/claim` | Claim a specific task by ID |
| `POST` | `/tasks/claim-batch` | Claim multiple tasks by ID in one call |
| `GET` | `/status` | Dashboard summary (counts by status and role) |
| `GET` | `/bulletin` | Recent bulletin board messages from agents |

---

## CLI reference

Run `bernstein --help` for the full option list.  The commands below are all
top-level subcommands:

| Command | Description |
|---------|-------------|
| `bernstein [-g GOAL] [--evolve] [--headless]` | Start orchestration; shows live TUI dashboard unless `--headless` |
| `bernstein --dry-run [-g GOAL]` | Preview the task plan without spawning any agents |
| `bernstein init` | Initialise a `.sdd/` workspace in the current directory |
| `bernstein stop [--timeout N]` | Gracefully stop all agents and the task server |
| `bernstein cancel TASK_ID [-r REASON]` | Cancel a running or queued task |
| `bernstein cost` | Show agent spend: cost, tokens, and duration per model |
| `bernstein live [--interval N]` | Attach the live TUI dashboard to a running session |
| `bernstein logs [AGENT_ID]` | Tail agent log output (all agents, or a specific one) |
| `bernstein plan [--json]` | Show task backlog as a table, or export to JSON |
| `bernstein benchmark run [--tier smoke\|capability\|stretch\|all]` | Run the tiered golden benchmark suite |
| `bernstein agents sync` | Refresh all agent catalogs and update the cache |
| `bernstein agents list [--source local\|agency\|all]` | List available agents from loaded catalogs |
| `bernstein agents validate` | Validate all agent catalog files and report issues |
| `bernstein evolve review` | List evolution proposals awaiting human review |
| `bernstein evolve approve PROPOSAL_ID` | Approve a specific evolution proposal |
| `bernstein evolve run [--window N] [--max-proposals N]` | Run the autoresearch evolution loop |

---

## Next steps

- See `docs/DESIGN.md` for the full architecture
- Add role templates in `templates/roles/` to customise agent prompts
- Review `.sdd/runtime/server.log` if anything behaves unexpectedly
