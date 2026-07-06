# CLI Reference

Bernstein ships **164 CLI commands** registered in `cli/main.py`. This page is the single-source reference for every flag on every visible command. For driving Bernstein from a script, also read [`cli/task-lifecycle.md`](cli/task-lifecycle.md) and [`cli/replay.md`](cli/replay.md).

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
| `--cli {claude\|codex\|gemini\|qwen\|auto}` | none | Force a specific agent (overrides auto-detection). |
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

The 164 commands are organised below by purpose, not alphabetically. Use the table inside each category for quick lookup; the longer per-command entries follow for the highest-traffic commands.

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
| `bernstein quickstart` | Zero-config Flask TODO API demo. | `cli/quickstart_cmd.py` |
| `bernstein demo` | 60-second zero-to-running demo. | `cli/run_confirm.py:demo` |
| `bernstein cook` | Run a recipe (multi-stage demo). | `cli/run_confirm.py:cook` |
| `bernstein init` | Initialize project (`.sdd/` + `bernstein.yaml`). | `cli/run_bootstrap.py:394` |
| `bernstein init-wizard` | Interactive project setup. | `cli/init_wizard_cmd.py` |
| `bernstein dry-run` | Preview the plan without spawning. | `cli/commands/dry_run_cmd.py:203` |
| `bernstein replay RUN_ID` | Replay a past run step-by-step. | `cli/commands/advanced_cmd.py:876` |
| `bernstein replay-filter RUN_ID` | Replay with `--filter` / `--event-type` / `--agent` / `--search`. | `cli/commands/replay_filter_cmd.py:164` |
| `bernstein undo` | Undo the last operation. | `cli/undo_cmd.py:15` |
| `bernstein checkpoint` | Save progress for later resume. | `cli/commands/checkpoint_cmd.py:49` |
| `bernstein wrap-up` | End session with summary + learnings. | `cli/wrap_up_cmd.py` |
| `bernstein fork --run ID --from-step N` | Rewind a run to journal step N and branch a new run from its content-addressed worktree snapshot. | `cli/commands/fork_cmd.py` |

#### `bernstein run`

Execute a plan file (or start orchestration with no plan).

**Synopsis:** `bernstein run [PLAN_FILE] [flags]`

The full flag list is large (28 flags inherited from the root group and re-exposed; see `cli/run_bootstrap.py:533+`). Most commonly used:

| Flag | Default | Meaning |
|---|---|---|
| `PLAN_FILE` | none | A YAML plan to execute. Optional. |
| `--budget USD` | 0.0 | Cost cap. 0 = unlimited. |
| `--max-cost-usd N` | unset | Hard cap on cumulative routed model spend; aborts the run when crossed. Sets `BERNSTEIN_MAX_COST_USD`. |
| `--cli` | auto | Force agent (claude/codex/gemini/qwen/auto). |
| `--model` | none | Force a specific model. |
| `--approval {auto\|review\|pr}` | auto | Approval gate. |
| `--merge {pr\|direct}` | pr | Merge strategy. |
| `--dry-run` | off | Preview without spawning. |
| `--plan-only` | off | Show plan, do not run agents. |
| `--auto-pr` | off | Auto-open a GitHub PR on completion. |
| `--task PATTERN` | none | Run only matching backlog tasks. |
| `--port N` | 8052 | Task server port. |
| `-v / -q` | off | Verbosity. |

`--max-cost-usd` is a hard cap, separate from the soft `--budget`
threshold model. It writes the value to `BERNSTEIN_MAX_COST_USD`
before bootstrap; the orchestrator drains live agents and aborts
when cumulative routed spend crosses the threshold. Precedence is
`BERNSTEIN_MAX_COST_USD` > `run_config.json` > `seed.budget_usd`
> default (0 = unlimited). Non-positive values normalise to 0.

#### `bernstein stop`

Graceful or force stop.

| Flag | Default | Meaning |
|---|---|---|
| `--force` / `--hard` | off | Hard stop: kill processes immediately. |

`bernstein stop` (no flag) sends `SIGTERM` to the orchestrator and waits for agents to finish their current step and persist artefacts. `bernstein stop --force` terminates everything immediately and runs orphan-recovery on the next start.

#### `bernstein cancel`

See [`cli/task-lifecycle.md#bernstein-cancel`](cli/task-lifecycle.md#bernstein-cancel).

#### `bernstein cleanup`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root. |
| `--worktrees` | off | Remove orphan worktrees. |
| `--logs` | off | Truncate `.sdd/logs/` and `.sdd/runtime/*.log`. |
| `--yes` | off | Skip confirmation. |

#### `bernstein replay` / `bernstein replay-filter`

See [`cli/replay.md`](cli/replay.md) for full reference.

#### `bernstein checkpoint`

| Flag | Default | Meaning |
|---|---|---|
| `--goal TEXT` | none | Goal label embedded in the checkpoint. |

Snapshots `.sdd/` state so a later `bernstein run` can resume from it.

#### `bernstein wrap-up`

End a session with a summary, retrospective, and learning capture. Hides under no flags; useful at the end of a long-running orchestration.

#### `bernstein init` / `bernstein init-wizard`

| Flag | Default | Meaning |
|---|---|---|
| `--here` | off | Initialize in the current directory (no subdir created). |
| `--name TEXT` | dirname | Project name. |
| `--force` | off | Overwrite existing `bernstein.yaml`. |

`init-wizard` adds an interactive prompt flow (project type, default agent, budget, etc.) and is preferred for first-time users.

---

## Plan & tasks

| Command | Purpose | Source |
|---|---|---|
| `bernstein plan` | Show the task backlog. | `cli/commands/task_cmd.py:454` |
| `bernstein plan generate "<goal>"` | Generate a plan YAML. | `cli/plan_generate_cmd.py` |
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
| `bernstein verify` | Run quality gates manually. | `cli/verify_cmd.py` |
| `bernstein delegate` | Assign a task to a specific agent. | `cli/delegate_cmd.py:22` |
| `bernstein from-ticket FILE` | Generate tasks from a ticket file. | `cli/commands/ticket_cmd.py:231` |
| `bernstein ticket` | Ticket integration group. | `cli/commands/ticket_cmd.py:246` |
| `bernstein validate PLAN.yaml` | Validate a plan file's schema. | `cli/plan_validate_cmd.py:142` |

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
| `GOAL` | required | Goal description (positional). |
| `--out FILE` | `plan.yaml` | Output path. |
| `--model NAME` | auto | Model used to draft the plan. |

#### `bernstein add-task`

See [`cli/task-lifecycle.md#bernstein-add-task`](cli/task-lifecycle.md#bernstein-add-task).

#### `bernstein review`

See [`cli/task-lifecycle.md#bernstein-review--bernstein-verify`](cli/task-lifecycle.md#bernstein-review--bernstein-verify).

#### `bernstein delegate`

| Flag | Default | Meaning |
|---|---|---|
| `--role ROLE` | required | Which agent role to assign. |
| `--task TEXT` | required | Task description. |
| `--cli {claude\|codex\|gemini\|qwen}` | auto | Force a specific agent. |

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
| `bernstein commit-stats` | Per-run git diff stats. | `cli/commands/status_cmd.py:914` |
| `bernstein report` | Build a custom report. | `cli/report_cmd.py` |
| `bernstein slo` | SLO dashboard. | `cli/slo_cmd.py:191` |
| `bernstein trace TASK_ID` | Step-by-step trace. | `cli/commands/advanced_cmd.py:666` |
| `bernstein incident` | Open an incident report. | `cli/incident_cmd.py:53` |
| `bernstein postmortem` | Failed-task postmortem. | `cli/postmortem_cmd.py:12` |

#### `bernstein status`

Compact one-screen project view.

| Flag | Default | Meaning |
|---|---|---|
| `--json` | off | Emit JSON. |
| `--workdir` | `.` | Project root. |

#### `bernstein live`

| Flag | Default | Meaning |
|---|---|---|
| `--interval SEC` | 2.0 | Polling interval. |
| `--classic` | off | Use the simpler Rich Live display. |
| `--no-splash` | off | Skip the startup splash. |

The default is the 3-column Textual TUI: Agents | Tasks | Activity feed. `--classic` falls back to a single-pane Rich Live view.

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
| `list` | none | List all agent log files. |
| `show NAME` | none | Print one specific log. |

`bernstein logs` (no subcommand) is equivalent to `bernstein logs tail`.

#### `bernstein recap`

| Flag | Default | Meaning |
|---|---|---|
| `--archive PATH` | `.sdd/archive/tasks.jsonl` | Path to task archive. |
| `--as-json` | off | Emit raw JSON. |

#### `bernstein retro`

| Flag | Default | Meaning |
|---|---|---|
| `--since HOURS` | all | Hours back to include. |
| `-o, --output FILE` | `.sdd/runtime/retrospective.md` | Output path. |
| `--print` | off | Also print to stdout. |
| `--archive PATH` | `.sdd/archive/tasks.jsonl` | Source archive. |

#### `bernstein watch`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root. |
| `--filter PATTERN` | none | Show only events matching PATTERN. |
| `--task TASK_ID` | none | Watch only a specific task. |

#### `bernstein trace`

| Flag | Default | Meaning |
|---|---|---|
| `TASK_ID` | required | Task to trace. |
| `--as-json` | off | Emit raw JSON. |
| `--traces-dir DIR` | `.sdd/traces` | Directory containing trace files. |

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
| `--reset` | off | Reset SLO budget (server endpoint requires auth). |

---

## Quality & autofix

| Command | Purpose | Source |
|---|---|---|
| `bernstein verify` | Run quality gates manually. | `cli/verify_cmd.py` |
| `bernstein autofix` | Auto-repair CI failures (group). | `cli/commands/autofix_cmd.py:172` |
| `bernstein ci` | CI integration commands (group). | `cli/commands/ci_cmd.py:49` |
| `bernstein chaos` | Chaos engineering (group). | `cli/commands/chaos_cmd.py:32` |
| `bernstein eval` | Evaluation pipelines (group). | `cli/commands/eval_benchmark_cmd.py:426` |
| `bernstein benchmark` | Benchmark pipelines (group). | `cli/commands/eval_benchmark_cmd.py:29` |
| `bernstein api-check` | Detect breaking-API changes. | `cli/api_check_cmd.py:22` |
| `bernstein dep-impact` | Dependency change impact. | `cli/dep_impact_cmd.py:25` |
| `bernstein diff` | Task-state diff. | `cli/diff_cmd.py:504` |

#### `bernstein autofix`

| Subcommand | Purpose |
|---|---|
| `start` | Start the autofix daemon (watches PRs, repairs CI failures). |
| `stop` | Stop the daemon. |
| `status` | Show daemon status + recent activity. |
| `run PR` | Single-shot autofix on a specific PR. |

`bernstein autofix start` flags include `--workdir`, `--server URL`, `--poll SEC`, `--max-attempts N`, `--token`. See `cli/commands/autofix_cmd.py:172-200` for full list.

#### `bernstein ci`

| Subcommand | Purpose |
|---|---|
| `tail RUN_URL` | Tail a GitHub Actions run, surface failing tests. |
| `watch REPO` | Watch a repo for CI failures and create autofix tasks. |
| `summarize` | Summarize recent CI runs. |

Common flags: `--token` (env: `GITHUB_TOKEN`), `--server`, `--interval`. (`cli/commands/ci_cmd.py:49+`.)

#### `bernstein chaos`

| Subcommand | Purpose |
|---|---|
| `agent-kill` | Kill a random or specific agent. |
| `rate-limit` | Simulate provider rate-limit. |
| `file-remove` | Delete files matching a glob. |
| `pause-agent` | Pause an agent for N seconds. |
| `status` | Show recent chaos events. |
| `slo` | SLO impact of recent chaos events. |

Most subcommands accept `--agent-id`, `--duration`, `--pattern` as relevant. (`cli/commands/chaos_cmd.py:32+`.)

#### `bernstein eval` / `bernstein benchmark`

The two groups share most flags:

| Flag | Default | Meaning |
|---|---|---|
| `--subset NAME` | full | Dataset subset (`lite`, `full`, etc.). |
| `--sample N` | none | Random sample of N instances. |
| `--instance ID` | none | Single instance by ID. |
| `--dataset PATH` | none | Local JSONL dataset file. |
| `--workdir DIR` | `.` | Project root. |
| `--save / --no-save` | save | Persist results to disk. |
| `--compare` | off | Compare against the previous run. |

`bernstein eval run` is the typical command for SWE-bench-style evaluations; `bernstein benchmark run` for Bernstein-internal performance benchmarks. See `cli/commands/eval_benchmark_cmd.py:127+` and `:426+`.

#### `bernstein api-check`

| Flag | Default | Meaning |
|---|---|---|
| `--baseline REF` | `origin/main` | Git ref for the baseline schema. |
| `--head REF` | `HEAD` | Git ref for the candidate schema. |
| `--threshold {patch\|minor\|major}` | minor | Maximum allowed delta. |

#### `bernstein dep-impact`

| Flag | Default | Meaning |
|---|---|---|
| `--package NAME` | required | Package whose version change to analyse. |
| `--from VERSION` | required | Old version. |
| `--to VERSION` | required | New version. |

#### `bernstein diff`

Show what changed between two task states.

| Flag | Default | Meaning |
|---|---|---|
| `TASK_A` | required | First task ID. |
| `TASK_B` | required | Second task ID. |
| `--unified N` | 3 | Unified-diff context lines. |

---

## Adapters & agents

| Command | Purpose | Source |
|---|---|---|
| `bernstein agents` | Agent catalog ops (group). | `cli/commands/agents_cmd.py:22` |
| `bernstein test-adapter` | Spawn one adapter to verify its plumbing. | `cli/adapter_cmd.py:84` |
| `bernstein delegate` | Assign a task to a specific agent. | `cli/delegate_cmd.py:22` |
| `bernstein worker` | Join a cluster as a remote worker node. | `cli/worker_cmd.py:371` |
| `bernstein evolve` | Self-improvement loop. | `cli/evolve_cmd.py:48` |

#### `bernstein agents`

| Subcommand | Purpose |
|---|---|
| `list` | Available agents and capabilities (`--show-all` includes unregistered). |
| `sync` | Pull the latest agent catalog. |
| `validate` | Validate the local catalog. |
| `showcase` | Print example invocations for each agent. |
| `match` | `--role X` `--task TEXT` - show which agent best matches. |
| `sandbox-backends` | List available sandbox backends. |
| `discover` | Auto-detect installed CLI agents. `--net` also searches GitHub/npm. |

#### `bernstein test-adapter`

| Flag | Default | Meaning |
|---|---|---|
| `ADAPTER_NAME` | required | Adapter to test (e.g. `claude`, `codex`). |
| `--model NAME` | adapter default | Force a specific model. |
| `--prompt TEXT` | smoke test | Prompt to send. |
| `--timeout SEC` | 60 | Hard timeout. |

#### `bernstein worker`

| Flag | Default | Meaning |
|---|---|---|
| `--server URL` | env `BERNSTEIN_SERVER_URL` | Cluster head node URL. |
| `--token TOKEN` | env `BERNSTEIN_AUTH_TOKEN` | JWT for cluster auth. |
| `--name NAME` | hostname | Worker display name. |
| `--max-agents N` | 4 | Max concurrent agents on this worker. |
| `--labels K=V` | none | Selector labels (repeatable). |

See [`operations/cluster-mode.md`](../operations/cluster-mode.md) for the full setup walkthrough.

#### `bernstein evolve`

| Flag | Default | Meaning |
|---|---|---|
| `--budget USD` | 0.0 | Cost cap. |
| `--max-cycles N` | 0 | Max iterations. |
| `--interval SEC` | 300 | Seconds between cycles. |
| `--github` | off | Sync proposals as GitHub Issues. |
| `--yes` | off | Skip the safety confirmation. |

`bernstein evolve` is hidden behind a confirmation prompt by default - see the safety guard at `cli/main.py:455`.

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

#### `bernstein plugins`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root. |

Lists plugins in `.bernstein/plugins/<name>/meta.json`.

#### `bernstein skills`

| Subcommand | Purpose |
|---|---|
| `list` | List installed skills. `--source local\|registry` filter. |
| `load NAME` | Load a skill by name. `--reference FILE` / `--script FILE` to override the default entry. |
| `install NAME` | Install a skill from the registry. |
| `uninstall NAME` | Remove an installed skill. |

(`cli/commands/skills_cmd.py:13-81`.)

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
| `list` | List prompt templates. |
| `show NAME` | Show a prompt's content. |
| `versions NAME` | List versions of a prompt. |
| `diff NAME V1 V2` | Diff two versions. `--json` for machine output. |
| `rollback NAME VERSION` | Roll a prompt back to VERSION. |
| `bandit NAME V_A V_B` | Run an A/B bandit on two versions. `--split FRACTION` (default 0.5). |
| `delete NAME` | Delete a prompt template. |

#### `bernstein manifest`

| Subcommand | Purpose |
|---|---|
| `show RUN_ID` | Show the manifest for a run. |
| `compare RUN_A RUN_B` | Compare two run manifests. |
| `verify RUN_ID` | Re-compute and verify the manifest hash. |

#### `bernstein templates`

| Subcommand | Purpose |
|---|---|
| `list` | List available templates. |
| `show TEMPLATE [OUTPUT]` | Print template content (or write to OUTPUT). |
| `apply TEMPLATE` | Apply a template to the current project. `--dest DIR`, `--force`. |
| `compress ROLE\|--all` | Operator-gated LLM compression of role prompt templates (`cli/commands/templates_cmd.py`). Rewrite via the configured adapter (`--model`, `--provider`), then mechanical validators (fenced blocks, headings, URLs, inline code, placeholders, completion-contract block; at most two targeted fix retries), then apply. Originals are stored under `~/.local/share/bernstein/template-backups/` keyed by content hash with readback verification; the receipt `{role, pre_sha256, post_sha256, pre_tokens, post_tokens, validators, adapter, model}` is chained to the audit log and a `templates.lock` row lets `bernstein team drift` classify the change as intentional. Prints only the template token delta; per-spawn savings come from `bernstein cost --by role`. `--workdir DIR`, `--yes` skips the confirmation. |
| `restore ROLE` | Reverse the most recent receipted compression byte-identically (backup hash, on-disk hash, and directory digest all verified). `--workdir DIR`. |

---

## Cloud & cluster

| Command | Purpose | Source |
|---|---|---|
| `bernstein cloud` | Cloudflare cloud agent ops (group). | `cli/commands/cloud_cmd.py:35` |
| `bernstein worker` | Join a cluster as worker (see [Adapters & agents](#adapters--agents)). | `cli/worker_cmd.py:371` |
| `bernstein gateway` | Gateway mgmt (group). | `cli/commands/gateway_cmd.py:28` |
| `bernstein tunnel` | Tunnel mgmt (group). | `cli/commands/tunnel_cmd.py:62` |
| `bernstein remote` | Remote-host execution (group). | `cli/commands/remote_cmd.py:52` |
| `bernstein connect` | Connect to a remote Bernstein server. | `cli/commands/creds_cmd.py:95` |
| `bernstein fleet` | Multi-project supervision (group). | `cli/commands/fleet_cmd.py:50` |

#### `bernstein cloud`

| Subcommand | Purpose |
|---|---|
| `setup` | Configure Cloudflare credentials. |
| `run GOAL` | Run an agent on Cloudflare Workers. `--max-agents N`, `--model`, `--budget USD`, `--wait/--no-wait`. |
| `status [RUN_ID]` | Status of a cloud run. |
| `runs` | Recent cloud runs. `--limit N`, `--json`. |
| `costs` | Cloud spend. `--period current\|YYYY-MM`. |
| `init` | Generate `wrangler.toml`. `--worker-name`, `-o FILE`. |
| `deploy` | Deploy the Worker. `--worker-name`. |

(`cli/commands/cloud_cmd.py:35+`.)

#### `bernstein gateway`

| Subcommand | Purpose |
|---|---|
| `start` | Start the gateway server. |
| `stop` | Stop the gateway. |
| `status` | Show gateway status. |
| `routes` | Show active routes. |

#### `bernstein tunnel`

| Subcommand | Purpose |
|---|---|
| `open PORT` | Open a tunnel to PORT. `--name NAME`, `--provider {cloudflared\|ngrok}`. |
| `list` | List active tunnels. |
| `close [NAME]` | Close one tunnel. `--all` closes every active tunnel. |

(`cli/commands/tunnel_cmd.py:62-117`.)

#### `bernstein remote`

| Subcommand | Purpose |
|---|---|
| `run HOST` | Spawn an agent on a remote host. `--user`, `--port`, `--key-file`. |
| `sync HOST PATH` | Rsync a path to a remote host. `--user`, `--port`, `--exclude`, `--delete`. |
| `disconnect HOST` | Close any open SSH multiplexing channel for HOST. |

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
| `list` | Projects in the fleet. |
| `add PATH` | Add a project to the fleet. |
| `remove NAME` | Remove a project from the fleet. |
| `dashboard` | Open the fleet web dashboard. |

(`cli/commands/fleet_cmd.py:50+`.)

---

## Auth & security

| Command | Purpose | Source |
|---|---|---|
| `bernstein login` | Log in (alias for `auth login`). | `cli/commands/auth_cmd.py:auth_login` |
| `bernstein auth` | Auth ops (group). | `cli/commands/auth_cmd.py:139` |
| `bernstein creds` | Credential mgmt (group). | `cli/commands/creds_cmd.py:214` |
| `bernstein users` | User mgmt (group). | `cli/commands/users_cmd.py:57` |
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
| `bernstein approve-tool` | Approve a tool-call request. | `cli/commands/approval_cmd.py:approve_tool_cmd` |
| `bernstein reject-tool` | Reject a tool-call request. | `cli/commands/approval_cmd.py:reject_tool_cmd` |
| `bernstein review-receipt` | Attested PR review receipts binding issue / plan / tool calls / diff (group): `emit` / `verify`. | `cli/commands/review_receipt_cmd.py` |
| `bernstein gate verify <run>` | Verify a maker-checker / judge-panel gate's signed adjudication record: recompute `inputs_hash` from `--inputs` and confirm the panel saw exactly those inputs, then confirm the spine anchor still verifies. Exit 1 when no record, 2 on mismatch. | `cli/commands/gate_cmd.py` |
| `bernstein governance verify <run>` | Recompute every RBAC access and per-subject budget decision recorded for a run from the signed spine and confirm the recorded verdicts: re-resolve roles from the signed `--bindings`, re-project spend from the `--ledger`, and match. Exit 1 when no records, 2 on mismatch. | `cli/commands/governance_cmd.py` |

> Task-level `approve` / `reject` are different commands - see [Plan & tasks](#plan--tasks).

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
| `logout` | Drop the local session. |
| `whoami` | Print the logged-in identity. |
| `token list` | List active tokens. |
| `token revoke ID` | Revoke a token. |

#### `bernstein creds`

| Subcommand | Purpose |
|---|---|
| `list` | List configured credentials. |
| `add PROVIDER` | Add credentials for a provider. |
| `remove PROVIDER` | Remove credentials. |
| `rotate PROVIDER` | Rotate stored credentials. |
| `test PROVIDER` | Verify credentials work. |

(`cli/commands/creds_cmd.py:214-282`.)

#### `bernstein users`

| Subcommand | Purpose |
|---|---|
| `list` | List users (auth required). |
| `add EMAIL` | Add a user. `--role` (admin/user/viewer), `--name`. |
| `remove EMAIL` | Remove a user. |
| `whoami` | Print the current user. |

#### `bernstein policy`

| Subcommand | Purpose |
|---|---|
| `list` | List active policies. |
| `show NAME` | Show one policy's contents. |
| `apply FILE` | Apply a policy YAML. |
| `remove NAME` | Remove a policy. |

#### `bernstein compliance`

| Subcommand | Purpose |
|---|---|
| `report` | Generate a compliance report. `--workdir`, `--json-output`. |
| `assess FRAMEWORK` | Assess against a framework (`eu_ai_act`, `hipaa`, `soc2`). |
| `evidence FRAMEWORK` | Export evidence package. `--version`. |
| `controls FRAMEWORK` | Export controls. `--json-output`. |

(`cli/commands/compliance_cmd.py:26+`.)

#### `bernstein audit`

| Subcommand | Purpose |
|---|---|
| `tail` | Recent audit events. `--limit N`. |
| `verify` | Verify audit log integrity. `--merkle-only`, `--hmac-only`. |
| `anchor` | Anchor the Merkle root as a git tag. `--anchor-git`. |
| `export PERIOD` | Export evidence for a period. `--output DIR`, `--dir WORKDIR`. Tenant-scoped slice via `--tenant`. |
| `slice` | Write a deterministic JSONL subset between two HMAC anchors. `--from`, `--to`, `-o PATH`. |
| `query` | Query audit events. `--event-type`, `--actor`, `--since`, `--limit`. |

(`cli/commands/audit_cmd.py:25+`. The `slice` verb is the
deterministic-subset extractor described in
[HMAC-chained audit log](../security/audit-log.md#slicing-a-deterministic-subset).)

#### `bernstein lineage`

| Subcommand | Purpose |
|---|---|
| `verify RUN_ID` | Verify the run's lineage spine: recompute the full Merkle hash chain and every HMAC tag, print the head hash. `--workdir DIR`. Exit 0 = OK, 1 = no entries, 2 = tamper. |
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

| Subcommand | Purpose |
|---|---|
| `list` | List quarantined tasks. |
| `clear` | Clear all quarantined tasks. `--confirm` to skip prompt. |

(`cli/commands/advanced_cmd.py:1120-1174`.)

#### `bernstein approve-tool` / `bernstein reject-tool`

Tool-call approval gate. When an agent requests a sensitive tool call (network egress, file write outside its worktree, exec outside its sandbox), the orchestrator pauses and writes a request to `.sdd/runtime/tool_approvals/`. Resolve with these commands.

```bash
bernstein approve-tool <request_id>
bernstein reject-tool  <request_id>
```

---

## Cost & tokens

| Command | Purpose | Source |
|---|---|---|
| `bernstein cost` | Spend breakdown by model / task. | `cli/commands/cost.py:540` |
| `bernstein cost profile-report` | Content-addressed per-profile cost report, appended to the audit chain. | `cli/commands/cost.py` |
| `bernstein estimate` | Estimate cost before running. | `cli/commands/cost.py:388` |
| `bernstein token-report` | Token usage breakdown. | `cli/token_cmd.py` |

#### `bernstein cost`

| Flag | Default | Meaning |
|---|---|---|
| `--period {today\|week\|month\|all}` | week | Time window. |
| `--by {model\|role\|task\|profile\|...}` | model | Group-by dimension. `profile` groups by response-style profile; tasks whose profile changed mid-run appear as an explicit excluded bucket. |
| `--json` | off | Emit JSON. |

#### `bernstein cost profile-report`

| Flag | Default | Meaning |
|---|---|---|
| `--last {1h\|24h\|7d\|30d}` | whole ledger | Ledger window. |
| `--ledger PATH` | `.sdd/cost/ledger.jsonl` | Spend ledger to compute from. |
| `--json` | off | Emit JSON. |

Emits per-profile tasks / output tokens / USD / mean tokens per task plus
joined verification pass rates. The artifact is canonical JSON named by its
own SHA-256, embeds the ledger line-hash range it was computed from, and is
appended to the audit chain, so anyone holding the ledger can recompute it
byte-identically. Cross-profile savings are only claimed when both profiles
have at least 5 tasks with the same role and model; otherwise the report
states "insufficient comparable runs".

#### `bernstein estimate`

| Flag | Default | Meaning |
|---|---|---|
| `--plan FILE` | `bernstein.yaml` | Plan to estimate. |
| `--model NAME` | auto | Override model. |
| `--detailed` | off | Per-task breakdown. |

#### `bernstein token-report`

| Flag | Default | Meaning |
|---|---|---|
| `--period {today\|week\|month}` | week | Time window. |
| `--by {model\|session}` | model | Group-by. |
| `--json` | off | Emit JSON. |

---

## Maintenance & debug

| Command | Purpose | Source |
|---|---|---|
| `bernstein cleanup` | Clean worktrees / logs. | `cli/maintenance_cmd.py:162` |
| `bernstein daemon` | systemd / launchd unit (group). | `cli/commands/daemon_cmd.py:76` |
| `bernstein dr` | Disaster recovery (group). | `cli/commands/disaster_recovery_cmd.py:12` |
| `bernstein debug-bundle` | Bug-report bundle. | `cli/debug_cmd.py:81` |
| `bernstein debug` | (alias of debug-bundle) | `cli/main.py:825` |
| `bernstein doctor` | Self-diagnostics. | `cli/doctor_cmd.py:281` |
| `bernstein self-update` | Upgrade Bernstein. | `cli/self_update_cmd.py:189` |
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

#### `bernstein doctor`

| Flag | Default | Meaning |
|---|---|---|
| `--json` | off | Emit raw JSON. |
| `--fix` | off | Attempt to auto-fix issues. |

(`cli/commands/advanced_cmd.py:536-550` re-exposes `cli/status_cmd.py:doctor`.)

#### `bernstein debug-bundle`

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root. |
| `--out FILE` | `debug-bundle-<ts>.zip` | Output zip path. |
| `--include-logs` | on | Include `.sdd/logs/`. |
| `--include-secrets` | off | (NOT recommended) include credential blobs. |

#### `bernstein self-update`

| Flag | Default | Meaning |
|---|---|---|
| `--channel {stable\|beta}` | stable | Release channel. |
| `--check-only` | off | Print available version, do not install. |

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
| `show` | Print effective config. |
| `set KEY VALUE` | Update a config value. |
| `unset KEY` | Remove a config value. |
| `validate` | Validate the config. |

#### `bernstein workspace`

| Subcommand | Purpose |
|---|---|
| `list` | Active worktrees. |
| `clean` | Clean up old worktrees. |
| `show NAME` | Show a worktree's metadata. |

#### `bernstein session`

| Subcommand | Purpose |
|---|---|
| `list` | List saved sessions. |
| `resume NAME` | Resume a saved session. |
| `save NAME` | Save current state as a session. |
| `delete NAME` | Delete a session. |

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
| `list` | List cache entries. `--workdir`, `--limit`, `--json`. |
| `show TASK_ID` | Show one cache entry. `--workdir`, `--json`. |
| `clear` | Clear the cache. `--workdir`, `--scope`, `--yes`. |

(`cli/commands/cache_cmd.py:45-146`.)

#### `bernstein notify`

| Subcommand | Purpose |
|---|---|
| `send` | Send a one-off notification. `--driver {slack\|telegram\|discord\|email\|webhook\|shell}`, `--message`, `--target`. |
| `test DRIVER` | Smoke-test a driver. |
| `list` | List configured drivers. |

(`cli/commands/notify_cmd.py:63+`.)

#### `bernstein triggers`

| Subcommand | Purpose |
|---|---|
| `list` | Configured trigger sources. `-n LIMIT`. |
| `enable NAME` | Enable a trigger. |
| `disable NAME` | Disable a trigger. |
| `test NAME` | Smoke-test a trigger. |

#### `bernstein dr`

Disaster recovery; see [`operations/disaster-recovery.md`](../operations/disaster-recovery.md).

| Subcommand | Purpose |
|---|---|
| `snapshot` | Create a state snapshot. |
| `restore SNAPSHOT_ID` | Restore from a snapshot. |
| `list` | List snapshots. |
| `verify` | Verify snapshot integrity. |

#### `bernstein daemon`

systemd / launchd unit installer.

| Subcommand | Purpose |
|---|---|
| `install` | Install the unit. `--user` / `--system`, `--workdir`. |
| `uninstall` | Remove the unit. |
| `status` | Show daemon status. |
| `start` / `stop` / `restart` | Control daemon lifecycle. |

(`cli/commands/daemon_cmd.py:76+`.)

#### `bernstein man-pages`

| Flag | Default | Meaning |
|---|---|---|
| `--out DIR` | `./man` | Output directory. |
| `--section N` | 1 | Manpage section. |

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
| `--server URL` | none | Upstream Bernstein server (default: localhost). |

#### `bernstein mcp catalog`

See [`reference/mcp-catalog.md`](mcp-catalog.md) for the full reference.

#### `bernstein chat`

| Subcommand | Purpose |
|---|---|
| `start` | Start a chat-control bridge. `--driver {telegram\|slack\|discord}`, `--token`, `--target`. |
| `stop` | Stop the bridge. |
| `status` | Show bridge status. |

#### `bernstein hooks`

| Subcommand | Purpose |
|---|---|
| `list` | Installed hooks. |
| `install NAME` | Install a hook (e.g. `smart_approve`). |
| `uninstall NAME` | Remove a hook. |
| `test NAME` | Smoke-test a hook. |

#### `bernstein pr`

| Flag | Default | Meaning |
|---|---|---|
| `--repo OWNER/NAME` | git remote | Target repo. |
| `--base BRANCH` | main | Base branch. |
| `--head BRANCH` | current | Head branch. |
| `--title TEXT` | task summary | PR title. |
| `--body TEXT` | task description | PR body. |
| `--draft` | off | Open as a draft PR. |

(`cli/commands/pr_cmd.py:183-220`.)

#### `bernstein review-responder`

| Subcommand | Purpose |
|---|---|
| `start` | Start the review-responder daemon. `--workdir`, `--server`, `--poll`. |
| `stop` | Stop the daemon. |
| `status` | Show daemon status. |
| `run PR` | Single-shot review-respond on one PR. |

#### `bernstein preview`

| Subcommand | Purpose |
|---|---|
| `start` | Start a preview server in the current task's worktree. `--port`, `--command`, `--public`, `--name`, `--ttl`. |
| `list` | List active previews. `--json`. |
| `show ID` | Show a preview's URL and process. `--json`. |
| `stop [ID]` | Stop one preview. `--all` stops every active preview. |

(`cli/commands/preview_cmd.py:46-220`.)

---

## Misc

| Command | Purpose | Source |
|---|---|---|
| `bernstein explain CONCEPT` | Concept explainer. | `cli/explain_help_cmd.py:171` |
| `bernstein help-all` | Comprehensive help screen. | `cli/commands/advanced_cmd.py:378` |
| `bernstein ideate` | Generate improvement ideas. | `cli/commands/advanced_cmd.py:393` |
| `bernstein aliases` | Show CLI aliases. | `cli/aliases.py` |
| `bernstein fingerprint` | Replay verification (group). | `cli/fingerprint_cmd.py:37` |
| `bernstein graph` | Dependency graph (group). | `cli/graph_cmd.py:19` |
| `bernstein profile` | Task profiling. | `cli/profile_cmd.py:73` |
| `bernstein evolve` | Self-improvement loop (see [Adapters & agents](#adapters--agents)). | `cli/evolve_cmd.py:48` |
| `bernstein changelog` | Generate a CHANGELOG entry. | `cli/changelog_cmd.py:314` |
| `bernstein run-changelog` | Changelog from runs. | `cli/run_changelog_cmd.py:25` |
| `bernstein checkpoint` | Save progress (see [Run & control](#run--control)). | `cli/commands/checkpoint_cmd.py:49` |
| `bernstein voice` / `bernstein listen` | Voice control (experimental). | `cli/voice_cmd.py:437` |
| `bernstein install-hooks` | Install git hooks. | `cli/commands/advanced_cmd.py:448` |
| `bernstein ab-test` | A/B model comparison. | `cli/commands/ab_test_cmd.py:14` |
| `bernstein acp serve` | Run an ACP server. | `cli/commands/acp_cmd.py:33` |
| `bernstein scaffold "<prompt>"` | Bootstrap a project from a prompt. | `cli/commands/scaffold_cmd.py` |
| `bernstein test` | Run the project's test suite. | `cli/test_cmd.py:13` |
| `bernstein wiki build` | Render `WIKI.md` from the AST symbol graph. | `cli/commands/wiki_cmd.py` |
| `bernstein workflow` | Workflow mgmt (group). | `cli/workflow_cmd.py:15` |
| `bernstein replay RUN_ID --verify` / `--from-step N` | Recompute the run journal's Merkle head and report the first divergent step (writes `divergence_report.json`), or rebuild deterministic state to step N. | `cli/commands/advanced_cmd.py` |
| `bernstein thread verify --run <id>` | Prove the live event stream equals the run journal: recompute the journal's Merkle chain and confirm every streamed event carries the byte-identical entry hash. `--json` for machine output. Exit 1 on divergence, 2 when the run journal is missing. | `cli/commands/thread_cmd.py` |
| `bernstein webhook verify <event_id>` | Verify an audited webhook node's signed receipts: recompute the inbound event hash and the outbound result hash against the run journal, re-check both Ed25519 signatures offline, and re-anchor both receipts against the webhook-node lineage spine. Exit 1 when no receipt / no outbound yet, 2 on tamper. | `cli/commands/webhook_cmd.py` |
| `bernstein escalation show <id>` | Print the operator projection of a stall escalation receipt: stall reason, deterministic recommended action, resume fork point, and spine anchor. `--json` for machine output. Exit 1 when no receipt matches the id. | `cli/commands/escalation_cmd.py` |
| `bernstein escalation verify <id>` | Reconstruct the trailing failure window from the run journal, walk the journal's Merkle chain, and confirm every bound entry hash matches the receipt (plus the Ed25519 signature and spine anchor). Exit 0 verified, 1 no receipt, 2 mismatch (a tampered journal entry inside the window). | `cli/commands/escalation_cmd.py` |
| `bernstein schedule show <id> --at <time>` / `bernstein schedule verify` | Project a recurring fire onto a canonical task graph. `show --at <epoch-or-ISO8601>` prints the deterministic graph hash the schedule would dispatch at that instant without firing (no journal, receipt, or `last_fire_at` mutation). `verify` replays every recorded fire and confirms its graph hash reproduces byte-identically from `(schedule, fire_time, state)`; `--json` for machine output, exit 1 on any mismatch. RFC-5545 `RRULE` and cron are both accepted; a webhook / file-change trigger binds its event as an input hash. | `cli/commands/schedule_cmd.py` |

#### `bernstein ab-test`

| Flag | Default | Meaning |
|---|---|---|
| `--model-a NAME` | required | First model. |
| `--model-b NAME` | required | Second model. |
| `--task FILE` | required | Task file or backlog ID. |
| `--runs N` | 5 | Repeats per model. |
| `--metric {success\|cost\|latency}` | success | What to compare on. |

#### `bernstein acp serve`

| Flag | Default | Meaning |
|---|---|---|
| `--transport {stdio\|http}` | stdio | ACP transport. |
| `--port N` | 8054 | HTTP port. |
| `--host HOST` | 127.0.0.1 | Bind host. |

#### `bernstein fingerprint`

| Subcommand | Purpose |
|---|---|
| `compute RUN_ID` | Compute the SHA-256 of a run's events. |
| `verify RUN_ID HASH` | Verify a run matches a known fingerprint. |
| `compare RUN_A RUN_B` | Compare two run fingerprints. |
| `seal FILES...` | Seal a set of files with a fingerprint. |

(`cli/commands/fingerprint_cmd.py:37+`.)

#### `bernstein graph`

| Subcommand | Purpose |
|---|---|
| `tasks` | Render the task DAG. |
| `agents` | Render the agent / capability graph. |
| `impact NODE` | Show downstream impact of a node. |

#### `bernstein voice` / `bernstein listen`

Experimental voice control (see [`operations/voice-control.md`](../operations/voice-control.md) when published).

| Flag | Default | Meaning |
|---|---|---|
| `--engine {whisper\|vosk}` | whisper | Speech recognition engine. |
| `--device INDEX` | default | Audio input device. |
| `--language LANG` | en | Language code. |

#### `bernstein explain`

| Flag | Default | Meaning |
|---|---|---|
| `CONCEPT` | required | Concept name (e.g. `cascade-router`, `wal`, `janitor`). |
| `--format {text\|markdown\|json}` | text | Output format. |

#### `bernstein ideate`

| Flag | Default | Meaning |
|---|---|---|
| `-c, --count N` | 3 | Number of improvement ideas. |
| `-f, --focus AREA` | none | Focus area (e.g. `performance`, `testing`, `docs`). |
| `--as-json` | off | Emit raw JSON. |

#### `bernstein test`

Convenience wrapper for the project's test suite. Honours the project's configured test runner (`bernstein.yaml: quality_gates.tests`); typically just delegates to `pytest -q` or equivalent.

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

Four task-related commands are wired but hidden from `--help`. They are stable and supported; just not surfaced because their UX is uneven or because their visible counterpart (`bernstein add-task`, `bernstein logs`) is what most users want.

| Command | Source | Replacement |
|---|---|---|
| `bernstein task compose TITLE` | `cli/commands/task_cmd.py:37` | Use `bernstein add-task TITLE` (it's the same command, registered with a different name at `cli/main.py:696`). |
| `bernstein task sync` | `cli/commands/task_cmd.py:116` | Reconciles on-disk task files with the running server. Use when you've hand-edited backlog files and want them registered without restarting. |
| `bernstein task notes` | `cli/commands/task_cmd.py:614` | Tail server / spawner logs. Prefer `bernstein logs tail`. |
| `bernstein task parts` | `cli/commands/task_cmd.py:637` | Same as `bernstein list-tasks`. |

To invoke any of them, just type the full path (`bernstein task compose ...`) - they accept the same flags as their visible siblings.

---

## See also

- [`cli/task-lifecycle.md`](cli/task-lifecycle.md) - driving Bernstein from a script.
- [`cli/replay.md`](cli/replay.md) - `replay` + `replay-filter` reference.
- [`reference/mcp-catalog.md`](mcp-catalog.md) - MCP catalog walkthrough.
- [`reference/openapi-reference.md`](openapi-reference.md) - REST + WebSocket + ACP/A2A endpoints.
- [`reference/FEATURE_MATRIX.md`](FEATURE_MATRIX.md) - capability matrix.
- [`operations/CONFIG.md`](../operations/CONFIG.md) - every config key Bernstein recognises.
