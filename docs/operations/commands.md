# Bernstein operator commands

Full operator command surface. The README keeps the short list; this page is the long form.

For session monitoring commands (`live`, `dashboard`, `status`, `ps`, `cost`, `doctor`, `recap`, `trace`, etc.) see `bernstein --help`.

## Core operator commands

| Command | What it does |
|---------|--------------|
| `bernstein pr` | Auto-creates a GitHub PR from a completed session; body carries the janitor's gate results and token/USD cost breakdown. |
| `bernstein from-ticket <url>` | Imports a Linear / GitHub Issues / Jira ticket as a Bernstein task. Label-based role + scope inference. Supports `--dry-run` and `--run`. |
| `bernstein ticket import <url>` | Alias / group form of `from-ticket` for scripting. |
| `bernstein backlog claim --role reviewer` | Atomically claims one eligible row from `.sdd/runtime/task-backlog.json` for external workers sharing a same-host JSON backlog. Supports `--backlog`, `--agent-id`, `--project`, `--capability`, `--done`, `--max-attempts`, and `--json`. |
| `bernstein hooks` | Lifecycle hooks for `pre_task`, `post_task`, `pre_merge`, `post_merge`, `pre_spawn`, `post_spawn`; shell scripts or pluggy `@hookimpl`s. `hooks list`, `hooks run <event>`, `hooks check`. |
| `bernstein chat serve --platform=telegram\|discord\|slack` | Drive runs from chat with `/run`, `/status`, `/approve`, `/reject`, `/switch`, `/stop`. |
| `bernstein workflow run <name>` | Run a YAML workflow manifest. Also `workflow list`, `workflow init`, `workflow validate`. |
| `bernstein approve-tool` / `bernstein reject-tool` | Interactive mid-run tool-call approval. `--latest`, `--id`, `--always`. |
| `bernstein autofix` | Daemon that monitors open Bernstein PRs; spawns a fixer agent when CI fails and pushes the repair automatically. |
| `bernstein preview start` | Sandboxed dev server for the current branch with a shareable public tunnel URL. |
| `bernstein remote` | SSH sandbox backend. `remote test <host>`, `remote run <host> <path>`, `remote forget <host>`. ControlMaster socket reuse for fast repeat calls. |
| `bernstein tunnel start <port> [--provider auto\|cloudflared\|ngrok\|bore\|tailscale]` | One wrapper around four tunnel providers. Also `tunnel list`, `tunnel stop <name>\|--all`. |
| `bernstein daemon install [--user\|--system] [--command="..."] [--env KEY=VAL]...` | Installs a systemd (Linux) or launchd (macOS) unit for auto-start. Also `daemon start/stop/restart/status/uninstall`. |
| `bernstein connect <provider>` / `bernstein creds` | Stores and rotates API credentials in the OS keychain. Agents inherit scoped keys per-run. |
| `bernstein sandbox web-test <task-id> --url <url> --scenarios <yaml>` | Drives a Playwright self-test against the dev server. See [docs/sandbox/playwright-self-test.md](../sandbox/playwright-self-test.md). |
| `bernstein agents-md` | Generates a canonical AAIF AGENTS.md for the repo and rewrites it into each CLI's native shape (`CLAUDE.md`, `.cursor/rules/*.mdc`, `CONVENTIONS.md`, `.goosehints`). `generate`, `write`, `sync`, `verify`, `diff`. |
| `bernstein scaffold "<prompt>"` | Bootstraps a project skeleton from a single goal prompt. `--template auto\|python-cli\|...`, `--output <dir>`, `--force`. |
| `bernstein wiki build` | Renders `WIKI.md` for the current repo from the AST symbol graph. Local, no LLM call, no cloud round-trip. |
| `bernstein simulate <plan.yaml>` | Digital-twin dry-run: predicts cost band (p50/p90), wall-clock, abandonment probability, per-task blast-radius, and bottlenecks against historical `.sdd/traces/` + `.sdd/metrics/` without spawning a real agent or hitting the network. See [docs/operations/simulate.md](simulate.md). |
| `bernstein compare <spec> --adapters claude,codex[,...]` | Side-by-side adapter A/B in isolated per-adapter worktrees. Up to four adapters, deterministic seed, unified diff against baseline. See [docs/operations/compare.md](compare.md). |
| `bernstein recipes list / show / run` | First-class workflow library. Parameterised recipes live in `templates/recipes/*.yaml`. See [docs/operations/recipes.md](recipes.md). |
| `bernstein resume <task-id>` | Pick up a task from its last `checkpoint.json` instead of restarting. See [docs/operations/resume.md](resume.md). |
| `bernstein fork --run <id> --from-step N` | Rewind a run to journal step N and branch a new run from its content-addressed worktree snapshot. The snapshot sha is recorded in the event journal, so a tampered snapshot ref is detected. See [docs/operations/fork-from-step.md](fork-from-step.md). |
| `bernstein worktrees list / gc` | Inspect and reap orphan worktrees. Four-state classifier (`active` / `orphan` / `stale` / `corrupt`). See [docs/operations/worktrees.md](worktrees.md). |
| `bernstein telemetry on / off / status / export` | Opt-in operator telemetry. Default off; honours `DO_NOT_TRACK` and `BERNSTEIN_TELEMETRY=0`. See [docs/telemetry.md](../telemetry.md). |
| `bernstein doctor extended` | Extended pre-flight on top of `bernstein doctor`: adapter conformance, network reachability, and CI integration probes. See [docs/operations/doctor.md](doctor.md). |
| `bernstein adapters check / list-status` | Conformance plus capability matrix for installed adapters. See [docs/operations/adapters.md](adapters.md). |
| `bernstein decisions tail / search` | Inspect `.sdd/runtime/decisions.jsonl`: every routing / criterion-profile / gate-fire decision. See [docs/operations/decision-log.md](decision-log.md). |
| `bernstein abandonments list / stats` | Read-side of the agent-abandon ledger at `.sdd/runtime/abandonments.jsonl`. See [docs/operations/abandonments.md](abandonments.md). |
| `bernstein criterion-profile list / show` | Inspect per-task criterion profile (correctness / cost / latency / reversibility). See [docs/operations/criterion-profiles.md](criterion-profiles.md). |
| `bernstein eval calibration report` | Brier score + ECE + reliability buckets over `.sdd/metrics/calibration.jsonl`. See [docs/operations/calibration.md](calibration.md). |
| `bernstein lineage v2 show / verify / export` | Opt-in two-layer lineage store. See [docs/operations/lineage-v2.md](lineage-v2.md). |
| `bernstein run --retry-budget SPEC` | Criterion-aware retry budget. See [docs/operations/retry-budget.md](retry-budget.md). |
| `bernstein identity show` / `decode` / `verify` / `disable` | Operator-side helpers for the install-rev fingerprint embedded in shared yaml/trace/role-prompt artefacts. |
| `bernstein security role-adapter-policy` | Inspects and edits the per-role adapter allow-list (deny-list enforcement at spawn time). |
| `bernstein run-lookup NAME` | Resolve a memorable run name back to its run UUID; exits non-zero when the name is malformed or unknown. Example: `bernstein run-lookup brave-otter-1234`. |

## Monitoring

```bash
bernstein live       # TUI dashboard
bernstein dashboard  # web dashboard
bernstein status     # task summary
bernstein ps         # running agents
bernstein cost       # spend by model/task
bernstein doctor     # pre-flight checks
bernstein recap      # post-run summary
bernstein export     # shareable HTML/Markdown report of the latest run
bernstein trace <ID> # agent decision trace
bernstein run-changelog --hours 48  # changelog from agent-produced diffs
bernstein explain <cmd>  # detailed help with examples
bernstein dry-run    # preview tasks without executing
bernstein dep-impact # API breakage + downstream caller impact
bernstein aliases    # show command shortcuts
bernstein config-path    # show config file locations
bernstein init-wizard    # interactive project setup
bernstein debug-bundle   # collect logs, config, and state for bug reports
bernstein skills list    # discoverable skill packs (progressive disclosure)
bernstein skills show <name>  # print a skill body with its references
```

```bash
bernstein fingerprint build --corpus-dir ~/oss-corpus  # build local similarity index
bernstein fingerprint check src/foo.py                 # check generated code against the index
```
