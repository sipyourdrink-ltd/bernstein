---
search:
  boost: 2
---

# CLI Reference

Bernstein ships a large surface of CLI commands registered in `cli/main.py`. This page is the single-source reference for every flag on every visible command. For driving Bernstein from a script, also read [`cli/task-lifecycle.md`](cli/task-lifecycle.md) and [`cli/replay.md`](cli/replay.md).

> **Find a command fast:** `Ctrl-F` for the command name. Every entry below cites its source as `cli/<file>:<line>`.
> **Get rich help in the terminal:** `bernstein --help` (root rich-formatted help) and `bernstein help-all` (the same, exhaustive). Per-command help: `bernstein <command> --help` works on every visible command and group.

---

## Root command flags

`bernstein` itself accepts these flags (defined at `cli/main.py:482-572`). Most of them only matter when invoked **without** a subcommand - i.e. when you run `bernstein` to start orchestration from `bernstein.yaml` or an inline `--goal`.

| Flag | Default | Meaning |
|---|---|---|
| `--version` | - | Print version and exit. |
| `-g, --goal TEXT` | none | Inline goal; bypasses the seed file. |
| `--json` | off | Emit machine-readable JSON for any subcommand that supports it. |
| `--output {json|text}` | text | Same effect as `--json` when set to `json`. |
| `-e, --evolve` | off | (hidden) Continuous self-improvement mode. |
| `--max-cycles N` | 0 | (hidden) Stop after N evolve cycles. 0 = unlimited. |
| `--budget USD` | 0.0 | Cost cap. 0 = unlimited. |
| `--interval N` | 300 | (hidden) Seconds between evolve cycles. |
| `--github` | off | (hidden) Sync evolve proposals as GitHub Issues. |
| `--headless` | off | (hidden) Run without dashboard (overnight/CI). |
| `--dry-run` | off | Preview the task plan without spawning agents. |
| `-y, --yes` | off | (hidden) Skip cost confirmation prompt. |
| `--fresh` | off | Ignore saved session; start clean. |
| `--plan-only` | off | Show the execution plan without running agents. |
| `--from-plan FILE` | none | Execute a saved plan file (skips interactive planning). |
| `--auto-approve` | off | Skip confirmation prompt before execution. |
| `--approval {auto\|review\|pr}` | auto | Approval gate: merge immediately / pause for review / open GitHub PR. |
| `--merge {pr\|direct}` | pr | Merge strategy: open a PR, or push directly to main. |
| `--cli NAME` | none | Force a specific agent: any adapter from `bernstein adapters list`, or `auto` (overrides auto-detection). |
| `--model NAME` | none | Force a specific model (e.g. `opus`, `sonnet`, `o3`). |
| `--workflow {governed}` | none | Activate governed workflow mode. |
| `-v, --verbose` | off | Show debug-level output. |
| `-q, --quiet` | off | Suppress all non-error output. |
| `-t, --task PATTERN` | none | Run only backlog tasks matching PATTERN. |
| `--auto-pr` | off | Auto-open a GitHub PR when all tasks complete. |
| `--activity-log [PATH]` | off | Write activity to a log file. Default path `.sdd/logs/activity.log`. |

The hidden flags (`--evolve`, `--max-cycles`, `--interval`, `--github`, `--headless`, `--yes`) are visible via `--help-all` and via `bernstein --evolve --help` once you know they exist.

Any global flag may also be set via `bernstein.yaml` (e.g. `budget: 5.00`); the CLI flag wins on conflict.

---

## Commands by category

The commands are organised below by purpose, not alphabetically. Use the table inside each category for quick lookup; the longer per-command entries follow for the highest-traffic commands.

### Conventions

- **Synopsis** lines use `[flags]` where every visible flag is listed in the flag table below it.
- All commands accept the root-level `--json` / `-v` / `-q` flags.
- Hidden subcommands (`task compose`, `task sync`, etc.) are documented in the [Hidden commands](#hidden-commands) section at the end.
- Flags marked `auth` require a logged-in session (`bernstein login`).

---

## Run & control

The "do work" commands. This is where most operators live.

| Command | Purpose | Source |
|---|---|---|
| `bernstein` | Run from `bernstein.yaml` (or inline `-g GOAL`). | `cli/main.py:482` |
| `bernstein run [PLAN.yaml]` | Execute a plan file. | `cli/run_bootstrap.py` (re-exported via `cli/run_cmd.py`) |
| `bernstein start` | Start the server + orchestrator (no goal). | `cli/run_bootstrap.py:start` |
| `bernstein stop` | Graceful stop (agents save work first). | `cli/commands/stop_cmd.py:717` |
| `bernstein cancel TASK_ID` | Cancel a running or queued task. | `cli/commands/task_cmd.py:160` |
| `bernstein cleanup` | Clean worktrees and old logs. | `cli/maintenance_cmd.py:162` |
| `bernstein demo --flask-todo` | Zero-config Flask TODO API demo (`bernstein quickstart` is a deprecated alias, removed in 4.0.0). | `cli/quickstart_cmd.py` |
| `bernstein quickstart` | Deprecated alias of `bernstein demo --flask-todo`; removed in v4.0.0. Keeps its own adapter auto-detection, so it can spend money without `--real`. | `cli/quickstart_cmd.py` |
| `bernstein demo` | 60-second zero-to-running demo. | `cli/run_confirm.py:demo` |
| `bernstein cook` | Run a recipe (multi-stage demo). | `cli/run_confirm.py:cook` |
| `bernstein init` | Initialize project (`.sdd/` + `bernstein.yaml`). | `cli/run_bootstrap.py:394` |
| `bernstein init --wizard` | Interactive project setup (`bernstein init-wizard` is a deprecated alias, removed in 4.0.0). | `cli/init_wizard_cmd.py` |
| `bernstein init-wizard` | Deprecated alias of `bernstein init --wizard`; removed in v4.0.0. | `cli/init_wizard_cmd.py` |
| `bernstein dry-run` | Preview the plan without spawning. | `cli/commands/dry_run_cmd.py:203` |
| `bernstein replay RUN_ID` | Replay a past run step-by-step. | `cli/commands/advanced_cmd.py:876` |
| `bernstein undo` | Undo the last operation. | `cli/undo_cmd.py:15` |
| `bernstein checkpoint` | Save progress for later resume. | `cli/commands/checkpoint_cmd.py:49` |
| `bernstein wrap-up` | End session with summary + learnings. | `cli/wrap_up_cmd.py` |
| `bernstein fork --run ID --from-step N` | Rewind a run to journal step N and branch a new run from its content-addressed worktree snapshot. | `cli/commands/fork_cmd.py` |

#### `bernstein run`

Execute a plan file (or start orchestration with no plan).

**Synopsis:** `bernstein run [PLAN_FILE] [flags]`

The full flag list is large (see `bernstein run --help` and `cli/run_bootstrap.py:533+`). Most commonly used:

| Flag | Default | Meaning |
|---|---|---|
| `PLAN_FILE` | none | A YAML plan to execute. Optional. |
| `--budget USD` | 0.0 | Cost cap. 0 = unlimited. |
| `--max-cost-usd N` | unset | Hard cap on cumulative routed model spend; aborts the run when crossed. Sets `BERNSTEIN_MAX_COST_USD`. |
| `--cli` | auto | Force agent: any registered adapter name (see `bernstein adapters list`) or `auto`. |
| `--model` | none | Force a specific model. |
| `--auto-approve` | off | Skip the interactive plan-approval gate. |
| `--dry-run` | off | Preview without spawning. |
| `--plan-only` | off | Show plan, do not run agents. |
| `--auto-pr` | off | Auto-open a GitHub PR on completion. |
| `--task PATTERN` | none | Run only matching backlog tasks. |
| `--port N` | 8052 | Task server port. |
| `-v / -q` | off | Verbosity. |

The merge strategy is not a `run` flag: set `merge_strategy: pr|direct` in
`bernstein.yaml` (default `pr`).

`--max-cost-usd` is a hard cap, separate from the soft `--budget`
threshold model. It writes the value to `BERNSTEIN_MAX_COST_USD`
before bootstrap; the orchestrator drains live agents and aborts
when cumulative routed spend crosses the threshold. Precedence is
`BERNSTEIN_MAX_COST_USD` > `run_config.json` > `seed.budget_usd`
> default (0 = unlimited). Non-positive values normalise to 0.

**Non-interactive output (pipes, CI).** When stdout is not a terminal the
CLI detaches after bootstrap instead of opening the dashboard. Before
exiting it waits up to ~10 seconds for the first spawn outcome:

- If the first spawn attempt was refused or errored before any work
  started, the failure reason is printed and the command exits `1`.
  Details: `bernstein status` or `.sdd/runtime/retrospective.md`.
- Otherwise the summary is followed by an explicit detach notice; the run
  continues in the background and the command exits `0`. Check progress
  with `bernstein status`.

#### `bernstein stop`

Graceful or force stop.

| Flag | Default | Meaning |
|---|---|---|
| `--force` / `--hard` | off | Hard stop: kill processes immediately. |
| `--timeout SEC` | 30 | Seconds to wait for agents on a soft stop. |

`bernstein stop` (no flag) sends `SIGTERM` to the orchestrator and waits for agents to finish their current step and persist artefacts. `bernstein stop --force` terminates everything immediately and runs orphan-recovery on the next start.

#### `bernstein cancel`

See [`cli/task-lifecycle.md#bernstein-cancel`](cli/task-lifecycle.md#bernstein-cancel).

#### `bernstein cleanup`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root. |
| `--yes` | off | Skip the confirmation prompt. |
| `--force` | off | Also delete agent branches not merged into main (may discard in-flight work). |

#### `bernstein replay`

See [`cli/replay.md`](cli/replay.md) for full reference.

#### `bernstein checkpoint`

| Flag | Default | Meaning |
|---|---|---|
| `--goal TEXT` | none | Goal label embedded in the checkpoint. |

Snapshots `.sdd/` state so a later `bernstein run` can resume from it.

#### `bernstein wrap-up`

End a session with a summary, retrospective, and learning capture. Hides under no flags; useful at the end of a long-running orchestration.

#### `bernstein init` / `bernstein init --wizard`

| Flag | Default | Meaning |
|---|---|---|
| `--dir PATH` | `.` | Directory to initialise. |
| `--wizard` / `-w` | off | Run the interactive setup wizard. |
| `--non-interactive` | off | With `--wizard`: take the wizard's defaults without prompting. Plain `init` never prompts. |
| `--remote` | off | Initialise for a remote container quickstart (e.g. Codespaces); skips local-binary checks. |
| `--add-badge` | off | Insert a shields.io "powered by bernstein" badge into `README.md`. |
| `--badge-variant NAME` | `signed` | Badge wording when `--add-badge` is passed. |

`init-wizard` adds an interactive prompt flow (project type, default agent, budget, etc.) and is preferred for first-time users.

---

## Plan & tasks

| Command | Purpose | Source |
|---|---|---|
| `bernstein plan` | Show the task backlog. | `cli/commands/task_cmd.py:454` |
| `bernstein plan generate "<goal>"` | Generate a plan YAML. | `cli/plan_generate_cmd.py` |
| `bernstein plan compile SPEC` | Compile a spec into a gated task graph with requirement-hash lineage. | `cli/plan_compile_cmd.py` |
| `bernstein plan ls` | List archived plans. | `cli/plan_archive_cmd.py:plan_ls` |
| `bernstein plan show NAME` | Show a stored plan. | `cli/plan_archive_cmd.py:plan_show` |
| `bernstein add-task TITLE` | Create a task on the running server. | `cli/commands/task_cmd.py:37` |
| `bernstein approve TASK_ID` | Approve a pending review. | `cli/commands/task_cmd.py:249` |
| `bernstein reject TASK_ID` | Reject a pending review. | `cli/commands/task_cmd.py:270` |
| `bernstein pending` | List tasks awaiting approval. | `cli/commands/task_cmd.py:291` |
| `bernstein list-tasks` | List tasks with filters. | `cli/commands/task_cmd.py:637` |
| `bernstein tasks` | Alias of `bernstein plan`. | `cli/main.py:706` |
| `bernstein merge` | Merge a completed task's worktree. | `cli/commands/merge_cmd.py:64` |
| `bernstein review` | Trigger queue review or run a review pipeline. | `cli/commands/task_cmd.py:175` |
| `bernstein verify` | Verify WAL integrity, execution determinism, memory provenance, formal properties, or a wheelhouse. | `cli/commands/verify_cmd.py` |
| `bernstein from-ticket FILE` | Generate tasks from a ticket file. | `cli/commands/ticket_cmd.py:231` |
| `bernstein ticket` | Ticket integration group. | `cli/commands/ticket_cmd.py:246` |
| `bernstein plan validate PLAN.yaml` | Validate a plan file's schema (`bernstein validate` is a deprecated alias, removed in 4.0.0). | `cli/plan_validate_cmd.py:142` |
| `bernstein validate PLAN.yaml` | Deprecated alias of `bernstein plan validate`; removed in v4.0.0. | `cli/plan_validate_cmd.py` |
| `bernstein task` | Durable task lifecycle: complete, park, and resume a task. | `cli/commands/task_cmd.py:837` |

#### `bernstein plan`

| Flag | Default | Meaning |
|---|---|---|
| `--export FILE` | none | Write full task list as JSON to FILE. |
| `--status STATUS` | none | Filter: `open / claimed / in_progress / done / failed / blocked / cancelled`. |
| `--graph` | off | Render an ASCII dependency graph. |

The graph view shows the critical path in bold yellow with a star (`★`) and lists bottlenecks at the bottom.

#### `bernstein plan generate`

| Flag | Default | Meaning |
|---|---|---|
| `DESCRIPTION` | required | Goal description (positional). |
| `--output FILE` / `-o` | `plans/<slug>.yaml` | Output path for the YAML plan. |
| `--model NAME` | `anthropic/claude-haiku-4-5` | Model used to draft the plan. |
| `--provider NAME` | `openrouter` | LLM provider (`openrouter`, `openai`, ...). |
| `--workdir PATH` | `.` | Project root directory to analyse. |
| `--dry-run` | off | Print the generated plan without saving to disk. |
| `--enforce-vertical / --no-enforce-vertical` | on | Enforce vertical-slice shape checks on the generated plan. |
| `--max-loc N` | config | Hard LOC cap per slice; overrides `bernstein.yaml [plan].max_loc`. |
| `--max-files N` | config | Max files per slice; overrides `bernstein.yaml [plan].max_files`. |

#### `bernstein plan compile`

Compile a requirements document into a gated task graph. A three-stage
pipeline with at most one model call: draft (structured requirement
extraction), approve (the requirement-set hash is bound into the audit
chain), and compile (a deterministic, model-free transformation to a task
graph). Each task node carries the content hashes of the requirement lines it
implements, so every artefact traces back to spec lines through lineage.

| Flag | Default | Meaning |
|---|---|---|
| `SPEC` | required | Spec / requirements document (positional). |
| `--name NAME` | spec stem | Plan name and output slug under `.sdd/spec/`. |
| `--approve` | off | Record an approval receipt for the requirement set into the audit chain. |
| `--json` | off | Emit a JSON summary instead of a table. |

Artefacts are written to `.sdd/spec/<name>/` (`requirements.json`,
`graph.json`, and, with `--approve`, `receipt.json`). The same approved
requirement set always compiles to a byte-identical graph, so `graph_hash` is
reproducible; editing one requirement re-plans only the affected node while
every other node keeps its content-addressed identity.

#### `bernstein add-task`

See [`cli/task-lifecycle.md#bernstein-add-task`](cli/task-lifecycle.md#bernstein-add-task).

#### `bernstein review`

See [`cli/task-lifecycle.md#bernstein-review-bernstein-verify`](cli/task-lifecycle.md#bernstein-review-bernstein-verify).

#### `bernstein task`

A group, with four subcommands. A task that must wait on something outside
the run - a mid-flight approval, an external review, a credential rotation, a
dependency landing - can be *parked* rather than left holding its resources:
the park writes an attested receipt, releases the seat, sandbox and budget
headroom, and `resume` restores from that receipt.

| Subcommand | Purpose | Source |
|---|---|---|
| `complete TASK_ID` | Mark a task complete on the running task server. | `cli/commands/task_cmd.py:861` |
| `suspend TASK_ID` | Park a running task and free its seat, sandbox and budget. | `cli/commands/task_cmd.py:918` |
| `resume TASK_ID` | Resume a parked task from its attested suspend receipt. | `cli/commands/task_cmd.py:1035` |
| `list-suspended` | List parked tasks with their parked-at hash and freed resources. | `cli/commands/task_cmd.py:1154` |

The group itself takes no flags beyond `--help`; each subcommand carries its
own, below.

##### `bernstein task complete`

Resolves the task-server URL and the session token itself, from
`BERNSTEIN_SERVER_URL` / `.sdd/runtime/server.port` and `BERNSTEIN_AUTH_TOKEN`
/ the persisted run-token file, so a completion does not have to be
hand-assembled as a request with a bearer header.

| Flag | Default | Meaning |
|---|---|---|
| `--summary TEXT` / `-s` | required | Result summary recorded on the task (max 2000 chars). |
| `--json` | off | Print the server's task payload as JSON. |

##### `bernstein task suspend`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir PATH` | `.` | Project root (the parent of `.sdd/`). |
| `--adapter TEXT` | latest checkpoint | Adapter owning the session. |
| `--session-id TEXT` | latest checkpoint | Native session id. |
| `--worktree TEXT` | checkpoint worktree, else cwd | Worktree to hash. |
| `--envelope TEXT` | `subscription` | Quota envelope whose headroom is freed. |
| `--reserved-usd FLOAT` | none | Envelope headroom reserved for the task. |
| `--spent-usd FLOAT` | none | Spend recorded against the reservation at park time. |
| `--until approval` | off | Resume only once `bernstein approve TASK_ID` lands. |
| `--role TEXT` | the task's recorded role | Role the checkpoint's grant is bound to. |
| `--parent-run-id TEXT` | `$BERNSTEIN_RUN_ID` | Run that owns the task, bound into the grant. |
| `--json` | off | Print the park result as JSON. |

##### `bernstein task resume`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir PATH` | `.` | Project root (the parent of `.sdd/`). |
| `--worktree TEXT` | the parked path | Re-materialized worktree to hash. |
| `--mode [warm\|fork\|cold]` | `warm` | Requested continuation mode; downgraded, never upgraded, on drift. |
| `--json` | off | Print the resume result as JSON. |

##### `bernstein task list-suspended`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir PATH` | `.` | Project root (the parent of `.sdd/`). |
| `--json` | off | Print the parked tasks as JSON. |

---

## Status & monitoring

| Command | Purpose | Source |
|---|---|---|
| `bernstein status` | Task summary + agent health. | `cli/commands/status_cmd.py:147` |
| `bernstein live` | Interactive Textual TUI dashboard. | `cli/commands/advanced_cmd.py:47` |
| `bernstein dashboard` | Open the web dashboard. | `cli/commands/advanced_cmd.py:180` |
| `bernstein ps` | Running agent processes. | `cli/commands/status_cmd.py:241` |
| `bernstein watch` | Stream task events. | `cli/watch_cmd.py:252` |
| `bernstein logs` | Tail agent logs (group). | `cli/logs_group_cmd.py:45` |
| `bernstein recap` | Post-run summary. | `cli/commands/advanced_cmd.py:558` |
| `bernstein retro` | Detailed retrospective. | `cli/commands/advanced_cmd.py:299` |
| `bernstein wrap-up` | End-of-session summary. | `cli/wrap_up_cmd.py` |
| `bernstein history` | Show run history. | `cli/maintenance_cmd.py:history_cmd` |
| `bernstein report commits` | Per-run git diff stats. | `cli/commands/status_cmd.py:1232` |
| `bernstein report` | Build a custom report (group). | `cli/report_cmd.py` |
| `bernstein slo` | SLO dashboard. | `cli/slo_cmd.py:191` |
| `bernstein trace TASK_ID` | Step-by-step trace. | `cli/commands/advanced_cmd.py:666` |
| `bernstein report incident` | Open an incident report. | `cli/commands/incident_cmd.py:53` |
| `bernstein report postmortem` | Failed-task postmortem. | `cli/commands/postmortem_cmd.py:12` |

#### `bernstein status`

Compact one-screen project view.

| Flag | Default | Meaning |
|---|---|---|
| `--json` | off | Emit JSON. |
| `--mode {novice\|standard\|expert}` | persisted or `standard` | Dashboard detail level. |
| `--no-color` | off | Disable colour output. |

#### `bernstein live`

| Flag | Default | Meaning |
|---|---|---|
| `--interval SEC` | 2.0 | Polling interval. |
| `--classic` | off | Use the simpler Rich Live display. |
| `--no-splash` | off | Skip the startup splash. |

The default is the 3-column Textual TUI: Agents | Tasks | Activity feed. `--classic` falls back to a single-pane Rich Live view.

Both views resolve the task server the same way as the rest of the CLI: `BERNSTEIN_SERVER_URL`, then the port the running orchestrator persisted in `.sdd/runtime/server.port`, then `http://localhost:8052`. When the poll cannot reach that server the header says `No connection to <url>` rather than drawing empty panels, which would be indistinguishable from an orchestrator with nothing to do. That state means *every* read failed: one route erroring while the others answer is a broken route, not a dead server, so the dashboard keeps rendering the panels that did load.

The run token the orchestrator persists under `.sdd/runtime` is only ever sent to a loopback address: it is a credential this machine minted for its own run, and `BERNSTEIN_SERVER_URL` can name any host. A token you set in `BERNSTEIN_AUTH_TOKEN` yourself goes wherever you point the dashboard.

#### `bernstein dashboard`

| Flag | Default | Meaning |
|---|---|---|
| `--port N` | 8052 | Server port. |
| `--no-open` | off | Do not open the browser. |

#### `bernstein logs`

A subcommand group; defaults to `bernstein logs tail`.

| Subcommand | Flags | Purpose |
|---|---|---|
| `tail` | `--follow / -f`, `--agent / -a ID`, `--lines / -n N`, `--runtime-dir DIR` | Tail the most recent agent log. |
| `search QUERY` | `--time-range`, `--agent-role` | Search logs across all agent sessions and the orchestrator. |

`bernstein logs` (no subcommand) is equivalent to `bernstein logs tail`.

#### `bernstein recap`

| Flag | Default | Meaning |
|---|---|---|
| `--archive PATH` | `.sdd/archive/tasks.jsonl` | Path to task archive. |
| `--since DURATION` | none | Build the report from workspace files instead of the task server (`45m`, `6h`, `2d`; a bare number is minutes). |
| `--workdir PATH` | `.` | Project root, used with `--since`. |
| `--as-json` | off | Emit raw JSON. |

With `--since` the summary is the "since you were away" report: completed and
failed tasks, provider errors, and estimated cost read from `.sdd/` alone, so
it still answers what happened after the run and its server have exited.

#### `bernstein retro`

| Flag | Default | Meaning |
|---|---|---|
| `--since HOURS` | all | Hours back to include. |
| `--output FILE` / `-o` | `.sdd/runtime/retrospective.md` | Output path. |
| `--print` | off | Also print to stdout. |
| `--archive PATH` | `.sdd/archive/tasks.jsonl` | Source archive. |

#### `bernstein watch`

| Flag | Default | Meaning |
|---|---|---|
| `DIRECTORY` | `.` | Directory to watch (positional). |
| `--glob PATTERN` | none | Restrict watching to files matching PATTERN (e.g. `src/**/*.py`). |

#### `bernstein trace`

Group: inspect, serve, and verify local agent traces.

| Flag | Default | Meaning |
|---|---|---|
| `--traces-dir DIR` | `.sdd/traces` | Directory containing trace files. |

| Subcommand | Purpose |
|---|---|
| `show TASK_ID` | Step-by-step execution trace for a task; `--as-json` emits raw JSON. |
| `verify TRACE_ID` | Confirm the on-disk bytes match the indexed sha256. |
| `reindex` | Rebuild `.sdd/traces/index.jsonl` from the on-disk blob tree. |
| `serve` | Read-only FastAPI viewer over the local content-addressed store (`--port`, `--bind`). |
| `project RUN_ID` | Project the run's event journal into a signed OTel span set. |
| `verify-projection RUN_ID` | Recompute span ids from the journal and verify the signature. |

Subcommands `project RUN_ID` and `verify-projection RUN_ID` emit and verify a
signed OTel GenAI span set projected from the run event journal. Span ids are
derived from journal entry hashes (byte-identical across replays), each span
carries `bernstein.journal.entry_hash`, and the set is signed with the install
identity. `--no-genai-stability` omits the Development-stage GenAI convention
attributes while keeping the ids journal-anchored; the local
`.sdd/runs/<run_id>/projection.otel.json` store emits even with no OTLP endpoint
set. (`cli/commands/advanced_cmd.py`, `core/observability/otel_projection.py`.)

#### `bernstein slo`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root. |
| `--json` | off | Emit raw JSON. |
| `--watch` | off | Refresh every `--interval` seconds until interrupted. |
| `--interval SEC` | 30 | Refresh interval in `--watch` mode. |
| `--compact` | off | Compact output without sparkline. |

---

## Quality & autofix

| Command | Purpose | Source |
|---|---|---|
| `bernstein verify` | Verify WAL integrity, execution determinism, memory provenance, formal properties, or a wheelhouse. | `cli/commands/verify_cmd.py` |
| `bernstein autofix` | Auto-repair CI failures (group). | `cli/commands/autofix_cmd.py:172` |
| `bernstein ci` | CI integration commands (group). | `cli/commands/ci_cmd.py:49` |
| `bernstein chaos` | Chaos engineering (group). | `cli/commands/chaos_cmd.py:33` |
| `bernstein eval` | Evaluation pipelines (group). | `cli/commands/eval_benchmark_cmd.py:426` |
| `bernstein benchmark` | Benchmark pipelines (group). Deprecated alias until v4.0.0: use `bernstein eval`. | `cli/commands/eval_benchmark_cmd.py:29` |
| `bernstein impact` | Change-impact analysis (group): API compatibility, caller sites, blast radius. | `cli/commands/impact_cmd.py:23` |
| `bernstein api-check` | Detect breaking-API changes. Second spelling of `bernstein impact api`. | `cli/commands/api_check_cmd.py:22` |
| `bernstein dep-impact` | Deprecated alias of `bernstein impact deps`; removed in v4.0.0. | `cli/commands/impact_cmd.py:39` |
| `bernstein blast-radius` | Deprecated alias of `bernstein impact blast`; removed in v4.0.0. | `cli/commands/impact_cmd.py:57` |
| `bernstein diff` | Task-state diff. | `cli/diff_cmd.py:504` |

#### `bernstein verify`

Group: verifies integrity and reproducibility artefacts. It does not run lint / test / type-check quality gates; use `bernstein test` and the project's configured quality gates for that.

| Subcommand | Purpose |
|---|---|
| `run RUN_ID` | Build the signed run receipt for a run (`--workdir`, `--output`, signing-key options). |
| `receipt RECEIPT_PATH` | Verify a run receipt offline (`--public-key`). |
| `ladder RECEIPT_HASH` | Re-derive and verify a verifier-ladder receipt (`--workdir`). |
| `legacy [WHEELHOUSE_PATH]` | The pre-receipt checks: `--wal-integrity RUN_ID`, `--determinism RUN_ID` (gated by `--expect` / `--baseline`), `--memory-audit`, `--formal TASK_ID`, or a positional wheelhouse path for air-gap signature verification. |

#### `bernstein autofix`

| Subcommand | Purpose |
|---|---|
| `start` | Start the autofix daemon (watches PRs, repairs CI failures). `--repo`, `--config`, `--foreground`, `--once`. |
| `stop` | Stop the daemon. `--timeout`. |
| `status` | Show daemon status + recent activity. `--watch`, `--json`, `--limit`. |
| `attach` | Attach to the running daemon's activity feed. `--limit`. |
| `ladder` | Single-shot escalation-ladder run against one failing PR. `--pr`, `--repo`, `--dry-run`. |
| `review` | Respond to review findings on a PR. `--pr`, `--repo`, `--poll-seconds`, `--once`. |
| `review-register` / `review-resolve` | Register / resolve review-finding state. |

See `cli/commands/autofix_cmd.py:172+` for the full flag list.

#### `bernstein ci`

| Subcommand | Purpose |
|---|---|
| `fix` | One-shot fix of a specific failing GitHub Actions run. |
| `watch REPO` | Watch a repo for CI failures and auto-create fix tasks. |

Common flags: `--token` (env: `GITHUB_TOKEN`), `--server`, `--interval`. (`cli/commands/ci_cmd.py:49+`.)

#### `bernstein chaos`

| Subcommand | Purpose |
|---|---|
| `agent-kill` | Kill a random or specific agent. |
| `file-remove` | Delete files matching a glob. |
| `status` | Show recent chaos events. |
| `slo` | SLO impact of recent chaos events. |

`agent-kill` accepts `--agent-id`, `file-remove` accepts `--pattern`, and `status` accepts `--limit`. (`cli/commands/chaos_cmd.py:33+`.)

#### `bernstein eval`

Group: evaluation pipelines. The subcommands carry the flags; the group itself
only accepts the reliability options listed below.

| Subcommand | Purpose |
|---|---|
| `run SPEC` | Drive the golden harness or a YAML eval spec (`--tier`, `--compare`, `--save/--no-save`, `--output`). |
| `swe-bench` | SWE-bench runner (`--subset`, `--sample`, `--instance`, `--dataset`, `--save/--no-save`). |
| `programbench` | ProgramBench runner (`--adapter`, `--subset`, `--tasks`, `--task`, `--dataset`, `--out`). |
| `simulate` | Replay the standard benchmark task set (`--tasks-dir`, `--seed`, `--task-id`, `--baseline`). |
| `compare` | Compare eval runs (`--tasks-dir`, `--mode`). |
| `golden` | Run the curated golden suite (`--workdir`). |
| `gate` / `gate-verify` | Statistical promotion gate and its receipt verification. |
| `receipt` | Emit / verify eval receipts. |

`bernstein eval run` is the typical command for SWE-bench-style evaluations; `bernstein eval swe-bench` and `bernstein eval golden` cover the harness and the curated golden suite. See `cli/commands/eval_benchmark_cmd.py:127+` and `:426+`.

`bernstein eval` additionally accepts group-level reliability options — a pass^k alias for `bench run --reliability` that cannot be combined with an eval subcommand:

| Flag | Default | Meaning |
|---|---|---|
| `--reliability K` | none | Run each suite task K times under fixed coordination; emit a signed pass^k reliability receipt. |
| `--suite NAME` | `golden-v1` | Suite name or `.json` path (`--reliability` mode only). |
| `--out PATH` | `reliability.json` | Output path for the reliability receipt. |
| `--scheduler NAME` | `default` | Scheduler name embedded in the receipt. |
| `--stub-signer` | off | Stub signer instead of the install identity (testing). |

Verification stays on `bernstein bench reliability-verify` / `bernstein bench reliability-check`. (`cli/commands/eval_benchmark_cmd.py:800+`.)

#### `bernstein benchmark` (deprecated)

`bernstein benchmark` is a deprecated alias for `bernstein eval` and prints a warning on stderr before running; it keeps every subcommand it carried and is removed in v4.0.0. The group itself takes no flags — including the reliability options above, which are `eval`-only. Every subcommand name is reachable under `eval` before the removal:

| Deprecated | Canonical | Notes |
| --- | --- | --- |
| `benchmark run` | `eval run` | **Different command.** `eval run` drives the golden harness or a YAML eval spec (positional `SPEC`, `--output`, `--compare`, tiers `smoke/standard/stretch/adversarial`); `benchmark run` drives the evolution benchmark tree (`--benchmarks-dir`, tiers `smoke/capability/stretch`). `--benchmarks-dir` has **no `eval` equivalent**. |
| `benchmark swe-bench` | `eval swe-bench` | Same runner and same options; `benchmark swe-bench --lite` is itself a deprecated alias, so migrate it to `eval swe-bench --subset lite`. |
| `benchmark programbench` | `eval programbench` | Same command object. |
| `benchmark compare` | `eval compare` | Same command object. |
| `benchmark simulate` | `eval simulate` | Same command object. |
| `benchmark receipt emit/verify` | `eval receipt emit/verify` | Same command object. |

One capability does not survive the rename: `bernstein benchmark run --benchmarks-dir DIR` runs the evolution benchmark tree and `eval run` cannot. Until that option is ported onto an `eval` command, `bernstein benchmark run` is the only spelling for it, and v4.0.0 should not unregister the alias without porting it first. `tests/unit/test_fold_benchmark_subcommands.py` declares the list of alias-only options and fails if it widens or if a declared replacement stops being accepted, so this note cannot silently go stale.

`eval simulate` is **not** the top-level `bernstein simulate`. The top-level command is a digital-twin simulation of a plan against historical traces (`--plan`, `--from-traces`, `--traces-dir`); `eval simulate` replays the standard benchmark task set for throughput, cost and quality (`--tasks-dir`, `--task-id`, `--baseline`). They share a verb and no options, and the top-level command is unaffected by this change. `bernstein bench` is a separate command and is not folded in.

#### `bernstein impact`

Change-impact analysis for a working tree. Three subcommands, each answering a
different question about the same change.

```bash
bernstein impact api --base main     # do the changed files break their own signatures?
bernstein impact deps --base main    # which callers elsewhere in the repo break?
bernstein impact blast score --file src/db/migrate.py   # how irreversible is it?
```

#### `bernstein impact api`

Compares Python function signatures between the working tree and a base ref.
Exits 1 when breaking changes are found.

| Flag | Default | Meaning |
|---|---|---|
| `--base REF` | `HEAD~1` | Git ref to compare the working tree against. |
| `--workdir PATH` | current directory | Repository to inspect. |

#### `bernstein impact deps`

Finds every call site in the repository that the changed signatures break.

| Flag | Default | Meaning |
|---|---|---|
| `--base REF` | `HEAD~1` | Git ref to compare the working tree against. |
| `--workdir PATH` | current directory | Repository to inspect. |
| `--strict` | off | Exit 1 on any call-site impact, even without an API break. |
| `--json` | off | Emit the report as JSON on stdout. |

#### `bernstein impact blast`

Group. `score` scores a change described on the command line; `show TASK_ID`
pretty-prints a saved report from `.sdd/metrics/blast_radius/`.

#### `bernstein api-check`

Second spelling of `bernstein impact api`. Same flags, same exit codes.

| Flag | Default | Meaning |
|---|---|---|
| `--base REF` | `HEAD~1` | Git ref to compare the working tree against. |
| `--workdir PATH` | current directory | Repository to inspect. |

#### `bernstein dep-impact`

Deprecated alias of `bernstein impact deps`, kept registered through the 3.x
line and removed in v4.0.0. Prints a deprecation notice to stderr, then runs
`bernstein impact deps` with the arguments it was given, so `--json` output on
stdout stays parseable.

| Flag | Default | Meaning |
|---|---|---|
| `--base REF` | `HEAD~1` | Git ref to compare the working tree against. |
| `--workdir PATH` | current directory | Repository to inspect. |
| `--strict` | off | Exit 1 on any call-site impact, even without an API break. |
| `--json` | off | Emit the report as JSON on stdout. |

#### `bernstein blast-radius`

Deprecated alias of `bernstein impact blast`, kept registered through the 3.x
line and removed in v4.0.0. Exposes the same `score` and `show` subcommands and
prints a deprecation notice to stderr before running them.

#### `bernstein diff`

Show the diff an agent produced for a task, resolved from its live worktree,
its merged branch, or the merge commit.

| Flag | Default | Meaning |
|---|---|---|
| `TASK_ID` | required unless `--compare` | Task whose agent diff to show. |
| `--base REV` | `main` | Base branch to diff against. |
| `--workdir PATH` | `.` | Project root. |
| `--stat` | off | Show the `--stat` summary only. |
| `--raw` | off | Print the raw diff without syntax highlighting. |
| `--fold` | off | Collapse each hunk to its header plus a few lines. |
| `--fold-lines N` | 3 | Lines kept per hunk when `--fold` is set. |
| `--word-diff` | off | Highlight only the tokens that changed on replaced lines. |
| `--compare A B` | none | Side-by-side comparison of two agents' work. |

---

## Adapters & agents

| Command | Purpose | Source |
|---|---|---|
| `bernstein agents` | Agent catalog ops (group). | `cli/commands/agents_cmd.py:22` |
| `bernstein test-adapter` | Spawn one adapter to verify its plumbing. | `cli/adapter_cmd.py:84` |
| `bernstein worker` | Join a cluster as a remote worker node. | `cli/commands/worker_cmd.py` |
| `bernstein evolve` | Self-improvement loop. | `cli/evolve_cmd.py:48` |

#### `bernstein agents`

| Subcommand | Purpose |
|---|---|
| `list` | Available agents and capabilities (`--source`, `--identities`). |
| `sync` | Pull the latest agent catalog. |
| `validate` | Validate the local catalog. |
| `showcase` | Print example invocations for each agent. |
| `match` | `--role X` `--task TEXT` - show which agent best matches. |
| `sandbox-backends` | List available sandbox backends. |
| `discover` | Auto-detect installed CLI agents. `--net` also searches GitHub/npm. |
| `trust` | Per-agent trust tiers from task outcomes (`--agent ID` adds the tier's permission profile, `--as-json` for machine output). |

#### `bernstein test-adapter`

| Flag | Default | Meaning |
|---|---|---|
| `--adapter NAME` | required | Adapter to test (e.g. `gemini`, `codex`). |
| `--task TEXT` | required | Task for the adapter to execute. |
| `--model NAME` | adapter default | Model to use for the smoke run. |
| `--timeout SEC` | 120 | Wait up to N seconds for exit. |

#### `bernstein worker`

| Flag | Default | Meaning |
|---|---|---|
| `--server URL` | required | Central Bernstein task server URL (e.g. `http://central:8052`). |
| `--token TOKEN` | none | Bearer token for cluster auth. |
| `--name NAME` | hostname | Worker node name. |
| `--slots N` | 6 | Max concurrent agents on this worker. |
| `--roles LIST` | `backend,qa,security,frontend` | Comma-separated roles this worker accepts. |
| `--label K=V` | none | Node labels (repeatable: `--label gpu=true --label region=us-east`). |
| `--adapter NAME` | auto-detect | CLI agent adapter. |
| `--model NAME` | adapter default | Default model for tasks that carry no explicit model. |
| `--poll-interval SEC` | 10 | Seconds between task polling cycles. |
| `--poll-interval-ms MS` | none | Milliseconds between polling cycles (overrides `--poll-interval`). |
| `--heartbeat-interval-ms MS` | 15000 | Milliseconds between heartbeats to the central server. |
| `--pool NAME` | none | Named sandbox pool to enrol into (signs an Ed25519 enrolment receipt). |
| `--pool-hash HASH` | none | Explicit pool hash to enrol against (overrides `--pool` name resolution). |

See [`operations/cluster-mode.md`](../operations/cluster-mode.md) for the full setup walkthrough.

#### `bernstein evolve`

> **Preview:** `bernstein evolve run` is not a zero-workspace first-run path. In a clean directory it exits before starting the evolution loop because `.sdd/` is missing. Initialise a Bernstein workspace first, then run the command from that workspace.

Group: self-evolution proposals and their review lifecycle.

| Subcommand | Purpose |
|---|---|
| `run` | Run evolution cycles (`--window`, `--max-proposals`, `--cycle`, `--dir`, `--github`, `--github-repo`); reads evolve config from `bernstein.yaml` for flags not set. |
| `review` | Show upgrade proposals pending human review (`--dir`). |
| `approve PROPOSAL_ID` | Approve an upgrade proposal (`--reviewer`, `--dir`). |
| `export OUTPUT` | Export a static evolution report, HTML or Markdown (`--format`, `--dir`). |
| `status` | Show evolution history (`--dir`). |

---

## Plugins & skills

| Command | Purpose | Source |
|---|---|---|
| `bernstein plugins` | List installed plugins. | `cli/commands/advanced_cmd.py:488` |
| `bernstein skills` | Skill packs (group). | `cli/commands/skills_cmd.py:13` |
| `bernstein prompts` | Prompt-template management (group). | `cli/commands/prompts_cmd.py:36` |
| `bernstein manifest` | Manifest mgmt (group). | `cli/commands/manifest_cmd.py:18` |
| `bernstein templates` | Project template mgmt (group). | `cli/commands/templates_cmd.py:41` |
| `bernstein skill` | Skill usage provenance (group): install receipts + provenance graph. | `cli/commands/skill_cmd.py:1` |
| `bernstein security-review` | Pattern-scan a diff for security issues. | `cli/commands/security_review_cmd.py` |

#### `bernstein plugins`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root. |
| `--trust-details` | off | Print the full trust-signal breakdown for each low-trust plugin. |

Lists plugins in `.bernstein/plugins/<name>/meta.json` with a trust tier and
score derived from the plugin's own signals - signature file, packaging
metadata, README, tests - so an unreviewed plugin is visible before it loads.

#### `bernstein skills`

| Subcommand | Purpose |
|---|---|
| `list` | List every discoverable skill with a one-line description. `--layered` shows the base/team/user view. |
| `show NAME` | Print a skill's `SKILL.md` body. `--reference FILE` / `--per-layer` to inspect a specific reference or the per-layer diff. |
| `install NAME` | Install a skill from a local path. |
| `remove NAME` | Remove a previously installed skill. |

The `skills catalog ...` subgroup browses, installs, searches, and upgrades skill packs from the registry. Other subcommands include `bench`, `diff`, `lint`, `test`, `sync`, and `watch`. (`cli/commands/skills_cmd.py`.)

#### `bernstein skill`

Usage-attestation surface for installed skills. Each catalog install anchors a
lineage receipt in the run's Merkle+HMAC spine; provenance recomputes usage
from verified journal heads rather than a stored counter.

| Subcommand | Purpose |
|---|---|
| `provenance SKILL` | Print the verified runs and artifacts a skill contributed to; the verified-run count is recomputed from journal heads on every call. |
| `verify SKILL` | Recompute the install receipt and flag a manifest-hash drift between the receipt and the currently installed content. |

`SKILL` is a catalog entry id (resolved via `skills.lock`) or a raw content
digest. (`cli/commands/skill_cmd.py`.)

#### `bernstein prompts`

| Subcommand | Purpose |
|---|---|
| `list` | List all versioned prompts and their active versions. |
| `show NAME` | Show all versions of a prompt with their metrics. |
| `compare NAME V1 V2` | Compare metrics between two prompt versions. |
| `promote NAME VERSION` | Promote a specific version to active. |
| `ab-start NAME A B` | Start an A/B test between two prompt versions. |
| `ab-stop NAME` | Stop an active A/B test without promoting either version. |
| `seed` | Seed `.sdd/prompts/` from `templates/prompts/` as v1. |

#### `bernstein manifest`

| Subcommand | Purpose |
|---|---|
| `list` | List all available run manifests. |
| `show RUN_ID` | Show the manifest for a run. |
| `diff RUN_A RUN_B` | Compare two run configurations and highlight differences. |

#### `bernstein templates`

| Subcommand | Purpose |
|---|---|
| `list` | List available templates. |
| `show TEMPLATE [OUTPUT]` | Print template content (or write to OUTPUT). |
| `use TEMPLATE [OUTPUT]` | Copy TEMPLATE to OUTPUT (default `plans/<name>.yaml`). |
| `compress ROLE\|--all` | Operator-gated LLM compression of role prompt templates (`cli/commands/templates_cmd.py`). Rewrite via the configured adapter (`--model`, `--provider`), then mechanical validators (fenced blocks, headings, URLs, inline code, placeholders, completion-contract block; at most two targeted fix retries), then apply. Originals are stored under `~/.local/share/bernstein/template-backups/` keyed by content hash with readback verification; the receipt `{role, pre_sha256, post_sha256, pre_tokens, post_tokens, validators, adapter, model}` is chained to the audit log and a `templates.lock` row lets `bernstein team drift` classify the change as intentional. Prints only the template token delta; per-spawn savings come from `bernstein cost` grouped by role. `--workdir DIR`, `--yes` skips the confirmation. |
| `restore ROLE` | Reverse the most recent receipted compression byte-identically (backup hash, on-disk hash, and directory digest all verified). `--workdir DIR`. |
| `hooks list` / `hooks use` | Browse and scaffold bundled command-hook templates. |

---

## Cloud & cluster

| Command | Purpose | Source |
|---|---|---|
| `bernstein cloud` | Cloudflare cloud agent ops (group). | `cli/commands/cloud_cmd.py:35` |
| `bernstein worker` | Join a cluster as worker (see [Adapters & agents](#adapters-agents)). | `cli/commands/worker_cmd.py` |
| `bernstein gateway` | Gateway mgmt (group). | `cli/commands/gateway_cmd.py:28` |
| `bernstein tunnel` | Tunnel mgmt (group). | `cli/commands/tunnel_cmd.py:62` |
| `bernstein remote` | Remote-host execution (group). | `cli/commands/remote_cmd.py:52` |
| `bernstein connect` | Connect to a remote Bernstein server. | `cli/commands/creds_cmd.py:95` |
| `bernstein fleet` | Multi-project supervision (group). | `cli/commands/fleet_cmd.py:50` |

#### `bernstein cloud`

| Subcommand | Purpose |
|---|---|
| `login` | Authenticate with Bernstein Cloud. |
| `logout` | Remove stored cloud credentials. |
| `run GOAL` | Run an agent on Cloudflare Workers. `--max-agents N`, `--model`, `--budget USD`, `--wait/--no-wait`. |
| `status [RUN_ID]` | Status of a cloud run. |
| `runs` | Recent cloud runs. `--limit N`, `--json`. |
| `cost` | Cloud usage and spend. |
| `init` | Generate `wrangler.toml` and the worker entry point. `--worker-name`, `-o FILE`. Deploy afterwards with `npx wrangler deploy`. |

(`cli/commands/cloud_cmd.py:35+`.)

#### `bernstein gateway`

| Subcommand | Purpose |
|---|---|
| `start` | Start the MCP gateway proxy. |
| `replay` | Replay recorded MCP tool calls from a previous gateway run. |

#### `bernstein tunnel`

| Subcommand | Purpose |
|---|---|
| `start` | Start a tunnel exposing `localhost:<PORT>` publicly. `--name NAME`, `--provider {cloudflared\|ngrok\|bore\|tailscale}`. |
| `list` | List active tunnels. |
| `stop` | Stop a named tunnel or (with `--all`) every active tunnel. |

(`cli/commands/tunnel_cmd.py:62-117`.)

#### `bernstein remote`

| Subcommand | Purpose |
|---|---|
| `run HOST` | Invoke `bernstein run PATH` against HOST over SSH. `--user`, `--port`, `--identity-file`, `--remote-path`. |
| `test HOST` | Check that HOST is reachable and time the round trip. |
| `forget HOST` | Remove any cached ControlMaster sockets for HOST. |

(`cli/commands/remote_cmd.py:52-200`.)

#### `bernstein connect`

| Flag | Default | Meaning |
|---|---|---|
| `PROVIDER` | required | Provider ID (e.g. `bernstein-cloud`). |
| Various `--*` | - | Provider-specific (see `cli/commands/creds_cmd.py:95-200`). |

#### `bernstein fleet`

Multi-project dashboard.

| Subcommand | Purpose |
|---|---|
| `list` | List instances discovered under the fleet root. |
| `ls` | List configured projects without launching the dashboard. |
| `reload` | Rescan the fleet root and report what would be picked up. |
| `bulk-cost-report` | Run `bernstein cost` against every matching project. |
| `bulk-pause` / `bulk-resume` / `bulk-stop` | Pause, resume, or stop every matching project. |

The group also accepts `--web [host:]port` to run the web view instead of the TUI. (`cli/commands/fleet_cmd.py:50+`.)

---

## Auth & security

| Command | Purpose | Source |
|---|---|---|
| `bernstein login` | Log in (alias for `auth login`). | `cli/commands/auth_cmd.py:auth_login` |
| `bernstein auth` | Auth ops (group). | `cli/commands/auth_cmd.py:139` |
| `bernstein creds` | Credential mgmt (group). | `cli/commands/creds_cmd.py:214` |
| `bernstein policy` | Policy mgmt (group). | `cli/commands/policy_cmd.py:12` |
| `bernstein compliance` | Compliance reports (group). | `cli/commands/compliance_cmd.py:26` |
| `bernstein audit` | Audit-log ops (group). | `cli/commands/audit_cmd.py:25` |
| `bernstein identity` | Install-identity ops (group): fingerprint helpers plus `keydir`. | `cli/commands/identity_cmd.py:identity_group` |
| `bernstein delegation` | Delegation-receipt verification (group). | `cli/commands/delegation_cmd.py:delegation_group` |
| `bernstein lineage` | Artifact-provenance lineage-spine ops (group). | `cli/commands/lineage_cmd.py` |
| `bernstein credential` | C2PA content credentials projected from the lineage spine (group). | `cli/commands/credential_cmd.py` |
| `bernstein mandate` | Verifiable spending mandates as journal-anchored consent receipts (group): `emit` / `verify` / `revoke`. | `cli/commands/mandate_cmd.py` |
| `bernstein compaction` | Compaction receipt-chain ops (group). | `cli/commands/compaction_cmd.py:32` |
| `bernstein quarantine` | Quarantined-task ops (group). | `cli/commands/advanced_cmd.py:1120` |
| `bernstein approve-tool` | Approve a tool-call request (alias; flag form `approve --tool <id>`). | `cli/commands/approval_cmd.py:approve_tool_cmd` |
| `bernstein reject-tool` | Reject a tool-call request (alias; flag form `reject --tool <id>`). | `cli/commands/approval_cmd.py:reject_tool_cmd` |
| `bernstein review-receipt` | Attested PR review receipts binding issue / plan / tool calls / diff (group): `emit` / `verify`. | `cli/commands/review_receipt_cmd.py` |
| `bernstein receipt` | Result receipt bundles binding a worker submission's patch / gate logs / task ref / sandbox selection into one DSSE-signed envelope, verifiable offline (group): `create` / `verify`. | `cli/commands/receipt_cmd.py` |
| `bernstein volunteer` | Volunteer-worker surfaces for opt-in projects (group): `verify` validates a project's `.bernstein/volunteer.json` through the same loader a donor's worker uses and prints the manifest digest a receipt binds to as `manifest_sha256`. | `cli/commands/volunteer_cmd.py` |
| `bernstein gate verify <run>` | Verify a maker-checker / judge-panel gate's signed adjudication record: recompute `inputs_hash` from `--inputs` and confirm the panel saw exactly those inputs, then confirm the spine anchor still verifies. Exit 1 when no record, 2 on mismatch. | `cli/commands/gate_cmd.py` |
| `bernstein governance verify <run>` | Recompute every RBAC access and per-subject budget decision recorded for a run from the signed spine and confirm the recorded verdicts: re-resolve roles from the signed `--bindings`, re-project spend from the `--ledger`, and match. Exit 1 when no records, 2 on mismatch. | `cli/commands/governance_cmd.py` |

> Task-level `approve` / `reject` are different commands - see [Plan & tasks](#plan-tasks). Both also accept `--tool <id>` to resolve tool-call approvals (the flag form of `approve-tool` / `reject-tool`).

#### `bernstein identity`

| Subcommand | Purpose |
|---|---|
| `show` | Print the install-rev fingerprint token. |
| `decode TOKEN` | Confirm a token came from a real install (shape + sentinel check). |
| `verify TOKEN [--nonce HEX]` | Full HMAC-strength verify when the operator holds the install nonce. |
| `keydir` | Print the install-identity key directory (JWKS) used to verify outbound HTTP Message Signatures. Mirrors `/.well-known/http-message-signatures-directory`. |
| `disable` | Print the env line that suppresses every fingerprint emit site. |

Outbound agent-facing requests (A2A card fetch, browser/research rendering)
carry an RFC 9421 Ed25519 signature keyed to the install-identity thumbprint.
`BERNSTEIN_HTTP_SIGNING_REQUIRED=1` turns an unsigned outbound path into a hard
error. `BERNSTEIN_AGENT_CARD_KEY_DIR` overrides the key directory location.

#### `bernstein delegation`

| Subcommand | Purpose |
|---|---|
| `verify RUN [--root DIR] [--json]` | Reconstruct the `principal -> orchestrator -> sub-agent` chain for a run from HMAC-chained per-hop receipts and confirm it is intact; exits non-zero on tamper, deleted hop, or a missing chain. |

#### `bernstein login`

| Flag | Default | Meaning |
|---|---|---|
| `--server URL` | env `BERNSTEIN_SERVER_URL` or localhost | Server URL. |
| `--sso` | off | Open browser automatically for SSO. |

(`cli/commands/auth_cmd.py:145-146`.)

#### `bernstein auth`

| Subcommand | Purpose |
|---|---|
| `login` | Same as `bernstein login`. |
| `logout` | Revoke the current session and clear the cached token. |
| `status` | Show current authentication status. |
| `dashboard-token` | Scoped dashboard credentials (group): `issue` / `list` / `revoke`. See [Dashboard authentication](#dashboard-authentication-bernstein-auth-dashboard-token). |

#### `bernstein creds`

| Subcommand | Purpose |
|---|---|
| `list` | List stored credentials. |
| `revoke PROVIDER` | Remove a credential locally and call the provider's revoke endpoint. |
| `test PROVIDER` | Re-validate a stored credential against the provider's whoami. |

(`cli/commands/creds_cmd.py:214-282`.)

#### `bernstein policy`

| Subcommand | Purpose |
|---|---|
| `check` | Run YAML / Rego policies against the current repository diff. |

#### `bernstein compliance`

| Subcommand | Purpose |
|---|---|
| `list` | List available compliance policies. |
| `enable` / `disable` | Activate or deactivate a compliance framework policy set. |
| `check` | Evaluate compliance policies against the current runtime. |
| `assess` | Run the EU AI Act compliance assessment. |
| `eu-ai-act` | Show the current EU AI Act task-risk summary. |
| `report` | Print the EU AI Act compliance report from an existing assessment. |
| `pack` | Build a one-command EU AI Act Article 12 evidence bundle. |
| `rego` | Export OPA / Rego rule files for a compliance framework. |

(`cli/commands/compliance_cmd.py:26+`.)

#### `bernstein audit`

| Subcommand | Purpose |
|---|---|
| `show` | Show recent audit log events. `--limit N`. |
| `verify` | Verify audit log integrity. `--merkle-only`, `--hmac-only`. |
| `seal` | Compute a Merkle root across all audit log files and store the seal. |
| `export PERIOD` | Export evidence for a period. `--output DIR`, `--dir WORKDIR`. Tenant-scoped slice via `--tenant`. |
| `slice` | Write a deterministic JSONL subset between two HMAC anchors. `--from`, `--to`, `-o PATH`. |
| `query` | Query audit events. `--event-type`, `--actor`, `--since`, `--limit`. |

(`cli/commands/audit_cmd.py:25+`. The `slice` verb is the
deterministic-subset extractor described in
[HMAC-chained audit log](../security/audit-log.md#slicing-a-deterministic-subset).)

#### `bernstein lineage`

| Subcommand | Purpose |
|---|---|
| `verify RUN_ID` | Verify the run's lineage spine: recompute the full Merkle hash chain and every HMAC tag, print the head hash. `--workdir DIR`. Exit 0 = OK, 1 = no entries / seal-only (chain intact but no produced-artifact provenance), 2 = tamper. |
| `replay RUN_ID` | Walk the run's spine entries in append order (artifact, actor, step, model, content hash, entry hash). `--workdir DIR`, `--limit N`. Exit 1 on an empty run. |

Every adapter artifact write is recorded, without per-adapter opt-in, as
one Merkle-chained, HMAC-tagged entry in the run's lineage spine under
`.sdd/lineage/<run_id>/spine.jsonl` (head hash in `spine.head`). The head
hash is the run's artifact-provenance identity. Recording is gated by
`BERNSTEIN_LINEAGE_ENABLED` (default on); when enabled it fails closed, so
a write that cannot be recorded raises rather than dropping provenance.
`verify` against an empty run reports a distinct `NO ENTRIES` status
instead of passing trivially. (`cli/commands/lineage_cmd.py`,
`core/lineage/spine.py`.)

#### `bernstein credential`

| Subcommand | Purpose |
|---|---|
| `emit ARTIFACT --run-id RUN_ID` | Project the artifact's lineage-spine subtree into a signed C2PA 2.2 manifest and write `<artifact>.c2pa.json`. `--workdir DIR`, `--json`. Exit 0 = written, 1 = no lineage / bad input. |
| `verify ARTIFACT` | Confirm the manifest's hard-binding hash matches the artifact bytes and the signature chains to the install identity. `--workdir DIR`, `--manifest PATH`. Exit 0 = OK, 1 = bad input, 2 = verification failed. |

The manifest is a deterministic projection of the artifact's lineage
entries: a hard-binding assertion (`c2pa.hash.data`) carries the spine
entry's content hash and an actions assertion (`c2pa.actions`) records the
producing model and actor. It is signed with the install-identity Ed25519
key, so one attestation root covers both who ran the artifact and what was
produced. With no lineage entry for the artifact there is nothing to
project, so `emit` fails rather than fabricating an unsigned label.
Watermark and fingerprint soft-binding layers are pluggable via
`c2pa.soft-binding`. Two replays of the same run produce byte-identical
manifests. (`cli/commands/credential_cmd.py`, `core/lineage/c2pa.py`.)

#### `bernstein compaction`

| Subcommand | Purpose |
|---|---|
| `log` | Print a task's compaction receipt chain. `--task ID` (required), `--audit-dir`, `--sdd-dir`, `--json`, `--verify`. |

Every context compaction (proactive threshold or reactive overflow recovery)
is recorded as a `compaction.receipt` event in the HMAC-chained audit log and
as a step in the worker's replay journal. `log` prints those receipts
(trigger, token delta, validator verdicts, retry count, pre/post SHA-256).
`--verify` re-runs the receipt verification: the HMAC chain must verify and
every journaled compaction step must have a chain receipt with matching
hashes; the command exits non-zero otherwise.

(`cli/commands/compaction_cmd.py:32+`.)

#### `bernstein quarantine`

Reads and writes `.sdd/runtime/quarantine.json`, the cross-run quarantine the
orchestrator maintains, so neither subcommand needs a running task server.

| Subcommand | Purpose |
|---|---|
| `list` | List active quarantine entries. `--all` includes expired ones, `--workdir PATH` selects the project root. |
| `clear` | Clear entries. `--task TITLE` clears one, `--confirm` skips the prompt. |

(`cli/commands/advanced_cmd.py`.)

#### `bernstein security-review`

Pattern-scans a unified diff without calling a model. Exit `0` clean or
advisory-only, `1` on any critical/high finding, `2` when there is no diff.

| Flag | Default | Meaning |
|---|---|---|
| `TASK_ID` | none | Scan the diff one agent produced for that task. |
| `--workdir PATH` | `.` | Project root. |
| `--base REV` | `main` | Base revision when scanning the working tree. |
| `--diff-file PATH` | none | Scan a saved diff; `-` reads stdin. |
| `--as-json` | off | Emit findings as JSON. |
| `--fail-on-any` | off | Exit non-zero on any finding, not just critical/high. |

#### `bernstein approve-tool` / `bernstein reject-tool`

Tool-call approval gate. When an agent requests a sensitive tool call (network egress, file write outside its worktree, exec outside its sandbox), the orchestrator pauses and writes a request to `.sdd/runtime/tool_approvals/`. Resolve with these commands.

```bash
bernstein approve-tool --id <request_id>
bernstein reject-tool  --id <request_id>
# Flag form (the aliases above stay registered through the 3.10 line,
# unregistered in 4.0.0):
bernstein approve --tool <request_id>
bernstein reject --tool <request_id>
```

With no identifier, the oldest pending approval is resolved.

---

## Cost & tokens

| Command | Purpose | Source |
|---|---|---|
| `bernstein cost` | Spend breakdown by model / task. | `cli/commands/cost.py:540` |
| `bernstein cost profile-report` | Content-addressed per-profile cost report, appended to the audit chain. | `cli/commands/cost.py` |
| `bernstein cost policy preflight` | Surface pool exhaustion before a run starts; exits non-zero when a capped pool is (or would be) exhausted. | `cli/commands/cost.py` |
| `bernstein cost policy verify DECISION_HASH` | Verify a sealed dispatch receipt offline against the lineage spine. | `cli/commands/cost.py` |
| `bernstein cost estimate` | Estimate cost before running. | `cli/commands/cost.py` |
| `bernstein cost envelopes show` | Per-quota-envelope cost attribution. | `cli/commands/cost.py` |
| `bernstein estimate` | Deprecated alias of `bernstein cost estimate`; removed in v4.0.0. | `cli/commands/cost.py` |
| `bernstein cost-envelopes` | Deprecated alias of `bernstein cost envelopes`; removed in v4.0.0. | `cli/commands/cost.py` |

#### `bernstein cost`

| Flag | Default | Meaning |
|---|---|---|
| `--last {1h\|24h\|7d\|30d}` | none | Time range window. |
| `--since ANCHOR` | none | Anchor for `--last` (e.g. `today`, `yesterday`). |
| `--by {agent\|model\|task\|day\|role\|feature_label\|envelope\|profile}` | model | Group-by dimension. `profile` groups by response-style profile; tasks whose profile changed mid-run appear as an explicit excluded bucket. |
| `--ledger PATH` | `.sdd/cost/ledger.jsonl` | Rolling spend ledger (used when `--by` is `role\|feature_label\|profile`). |
| `--metrics-dir DIR` | `.sdd/metrics` | Directory containing metrics JSONL files. |
| `--json` | off | Emit JSON. |
| `--share` | off | Print only the shareable summary snippet. |

#### `bernstein cost profile-report`

| Flag | Default | Meaning |
|---|---|---|
| `--last {1h\|24h\|7d\|30d}` | whole ledger | Ledger window. |
| `--ledger PATH` | `.sdd/cost/ledger.jsonl` | Spend ledger to compute from. |
| `--metrics-dir DIR` | `.sdd/metrics` | Metrics JSONL files for the quality-outcome join. |
| `--transitions PATH` | `.sdd/cost/profile_transitions.jsonl` | Profile-transition event records. |
| `--audit-dir DIR` | `.sdd/audit` | Audit chain the report event is appended to. |
| `--reports-dir DIR` | `.sdd/reports/cost_profiles` | Where the content-addressed report artifact is written. |
| `--eval-ab-dir DIR` | `.sdd/reports/eval_ab` | Eval A/B artifacts; cross-profile claims link the latest one per pair. |
| `--json` | off | Emit JSON. |

Emits per-profile tasks / output tokens / USD / mean tokens per task plus
joined verification pass rates. The artifact is canonical JSON named by its
own SHA-256, embeds the ledger line-hash range it was computed from, and is
appended to the audit chain, so anyone holding the ledger can recompute it
byte-identically. Cross-profile savings are only claimed when both profiles
have at least 5 tasks with the same role and model; otherwise the report
states "insufficient comparable runs".

#### `bernstein cost estimate`

| Flag | Default | Meaning |
|---|---|---|
| `GOAL` | required | Task description to estimate (positional). |
| `--role ROLE` | none | Agent role for the task. |
| `--scope {small\|medium\|large}` | none | Task scope. |
| `--complexity {low\|medium\|high}` | none | Task complexity. |
| `--metrics-dir DIR` | `.sdd/metrics` | Directory containing historical metrics. |

#### `bernstein cost policy preflight`

Cost-aware scheduling (issue #2354). Projects the spend ledger into named
pools, compares each against its configured cap plus the planned run spend, and
exits non-zero when any capped pool is (or would be) exhausted -- so pool
exhaustion stops a run at the gate, not halfway through. Also reports the
shipped price-table staleness advisory.

| Flag | Default | Meaning |
|---|---|---|
| `--ledger PATH` | `.sdd/cost/ledger.jsonl` | Rolling spend ledger to project. |
| `--config PATH` | `bernstein.yaml` | Config holding `cost_policy.pools` caps. |
| `--plan SPEC` | none | Planned per-pool spend, e.g. `api=2.50,subscription=0`. |
| `--json` | off | Emit JSON. |

#### `bernstein cost policy verify DECISION_HASH`

Re-derives the decision hash from the stored dispatch receipt (catching a
forged admit / zeroed overrun) and re-checks the lineage-spine anchor. A
receipt that no longer recomputes fails exactly like a tampered chain entry.

| Flag | Default | Meaning |
|---|---|---|
| `--workdir DIR` | `.` | Project root holding `.sdd/cost/dispatch` receipts and `.sdd/lineage`. |
| `--json` | off | Emit JSON. |

---

## Maintenance & debug

| Command | Purpose | Source |
|---|---|---|
| `bernstein cleanup` | Clean worktrees / logs. | `cli/maintenance_cmd.py:162` |
| `bernstein daemon` | systemd / launchd unit (group). | `cli/commands/daemon_cmd.py:76` |
| `bernstein dr` | Disaster recovery (group). | `cli/commands/disaster_recovery_cmd.py:12` |
| `bernstein debug bundle` | Bug-report bundle. | `cli/debug_bundle.py:bundle_cmd` |
| `bernstein debug-bundle` | Deprecated, removed in v4.0.0. A separate, older builder -- not a rename of `debug bundle`. | `cli/commands/debug_cmd.py:debug_cmd` |
| `bernstein doctor` | Self-diagnostics. | `cli/doctor_cmd.py:281` |
| `bernstein self` | Provenance-verified update lifecycle (group). | `cli/commands/self_update_cmd.py:self_group` |
| `bernstein self-update` | Compatibility alias for `bernstein self`. | `cli/commands/self_update_cmd.py:self_update_cmd` |
| `bernstein man-pages` | Man-page generator. | `cli/man_page.py:man_pages_cmd` |
| `bernstein completions` | Shell completion script. | `cli/commands/advanced_cmd.py:1076` |
| `bernstein config-path` | Show config path. | `cli/config_path_cmd.py:54` |
| `bernstein config` | Config mgmt (group). | `cli/workspace_cmd.py:180` |
| `bernstein workspace` | Workspace mgmt (group). | `cli/workspace_cmd.py:30` |
| `bernstein session` | Session mgmt (group). | `cli/session_cmd.py:27` |
| `bernstein memory` | Memory store (group). | `cli/commands/memory_cmd.py:19` |
| `bernstein cache` | Prompt-cache mgmt (group). | `cli/commands/cache_cmd.py:45` |
| `bernstein notify` | Outbound notification drivers (group). | `cli/commands/notify_cmd.py:63` |
| `bernstein triggers` | Trigger sources (group). | `cli/commands/triggers_cmd.py:17` |
| `bernstein issue-to-pr trace --repo OWNER/NAME N` | Print the read-only issue-to-PR pipeline state snapshot. | `cli/commands/issue_to_pr_cmd.py:trace_cmd` |

#### `bernstein doctor`

| Flag | Default | Meaning |
|---|---|---|
| `--json` | off | Emit raw JSON. |
| `--fix` | off | Attempt to auto-fix issues. |
| `--suggest-docs` | off | Print the top curated documentation gaps and exit. |
| `--failover-drill` | off | Exercise every declared provider fallback chain; exit non-zero on any broken chain. |
| `--endpoint URL` | none | Certify an OpenAI-compatible endpoint; see [Endpoint certification](#endpoint-certification-bernstein-doctor---endpoint). |
| `--endpoint-model NAME` | first `/models` entry | Model id to certify. |
| `--endpoint-engine NAME` | none | Runtime label recorded in the receipt (e.g. `ollama`, `lmstudio`, `mlx`). |
| `--endpoint-api-key-env NAME` | none | Name of the env var holding the endpoint's API key (never the key itself). |
| `--endpoint-timeout SEC` | 60 | Per-probe response budget; exceeding it fails the probe. |
| `--role NAME` | low-stakes local tier | Role(s) to evaluate against `--endpoint` (repeatable). |

(`cli/commands/advanced_cmd.py:536-550` re-exposes `cli/status_cmd.py:doctor`.)

#### `bernstein debug bundle`

| Flag | Default | Meaning |
|---|---|---|
| `--task ID` | none | Filter traces/metrics by task. |
| `--run ID` | none | Filter traces/metrics by run. |
| `--last / --no-last` | `--last` | Select the most recent run. |
| `--out FILE` | timestamped file in CWD | Output zip path. |
| `--manifest-only` | off | Print the manifest JSON instead of writing a ZIP. |
| `--include-source-snippets N` | `0` | Include the N most-recently-changed `src/` files. |

#### `bernstein debug-bundle` (deprecated)

The older, separate builder. Removed in v4.0.0; `bernstein debug bundle` does
not accept these flags.

| Flag | Default | Meaning |
|---|---|---|
| `--yes` / `-y` | off | Skip the confirmation prompt. |
| `--output PATH` / `-o` | timestamped file | Output zip path. |
| `--extended` | off | Include full (untruncated) logs. No equivalent in `debug bundle`. |

#### `bernstein self`

| Subcommand | Purpose |
|---|---|
| `check-update` | Verify the signed release feed offline and seal a chain-anchored advisory. |
| `update` | Install the verified candidate; refuses mid-run, verifies the wheel hash first. `--override-pin` crosses a signed pin. |
| `pin VERSION` / `unpin` | Signed version pin the updater will not cross unless explicitly overridden. |
| `rollback` | Return to the previous receipted version. |

See [Updates: check, verify, apply](../operations/updates.md).

#### `bernstein self-update`

| Flag | Default | Meaning |
|---|---|---|
| `--check` | off | Same as `bernstein self check-update`. |
| `--rollback` | off | Same as `bernstein self rollback`. |
| `--yes`, `-y` | off | Skip the confirmation prompt. |

#### `bernstein completions`

| Flag | Default | Meaning |
|---|---|---|
| `--shell {bash\|zsh\|fish}` | bash | Target shell. |

```bash
eval "$(bernstein completions --shell bash)"
bernstein completions --shell zsh > ~/.zsh/completion/_bernstein
```

#### `bernstein config`

| Subcommand | Purpose |
|---|---|
| `list` | List all config keys with their effective values and sources. |
| `get KEY` | Show the effective value for KEY and its source. |
| `set KEY VALUE` | Update a config value. |
| `diff` | Show settings that differ from defaults. |
| `conflicts` | Show settings where multiple sources define conflicting values. |
| `view-mode` | Set the dashboard detail level (novice, standard, expert). |
| `validate` | Validate project configuration. |

#### `bernstein workspace`

| Subcommand | Purpose |
|---|---|
| `clone` | Clone all missing repos defined in the workspace. |
| `validate` | Check workspace health: all repos exist and are valid git checkouts. |

For worktree lifecycle (inspection / reaping) use `bernstein worktrees list` / `bernstein worktrees gc`.

#### `bernstein session`

| Subcommand | Purpose |
|---|---|
| `list` | List all recorded sessions, newest first. |
| `show NAME` | Show full details of a recorded session. |
| `fork` | Fork a recorded session into a sibling git worktree. |
| `replay` | Replay a recorded session for deterministic reproducibility. |

#### `bernstein memory`

| Subcommand | Purpose |
|---|---|
| `list` | List stored memories. |
| `add CONTENT` | Add a persistent memory entry. |
| `remove ID` | Remove a memory entry by id. |
| `share KEY VALUE --tag TAG` | Publish a cross-task fact. |
| `query --tag TAG` | List published facts (redacted by default). |
| `verify --scope SCOPE --namespace NS` | Prove every fact in a scope/namespace chain was written by its actor and never edited; recomputes the hash chain, every HMAC tag, and each `source_hash` anchor against the lineage spine. Exit 0 = OK, 1 = no entries, 2 = tamper. |
| `why FACT --scope SCOPE --namespace NS` | Return the originating run id and step for a stored fact (only when its `source_hash` resolves to a real lineage-spine entry). |
| `forget ENTRY_HASH --scope SCOPE --namespace NS` | Append a signed tombstone for a memory-chain entry without deleting it; the original entry and chain stay verifiable. |

#### `bernstein cache`

| Subcommand | Purpose |
|---|---|
| `list` | List cached task-result entries. `--workdir`, `--limit`, `--json`. |
| `inspect TASK_ID` | Inspect the cached result produced by a specific task. `--workdir`, `--json`. |
| `action` | Inspect / replay the action-level LLM cache. |
| `clear` | Clear response-cache entries. `--workdir`, `--unverified`, `--yes`. |

(`cli/commands/cache_cmd.py:45-146`.)

#### `bernstein notify`

| Subcommand | Purpose |
|---|---|
| `list` | List configured sinks from `bernstein.yaml`. |
| `test` | Fire a synthetic event end-to-end through `--sink`. |

(`cli/commands/notify_cmd.py:63+`.)

#### `bernstein triggers`

| Subcommand | Purpose |
|---|---|
| `list` | Show all configured triggers and their status. `-n LIMIT`. |
| `fire NAME` | Manually fire a trigger by name (for testing). |
| `history` | Show the recent trigger fire log. |

#### `bernstein dr`

Disaster recovery; see [`operations/disaster-recovery.md`](../operations/disaster-recovery.md).

| Subcommand | Purpose |
|---|---|
| `backup` | Backup persistent `.sdd/` state to a file. |
| `restore` | Restore `.sdd/` state from a backup file. |

#### `bernstein daemon`

systemd / launchd unit installer.

| Subcommand | Purpose |
|---|---|
| `install` | Install the unit. `--user` / `--system`, `--command`, `--env`, `--force`. |
| `uninstall` | Remove the unit. |
| `status` | Show daemon status. |
| `start` / `stop` / `restart` | Control daemon lifecycle. |

(`cli/commands/daemon_cmd.py:76+`.)

#### `bernstein man-pages`

| Flag | Default | Meaning |
|---|---|---|
| `--output-dir DIR` | `docs/man` | Directory to write man page files into. |

#### `bernstein config-path`

Print the path Bernstein would read config from. Useful for shell completion and CI. No flags.

---

## Integration & MCP

| Command | Purpose | Source |
|---|---|---|
| `bernstein mcp` | MCP server (transport, port). | `cli/mcp_cmd.py:29` |
| `bernstein mcp catalog` | MCP catalog (group). | `cli/commands/mcp_catalog_cmd.py:130` |
| `bernstein chat` | Chat-control bridges (group). | `cli/commands/chat_cmd.py:54` |
| `bernstein hooks` | Hook mgmt (group). | `cli/commands/hooks_cmd.py:35` |
| `bernstein github setup` | GitHub integration setup. | `cli/commands/advanced_cmd.py:1056` |
| `bernstein github test-webhook` | Test webhook config. | `cli/commands/advanced_cmd.py:1065` |
| `bernstein pr` | GitHub PR ops. | `cli/commands/pr_cmd.py:183` |
| `bernstein review-responder` | PR review responder daemon (group). | `cli/commands/review_responder_cmd.py:46` |
| `bernstein preview` | Sandboxed dev-server with public tunnel (group). | `cli/commands/preview_cmd.py:46` |

#### `bernstein mcp`

The root MCP command - runs Bernstein as an MCP server itself.

| Flag | Default | Meaning |
|---|---|---|
| `--transport {stdio\|http}` | stdio | MCP transport. |
| `--port N` | 8053 | HTTP port (when `--transport http`). |
| `--host HOST` | 127.0.0.1 | Bind host. |
| `--server-url URL` | `http://localhost:8052` | Upstream Bernstein server. |
| `--mcp-tier {core\|standard\|all}` | unset | Tool tier to advertise (context-budget knob); overrides `BERNSTEIN_MCP_TOOL_TIER`, and the effective default is `standard`. |

#### `bernstein mcp catalog`

See [`reference/mcp-catalog.md`](mcp-catalog.md) for the full reference.

#### `bernstein chat`

| Subcommand | Purpose |
|---|---|
| `serve` | Run the chat bridge until Ctrl-C. `--platform {telegram\|discord\|slack\|teams}`, `--token`, `--allow`. |
| `status` | Print active chat<->session bindings. |
| `logout` | Drop cached bindings for PLATFORM. |

#### `bernstein hooks`

| Subcommand | Purpose |
|---|---|
| `list` | Print registered hooks for each lifecycle event. |
| `run EVENT` | Fire EVENT with an empty context (useful for smoke-testing). |
| `check` | Validate hook-config syntax and script availability. |
| `dry-run EVENT` | Fire EVENT with a synthetic payload to see what fires. |

#### `bernstein pr`

| Flag | Default | Meaning |
|---|---|---|
| `--session-id ID` | most recent completed session | Session to publish. |
| `--base BRANCH` | main | Base branch for the pull request. |
| `--title TEXT` | auto-generated | Override the PR title. |
| `--draft` | off | Open as a draft PR. |
| `--dry-run` | off | Print the would-be title and body without calling `gh`. |
| `--no-push` | off | Skip `git push`; assume the branch is already on origin. |

(`cli/commands/pr_cmd.py:183-220`.)

#### `bernstein review-responder`

| Subcommand | Purpose |
|---|---|
| `start` | Start the review-responder daemon. `--repo`, `--tunnel`, `--port`, `--quiet-window`, `--cost-cap`, `--foreground`. |
| `status` | Show daemon status. `--pr`. |
| `tick` | Single-shot poll-and-respond cycle. `--repo`, `--pr`. |

#### `bernstein preview`

| Subcommand | Purpose |
|---|---|
| `start` | Start a preview server in the current task's worktree. `--cwd`, `--command`, `--provider`, `--auth`, `--expire`, `--no-clipboard`. |
| `list` | List active previews. `--json`. |
| `status ID` | Show a preview's URL and process. `--json`. |
| `stop [ID]` | Stop one preview. `--all` stops every active preview. |

(`cli/commands/preview_cmd.py:46-220`.)

---

## Misc

| Command | Purpose | Source |
|---|---|---|
| `bernstein explain CONCEPT` | Concept explainer. | `cli/explain_help_cmd.py:171` |
| `bernstein help-all` | Comprehensive help screen. | `cli/commands/advanced_cmd.py:378` |
| `bernstein aliases` | Show CLI aliases. | `cli/aliases.py` |
| `bernstein fingerprint` | Replay verification (group). | `cli/fingerprint_cmd.py:37` |
| `bernstein graph` | Dependency graph (group). | `cli/graph_cmd.py:19` |
| `bernstein profile` | Task profiling. | `cli/profile_cmd.py:73` |
| `bernstein evolve` | Self-improvement loop (see [Adapters & agents](#adapters-agents)). | `cli/evolve_cmd.py:48` |
| `bernstein changelog` | Changelog from runs (group: bare = agent-produced diffs, `conventional` subcommand = from conventional commits). | `cli/changelog_cmd.py:405` |
| `bernstein run-changelog` | Deprecated alias for `bernstein changelog` (removed in a later release). | `cli/changelog_cmd.py:533` |
| `bernstein checkpoint` | Save progress (see [Run & control](#run-control)). | `cli/commands/checkpoint_cmd.py:49` |
| `bernstein listen` | Voice control (experimental). | `cli/commands/voice_cmd.py` |
| `bernstein install-hooks` | Install git hooks. | `cli/commands/advanced_cmd.py:448` |
| `bernstein ab-test` | A/B model comparison. | `cli/commands/ab_test_cmd.py:14` |
| `bernstein acp serve` | Run an ACP server. | `cli/commands/acp_cmd.py:33` |
| `bernstein scaffold "<prompt>"` | Bootstrap a project from a prompt. | `cli/commands/scaffold_cmd.py` |
| `bernstein test` | Run automated resilience tests. | `cli/commands/test_cmd.py:13` |
| `bernstein wiki build` | Render `WIKI.md` from the AST symbol graph. | `cli/commands/wiki_cmd.py` |
| `bernstein workflow` | Workflow mgmt (group). | `cli/workflow_cmd.py:15` |
| `bernstein replay RUN_ID --verify` / `--from-step N` | Recompute the run journal's Merkle head and report the first divergent step (writes `divergence_report.json`), or rebuild deterministic state to step N. | `cli/commands/advanced_cmd.py` |
| `bernstein thread verify --run <id>` | Prove the live event stream equals the run journal: recompute the journal's Merkle chain and confirm every streamed event carries the byte-identical entry hash. `--json` for machine output. Exit 1 on divergence, 2 when the run journal is missing. | `cli/commands/thread_cmd.py` |
| `bernstein webhook verify <event_id>` | Verify an audited webhook node's signed receipts: recompute the inbound event hash and the outbound result hash against the run journal, re-check both Ed25519 signatures offline, and re-anchor both receipts against the webhook-node lineage spine. Exit 1 when no receipt / no outbound yet, 2 on tamper. | `cli/commands/webhook_cmd.py` |
| `bernstein escalation show <id>` | Print the operator projection of a stall escalation receipt: stall reason, deterministic recommended action, resume fork point, and spine anchor. `--json` for machine output. Exit 1 when no receipt matches the id. | `cli/commands/escalation_cmd.py` |
| `bernstein escalation verify <id>` | Reconstruct the trailing failure window from the run journal, walk the journal's Merkle chain, and confirm every bound entry hash matches the receipt (plus the Ed25519 signature and spine anchor). Exit 0 verified, 1 no receipt, 2 mismatch (a tampered journal entry inside the window). | `cli/commands/escalation_cmd.py` |
| `bernstein schedule show <id> --at <time>` / `bernstein schedule verify` | Project a recurring fire onto a canonical task graph. `show --at <epoch-or-ISO8601>` prints the deterministic graph hash the schedule would dispatch at that instant without firing (no journal, receipt, or `last_fire_at` mutation). `verify` replays every recorded fire and confirms its graph hash reproduces byte-identically from `(schedule, fire_time, state)`; `--json` for machine output, exit 1 on any mismatch. RFC-5545 `RRULE` and cron are both accepted; a webhook / file-change trigger binds its event as an input hash. | `cli/commands/schedule_cmd.py` |
| `bernstein schedule routine export\|provision\|register\|bindings` | Bridge a scenario to an external Routine session and back (`bernstein routine` is a deprecated alias, removed in 4.0.0). See [routine scenarios](../routine-scenarios.md). | `cli/commands/routine_cmd.py` |
| `bernstein activity verify <run>` | Re-verify every typed activity boundary crossing anchored in a run's canonical event journal. Confirms the journal's Merkle chain is intact, recomputes each activity's `evidence_set_hash` from its pinned observation hashes, and reattaches the evidence bytes from the run's content store (when present), re-checking each content hash. Works across modalities (research, browser/computer-use, data, ops, coding). `--json` for machine output. Exit 0 verified, 1 no run / no activity, 2 mismatch (a tampered journal entry or a divergent stored blob). | `cli/commands/activity_cmd.py` |
| `bernstein interop a2a verify-thread --from-thread <task-uuid>` | Prove a cross-agent A2A thread equals the executed actions: for the task uuid, recompute every signed message receipt binding `{message_hash, peer_card_fingerprint, task_uuid, journal_entry_hash}`, re-check each Ed25519 signature offline, verify the message-receipt lineage spine, re-anchor each receipt against it, and confirm every message hash is referenced by the seeded per-task journal. `--json` for machine output. Exit 0 verified, 1 on no thread / mismatch (a tampered receipt, spine, or journal). | `cli/commands/interop_cmd.py` |
| `bernstein a2a verify --receipt <file> --response <file>` | Verify an inbound A2A response against its lineage receipt, offline. Recomputes `content_hash` over the canonical response bytes and checks the Ed25519 head signature over the receipt binding `{schema_version, task_id, artefact_path, content_hash, entry_hash, operator_hmac, kid}`. `--trusted-jwk <file>` pins the signing key instead of trusting the embedded one; `--json` for machine output. Exit 0 verified, 1 on a tampered answer, a rewritten receipt field, or a missing signature (an unattested answer is treated as unverified, not trusted). | `cli/commands/a2a_cmd.py` |
| `bernstein a2a publish --endpoint <url>` | Emit agent-registry records advertising this node's signed capability card. Each record embeds the full signed card plus an `ed25519/<fp>` publisher fingerprint so a consumer verifies the claim against the node's own key. `--surface a2a-card\|mcp-registry\|agntcy-ads` (repeatable) selects surfaces; the default emits `a2a-card` and `mcp-registry`, while `agntcy-ads` is opt-in and emits an OASF capability descriptor (a deterministic projection of the card pinned to a stated OASF schema version) with Sigstore provenance signed by a distinct provenance key. `--card <file>` reuses a persisted card so republishing keeps one identity, `--output-dir <dir>` sets the destination. Output is deterministic: republishing an unchanged node rewrites identical bytes. | `cli/commands/a2a_cmd.py` |
| `bernstein evidence show <task>` | Render the sealed verification evidence bundle for a task: gate verdict, bundle hash, spine anchor, and a per-producer table (kind, required/advisory, pass/fail, exit code, stored size, content hash). `-w/--workdir` sets the project root. Exit 0 when a bundle exists, 1 when there is none. | `cli/commands/evidence_cmd.py` |
| `bernstein evidence verify <task>` | Recompute a task's evidence bundle offline: check the Ed25519 signature over the canonical binding, verify the evidence lineage spine and the bundle's spine anchor, and re-hash every stored evidence blob (plus each media item's C2PA content credential) against the sealed manifest. Exit 0 verified, 1 no bundle, 2 mismatch (a tampered evidence file, bundle, or spine). `bernstein audit verify` runs the same check across every bundle. | `cli/commands/evidence_cmd.py` |
| `bernstein ledger verify <run>` | Walk a run's durable work ledger (`.sdd/runtime/ledger/<run-id>/`) and recompute every entry hash against the canonical-JSON contract. A tampered entry is named at its exact position (`entry <seq> (line <n>)`). `--expected-head HASH` additionally pins the tail. `--json` for machine output. Exit 0 verified, 1 no ledger, 2 mismatch. | `cli/commands/ledger_cmd.py` |
| `bernstein ledger anchor <run>` | Verify the run's chain, then publish it -- chunked, with a deterministic tree identity -- to `refs/bernstein/work-ledger/<run-id>` and mirror the anchor into the HMAC audit chain as a `work_ledger.anchor` event. Re-anchoring an extended chain adds a child commit; an identical chain is idempotent. Exit 0 anchored, 1 no ledger, 2 broken chain or git refusal, 3 the anchored chain diverges from the local one. | `cli/commands/ledger_cmd.py` |
| `bernstein ledger fetch <run>` | Pull the anchored ledger ref from a remote (default `origin`) after a clone and materialize it into `.sdd/runtime/ledger/<run-id>/`. Verifies the anchored chain end to end before writing; an existing local chain is only ever fast-forwarded -- a diverged pair is refused with the exact fork entry named. Exit 0 materialized, 1 no anchored ledger on the remote, 2 broken anchored chain, 3 divergence. | `cli/commands/ledger_cmd.py` |
| `bernstein ledger resume <run>` | Resume a run from its work ledger on any clone: verify the chain end to end, rebuild scheduler state by deterministic replay (completed / in-flight / scheduled / failed tasks), record the resume as a new chain entry, and write one resume signal per frontier task for the resume watcher. `--dry-run` prints the plan without recording anything; `--json` for machine output. Exit 0 resumed, 1 no ledger, 2 verification failed (exact entry position reported), 3 two divergent resumes detected and refused. | `cli/commands/ledger_cmd.py` |
| `bernstein ledger runs` | List runs with an anchored work ledger in this repository. `--json` for machine output. | `cli/commands/ledger_cmd.py` |
| `bernstein ledger gc <run>` | Squash the run's anchor history to a single commit, preserving the current anchored tree byte for byte. Superseded chunk blobs become unreachable so a normal `git gc` reclaims them -- the repo-bloat bound for long runs. Exit 0 done, 1 no anchored ledger. | `cli/commands/ledger_cmd.py` |
| `bernstein run-service submit <goal> --task <id>...` | Open a detached run: seed the work ledger (`run.open` + one `task.scheduled` per `--task`), persist the run descriptor (goal digest, never the goal text), and sign a `submitted` lifecycle receipt into the HMAC audit chain. By default spawns a session-detached supervisor that survives the terminal; `--foreground` advances the run in-process; `--per-task-delay` makes off-terminal progress observable; `--json` for machine output. `--backend ssh` runs each task off-host on the ssh backend in its own isolated remote git worktree (one branch per task) and signs a `run.ssh_task` receipt binding that worktree; pass `--ssh-host` and `--ssh-path` (absolute remote dir), optionally `--ssh-user`/`--ssh-port`/`--ssh-identity`, `--ssh-repo` to git-worktree from with `--ssh-base-branch`, and `--ssh-secret ENV=PROVIDER` (repeatable) to inject a vault credential into the remote env resolved from the vault only, never the ledger or the receipts. | `cli/commands/run_service_cmd.py` |
| `bernstein run-service attach <run>` | Reattach from any shell: prove the current ledger head is a forward extension of the head last seen (the reattach artefact is that continuity proof), record a `reattached` receipt, and render the live projection (completed / in-flight / scheduled tasks). `--json` for machine output. Exit 0 continuous, 1 no such run, 3 continuity broken (the ledger diverged or failed to verify). | `cli/commands/run_service_cmd.py` |
| `bernstein run-service status [<run>]` | Show supervisor liveness plus the ledger projection for a run; with no run id, list every run in the project. `--json` for machine output. Exit 1 when the named run does not exist. | `cli/commands/run_service_cmd.py` |
| `bernstein run-service stop <run>` | Stop the run's supervisor process (SIGTERM then SIGKILL after a grace window) and record a `detached` boundary receipt so a later attach can prove continuity. `--json` for machine output. Exit 1 when the run does not exist. | `cli/commands/run_service_cmd.py` |
| `bernstein run-service verify <run>` | Re-verify offline that the HMAC audit chain is intact, the work ledger recomputes end to end, and every lifecycle receipt binds a ledger head that exists in the chain (every reattach / daemon-restart boundary is a genuine ancestor). `--json` for machine output. Exit 0 verified, 2 a check failed (each reason listed). | `cli/commands/run_service_cmd.py` |

#### `bernstein ab-test`

| Flag | Default | Meaning |
|---|---|---|
| `--model-a NAME` | required | First model. |
| `--model-b NAME` | required | Second model. |
| `--task TEXT` | required | Task description handed to both models. |
| `--role NAME` | `backend` | Agent role. |
| `--scope {small\|medium\|large}` | `medium` | Task scope. |
| `--timeout SECONDS` | `1800` | Per-model timeout. |

#### `bernstein acp serve`

| Flag | Default | Meaning |
|---|---|---|
| `--stdio/--no-stdio` | `--stdio` | Serve over POSIX stdio (line-delimited JSON-RPC), the IDE embedding transport. |
| `--http HOST:PORT` | off | Serve over HTTP on HOST:PORT (e.g. `:8062` or `127.0.0.1:8062`). Overrides `--stdio` when both are supplied. |
| `--server-url URL` | `http://localhost:8052` | URL of the running Bernstein task server. |

#### `bernstein fingerprint`

| Subcommand | Purpose |
|---|---|
| `build` | Build a local similarity index from a corpus directory. |
| `check FILE` | Check generated code against the index. |

(`cli/commands/fingerprint_cmd.py:37+`.)

#### `bernstein graph`

| Subcommand | Purpose |
|---|---|
| `tasks` | Render the current task dependency graph as ASCII or Mermaid. |
| `impact FILE_QUERY` | Print downstream files impacted by changing FILE_QUERY. |

#### `bernstein listen`

Experimental voice control (see [`operations/voice-control.md`](../operations/voice-control.md) when published).

| Flag | Default | Meaning |
|---|---|---|
| `--model SIZE` | `base` | Whisper model size; smaller is faster but less accurate. |
| `--threshold RMS` | 0.01 | RMS amplitude threshold distinguishing speech from silence. |
| `--min-duration SEC` | 0.5 | Minimum utterance duration before transcription. |
| `--alias-file PATH` | `~/.bernstein/voice.yaml` | Voice alias YAML file. |
| `--dry-run` | off | Show the parsed command without executing. |

#### `bernstein explain`

| Flag | Default | Meaning |
|---|---|---|
| `CONCEPT` | required | Concept name (e.g. `cascade-router`, `wal`, `janitor`). |

#### `bernstein test`

Runs automated resilience tests. This is not a project test-suite runner; use `bernstein.yaml: quality_gates.tests` (and your configured test runner) for that.

| Flag | Default | Meaning |
|---|---|---|
| `--duration N` | 300 | Test duration in seconds. |
| `--workdir PATH` | `.` | Project root. |

#### `bernstein wiki build`

| Flag | Default | Meaning |
|---|---|---|
| `--repo PATH` | current directory | Repo root to scan. |
| `--write` | off | Write to `WIKI.md` at the repo root. |
| `--output PATH` | unset | Custom output path; implies `--write`. |

Renders a deterministic Markdown wiki from the AST symbol graph
plus the `agents.md` IR. Streams to stdout by default. See
[Wiki build](../concepts/wiki-build.md) for the operator guide.

#### `bernstein scaffold`

| Flag | Default | Meaning |
|---|---|---|
| `PROMPT` | required | Free-form goal prompt. |
| `--template NAME` | `auto` | Pin a template; `auto` runs the keyword heuristic. |
| `--output DIR` | `./<slug>` | Destination directory. |
| `--force` | off | Allow writing into a non-empty directory. |

First slice of the prompt-to-repo scaffolder. See
[Prompt-to-repo scaffold](../concepts/scaffold.md).

---

## Hidden commands

Three task-related commands carry `hidden=True`, so they do not appear in
`--help`. They are stable and supported, and each is registered at the **top
level** -- not under `bernstein task`. The spellings below are the ones that
resolve.

| Command | Source | Notes |
|---|---|---|
| `bernstein add-task TITLE` | `cli/commands/task_cmd.py:155` | Declared as `compose`, registered top-level as `add-task`. |
| `bernstein sync` | `cli/commands/task_cmd.py:337` | Reconciles on-disk task files with the running server. Use when you've hand-edited backlog files and want them registered without restarting. |
| `bernstein list-tasks` | `cli/commands/task_cmd.py:776` | Declared as `parts`, registered top-level as `list-tasks`. |

A command's declared name and its registered name differ here, so the
declared spelling is not an invocation: `bernstein task compose` and
`bernstein task parts` resolve to nothing. Type the names in the table.

`bernstein task` itself is a real group and is not hidden - it carries the
durable lifecycle subcommands documented under
[Plan & tasks](#bernstein-task). It is only these three that are *not* under
it, despite their declared names suggesting otherwise.

`task_cmd.py` also declares a `notes` command (`_notes_legacy`,
`cli/commands/task_cmd.py:753`) that is registered nowhere and therefore
cannot be invoked at all. To tail server / spawner logs, use `bernstein logs`.

---

## See also

- [`cli/task-lifecycle.md`](cli/task-lifecycle.md) - driving Bernstein from a script.
- [`cli/replay.md`](cli/replay.md) - `replay` reference.
- [`reference/mcp-catalog.md`](mcp-catalog.md) - MCP catalog walkthrough.
- [`reference/openapi-reference.md`](openapi-reference.md) - REST + WebSocket + ACP/A2A endpoints.
- [`reference/FEATURE_MATRIX.md`](FEATURE_MATRIX.md) - capability matrix.
- [`operations/CONFIG.md`](../operations/CONFIG.md) - every config key Bernstein recognises.

---

## Endpoint certification: `bernstein doctor --endpoint`

Certify an OpenAI-compatible endpoint (a local runtime such as ollama, LM
Studio, or an MLX server) for per-role use. The doctor runs a fixed
conformance subset -- reachability, chat completion, tool calling, patch
format fidelity, timeout behavior, context floor -- and prints a
deterministic certify/reject verdict per role with machine reason codes.
The result is sealed as a signed receipt under
`.sdd/endpoints/certifications/`, anchored to the lineage spine, and
mirrored into the audit chain; config validation gates merge-critical roles
on it.

| Flag | Default | Meaning |
|---|---|---|
| `--endpoint URL` | - | Base URL of the endpoint to certify (activates this mode). |
| `--endpoint-model NAME` | first `/models` entry | Model id to certify. |
| `--endpoint-engine NAME` | empty | Runtime label recorded in the receipt. |
| `--endpoint-api-key-env NAME` | none | NAME of the env var holding the endpoint key. |
| `--endpoint-timeout SECONDS` | 60 | Per-probe response budget; exceeding it fails the probe. |
| `--role ROLE` | low-stakes local tier | Role(s) to evaluate (repeatable). |
| `--json` | off | Machine-readable transcript, verdicts, and receipt anchor. |

Exit codes: `0` every evaluated role certified, `1` at least one role
rejected, `2` no model could be resolved.

```bash
bernstein doctor --endpoint http://127.0.0.1:11434/v1 --endpoint-engine ollama
bernstein doctor --endpoint http://127.0.0.1:11434/v1 --role manager
```

See [Local endpoints](local-endpoints.md) for profiles, role tiers, and the
verified-configuration table.

## Provider failover drill

#### `bernstein doctor --failover-drill`

| Flag | Default | Meaning |
|---|---|---|
| `--failover-drill` | off | Exercise every fallback chain declared under `provider_availability` in `bernstein.yaml`. |
| `--json` | off | Machine-readable drill report (for CI). |

Probes every declared chain element and evaluates each chain position as
the dispatch target under a simulated outage of its predecessors. Exits
non-zero when any declared chain element is broken, and zero when all are
healthy. Each drill row carries the deterministic routing-decision hash its
simulated outage prefix would produce; drill outcomes are mirrored into the
audit chain when a `.sdd` workspace is present. See
[Provider availability & failover](../operations/provider-availability.md).

## Packaged agent skill: `bernstein skills package`

Bernstein ships a cross-vendor `bernstein-run` skill (open `SKILL.md`
format) so agent sessions can drive orchestration without a separate
shell. Installs are receipt-backed: each install anchors a
content-addressed receipt in the `skills` lineage spine and mirrors a
`plugin.install_receipt` event into the HMAC audit chain.

#### `bernstein skills package show`

Prints the bundled skill's content address, manifest hash, and the
supported host list.

#### `bernstein skills package install`

| Flag | Default | Meaning |
|---|---|---|
| `--host NAME` | - | Target host (`claude`, `codex`, `copilot`, `cursor`, `gemini`); selects the host's default skills directory. |
| `--scope project\|user` | `project` | Install under the project root or the home directory. |
| `--dest DIR` | - | Explicit destination directory (overrides `--host`/`--scope`). |
| `--record-only` | off | Anchor a tree the host already installed (e.g. a plugin checkout) without copying. |
| `--force` | off | Overwrite a destination whose content differs from the bundled skill. |
| `--workdir DIR` | `.` | Project root where the receipt is anchored. |

Exit codes: `0` installed and anchored, `1` error.

#### `bernstein skills package verify`

Re-hashes the installed tree and proves it against the anchored receipt:
the recomputed content address selects the receipt, then the install
spine and the manifest hash are checked. A tampered tree resolves to a
content address with no receipt, so the verdict is structural.

Exit codes: `0` verified, `1` missing directory, `2` attestation failure.

#### `bernstein skills package update`

Supersedes a previously attested install with new content. Unlike
`install --force` (which overwrites and anchors an independent install
receipt), `update` binds the *prior* content address to the new one: the
update receipt is content-addressed by the new tree, anchored in the same
`skills` lineage spine, and mirrored into the HMAC chain as a
`plugin.update_receipt` event. A verifier walks the update receipts newest
to oldest and lands on the root install, so the supersession history of an
installed tree is reconstructable offline. A tree that was never anchored
is refused (run `install` first).

| Flag | Default | Meaning |
|---|---|---|
| `--host NAME` | - | Target host; selects the host's default skills directory. |
| `--scope project\|user` | `project` | Update under the project root or the home directory. |
| `--dest DIR` | - | Explicit installed directory (overrides `--host`/`--scope`). |
| `--source DIR` | bundled skill | Tree to update to. |
| `--workdir DIR` | `.` | Project root where the receipt is anchored. |

Exit codes: `0` updated or already current, `1` error (missing or
unattested install).

#### `bernstein skills package status`

Scans the default skill directory for each supported host and scope,
re-hashes any present tree, and proves it against its anchored install or
update receipt. `--json` emits the per-install verdicts as JSON; `--home`
overrides the home directory for user-scoped destinations.

Exit codes: `0` every present install verifies (or none present), `2` at
least one present install failed verification.

#### `bernstein skills package conformance`

Installs the bundled skill into every selected host against one shared
install, then replays the skill's documented self-check contract (`skills
package show`, then `skills package verify --dest`) per host. Each host runs
the contract as it would from inside its own session; the per-host pass/fail
table, the shared content address, and the aggregate verdict are sealed into
a content-addressed conformance receipt anchored in the lineage spine and a
`plugin.conformance_receipt` audit-chain event.

Options: `--host` (repeatable; defaults to every supported host), `--scope`
(`project`/`user`), `--min-hosts` (green hosts required for an overall pass;
default `3`), `--json`, `--workdir`.

Exit codes: `0` every host green and the `--min-hosts` bar met, `2`
conformance failed, `1` error.

#### `bernstein skills package image-verify`

Proves, offline, that the MCP registry listing (`server.json`) and the Docker
MCP catalog entry (`packaging/docker-mcp/server.yaml`) resolve to the same
canonical signed `ghcr.io/<owner>/bernstein` image and that the registry
listing pins the release version, so a host cannot pull a different (or
unsigned) image than the catalog advertises. With `--online` it additionally
runs `gh attestation verify` against the live Sigstore build-provenance
attestation.

Options: `--version` (the release version the image must pin; defaults to the
installed bernstein version), `--online`, `--json`, `--repo-root`.

Exit codes: `0` consistent (and, with `--online`, attestation verified or
tooling unavailable), `2` a manifest mismatch or a failed online attestation.

```bash
bernstein skills package install --host claude --scope project
bernstein skills package install --dest ~/.claude/plugins/bernstein --record-only
bernstein skills package verify --host claude --scope project
bernstein skills package update --host claude --scope project
bernstein skills package status
bernstein skills package conformance --host claude --host codex --host cursor
```

See [Agent sessions](../integrations/agent-session.md) for the skill
body, per-host notes, and the registry listings generated at release
time.

## Dashboard authentication: `bernstein auth dashboard-token`

The dashboard (`bernstein gui serve`, `/dashboard` on the task server)
accepts two credential kinds: a password (`BERNSTEIN_DASHBOARD_PASSWORD` or
the `dashboard_auth` block in `bernstein.yaml`) and scoped tokens issued
here. Tokens carry a principal and a scope: `viewer` reads every surface and
can change nothing; `operator` can also trigger state-changing actions.

Grants live in an append-only journal of HMAC-signed rows
(`.sdd/auth/dashboard_tokens.jsonl`) that stores only the token's SHA-256
digest - the raw token is printed once at issue time. Editing a row (for
example widening `viewer` to `operator`) breaks its signature and the token
stops validating. Every issue and revoke is mirrored onto the audit chain
(`dashboard.token_grant`), and every login and write authorization is a
signed governance decision in the `dashboard-auth` lineage run - recompute
them offline with `bernstein governance verify dashboard-auth`.

| Subcommand | Purpose |
|---|---|
| `issue --principal NAME [--scope viewer\|operator]` | Issue a token (printed once, digest journaled). |
| `list` | Show journal rows: id, kind, principal, scope. Never prints tokens. |
| `revoke TOKEN_ID` | Append a signed revocation; the token stops validating immediately. |

All subcommands accept `--workdir` (default `.`) pointing at the project
root containing `.sdd/`.

```bash
bernstein auth dashboard-token issue --principal alice --scope viewer
bernstein auth dashboard-token list
bernstein auth dashboard-token revoke 3f1a9c2d5e7b0a41
bernstein governance verify dashboard-auth
```

Startup posture: `bernstein gui serve` on a loopback host without any
credential configured issues an operator token and prints it once; on a
non-loopback host it refuses to start until a token or password is
configured. There is no silent open mode on a routable interface. Use the
token as `Authorization: Bearer <token>` or in the dashboard login form
(`POST /dashboard/auth/login`); the session cookie inherits exactly the
token's principal and scope.

## SPIFFE workload identity: `bernstein spiffe`

Infrastructure teams standardizing on [SPIFFE](https://spiffe.io/) workload
identity can consume Bernstein workloads directly. The Ed25519 install identity
and each agent card map onto a deterministic SPIFFE ID; when a SPIRE agent is
present (optional `bernstein[spiffe]` extra) its X.509-SVID is bound to a card
by a receipt anchored in the HMAC audit chain. The self-contained Ed25519 path
stays the default with the extra absent.

SPIFFE ID scheme (deterministic):

```
spiffe://<trust-domain>/bernstein/<install>/<agent>
```

- `<trust-domain>`: operator SPIFFE trust domain (validated, lowercase DNS-like).
- `<install>`: 16-hex fingerprint of the install public key (SHA-256 prefix).
- `<agent>`: the agent card id.

Two operators deriving the id for the same install and agent obtain the same
string, and a verifier re-derives it later to check a card-to-SVID binding.

| Subcommand | Purpose |
|---|---|
| `id --install-key PEM --agent ID --trust-domain TD` | Derive and print the SPIFFE ID offline (pure, no network). |
| `verify-binding BINDING.json --install-key PEM --trust-domain TD [--audit-dir DIR]` | Re-derive the id from the install key and verify a card-to-SVID binding; with `--audit-dir`, also check it against its chained `spiffe.svid_binding` receipt. |

```bash
bernstein spiffe id \
    --install-key .bernstein/keys/agent-card.ed25519.pub \
    --agent backend-1 --trust-domain example.org
# -> spiffe://example.org/bernstein/<install>/backend-1

bernstein spiffe verify-binding binding.json \
    --install-key .bernstein/keys/agent-card.ed25519.pub \
    --trust-domain example.org --audit-dir .sdd/audit
# -> valid (chain-anchored)
```

The card-to-SVID binding is the receipt: `bind_svid_to_card` records a
`spiffe.svid_binding` event pinning the binding content hash, the derived
SPIFFE ID, the install fingerprint, the card hash, and the leaf SVID content
address -- never the SVID private key. A post-hoc tamper to the binding fails
`verify-binding` because its recomputed content hash no longer matches the
chained receipt. SVID material also projects onto the cluster mTLS config, so
the task server enforces mutual TLS through its existing uvicorn `--ssl` path.
See [SPIFFE workload identity](spiffe-workload-identity.md) for an example
SPIRE configuration and threat-model notes.

## In-process verification gate: `bernstein hook-gate`

A gate-capable adapter (Claude Code) wires its worker's `PreToolUse` and `Stop`
hooks to `bernstein hook-gate check`. The command reads the hook event JSON on
stdin, loads the task's persisted policy (`.sdd/runtime/hook_gate/<session>.json`,
written at spawn from the task's `owned_files` and required `evidence_producers`),
and enforces it in-session:

| Event | Behaviour |
| --- | --- |
| `PreToolUse` | A write whose target is outside the task's path allowlist is refused; the refusal is sealed as a gate receipt and the command exits `2` so the tool call never runs. Realpath containment refuses a `..` traversal or an in-scope symlink that resolves outside the worktree. |
| `Stop` | The task's required verification producers run in-session; the attempt is sealed as a proof-of-done receipt and the command exits `2` when a required check failed, so the worker cannot end its turn on red. |

```bash
# Invoked by the worker's hook runner, not by hand:
bernstein hook-gate check --session <id> --event PreToolUse < event.json
bernstein hook-gate check --session <id> --event Stop < event.json
```

Trust model: the in-process gate is defence in depth and a cost optimisation.
The scheduler-side evidence gate stays authoritative and runs regardless. A gate
receipt IS an evidence bundle (`bernstein evidence show` / `verify`,
`bernstein audit verify`), so a verifier cannot tell from the schema whether the
gate fired in-process or scheduler-side. An adapter with no blocking hook surface
injects no gate hooks and degrades to the scheduler-side gate with no policy
weakening.

## `bernstein tournament`

Tournament runs: parallel attempts selected by deterministic evaluators (#2353).

| Command | Description | Source |
| --- | --- | --- |
| `bernstein tournament show <task>` | Render the tournament selection receipt for a task: the winner, the attempt count, the evaluators and tie-break, the spine anchor, and a per-attempt table (rank, attempt hash, score, `chosen`/`sibling` edge). `-w/--workdir` sets the project root. Exit 0 when a receipt exists, 1 when there is none. | `cli/commands/tournament_cmd.py` |
| `bernstein tournament verify <task>` | Recompute a task's tournament selection offline: replay the deterministic scorer over the recorded evaluator outputs, check exactly one chosen edge over the recorded attempts, verify the Ed25519 signature over the canonical binding, verify the tournament lineage spine, and re-anchor the receipt. A tampered score or a hand-picked winner diverges from the replay and fails. Exit 0 verified, 1 no receipt, 2 mismatch. `bernstein audit verify` runs the same check across every receipt. | `cli/commands/tournament_cmd.py` |

Selection is a pure function of the evaluator outputs (test pass rate, lint
status, coverage delta, mutation score, arbitrary commands) with a stable
attempt-hash tie-break, so replaying the run reproduces the identical decision.
Fan-out is gated on the task's existing per-ticket budget ceiling and aborts
with a clear error before spawning when projected spend would breach the cap.
