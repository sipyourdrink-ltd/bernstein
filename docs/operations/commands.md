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
| `bernstein approve-tool` / `bernstein reject-tool` | Interactive mid-run tool-call approval. `--latest`, `--id`, `--always`. Flag form: `approve --tool <id>` / `reject --tool <id>`. |
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
| `bernstein recipes register / fire / history / repair-lineage` | Content-addressed registered runs. Each fire writes a receipt to the audit chain; `fire` needs a reachable, authenticated task server (exit `2` when it cannot submit); `repair-lineage` resolves a forked definition lineage additively. See [docs/operations/recipes.md](recipes.md). |
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
| `bernstein stop [--force]` | Graceful drain (soft stop) by default. `--force`/`--hard` SIGKILLs everything immediately, then reaps whole process groups so a re-parented grandchild (disowned heartbeat/curl loops) dies with its leader; the summary counts only PIDs confirmed terminated. |
| `bernstein self check-update` / `update` / `pin` / `rollback` | Provenance-verified update lifecycle. Verifies a signed release feed against a configured trust root before naming a candidate, seals a chain-anchored advisory that `--verify` recomputes offline, refuses to update while a run is active, and re-hashes the wheel before install. Offline-first and opt-in; the air-gap profile disables the remote path. See [updates.md](updates.md). |
| `bernstein cluster status` / `bernstein cluster nodes` | Render the node registry as a table (id, adapter, heartbeat age, claimed tasks). `status` adds the topology summary line. Both take `--json-output` and `--server-url`. See [cluster-mode.md](cluster-mode.md). |
| `bernstein serve` | Runs the task server in the foreground (blocks until SIGINT/SIGTERM) instead of detaching it, so a container's PID 1 stays alive and can host a long-lived central/coordinator node whose `/health` endpoint stays reachable. It is the published image's default `CMD`. Set `BERNSTEIN_BIND_HOST=0.0.0.0` and `BERNSTEIN_CLUSTER_ENABLED=1` to bind all interfaces and expose cluster endpoints. |
| `bernstein schedule add\|list\|run` | Manage operator-registered recurring schedules; `schedule audit` walks persisted fire receipts to prove the sequence is replayable. |
| `bernstein schedule show <id> --at <time>\|schedule verify` | Treats a recurring fire as a pure projection of `(schedule, fire_time, state)` onto a canonical task graph: `show --at` prints the deterministic graph hash a fire would dispatch without firing (no journal, no receipt, no side effects), and `verify` replays a recorded fire and confirms the graph hash reproduces byte-identically. RFC-5545 `RRULE` and cron are both accepted; a webhook / file-change trigger binds its event as an input hash. |
| `bernstein sla add\|list\|show\|verify\|report` | Attach a per-goal SLA contract (a content-addressed document) to a single recurring goal, task family, or spend envelope, declaring run-duration, start-lateness, fire-frequency, artifact-freshness, and spend-rate axes. The supervisor evaluates contracts against chain evidence on each tick (read-only, never dispatches); a breach becomes a signed violation receipt embedding the contract hash, the chain evidence of the miss, and a deterministic, budget-gated remediation. `sla verify <receipt.json>` re-derives the verdict from the embedded evidence and checks the Ed25519 signature offline (flip any byte and it fails); `sla report` projects a per-contract error budget over the work-ledger segment so two parties derive identical numbers from identical history. |
| `bernstein templates compress <role>\|--all` | Operator-gated, one-time LLM compression of role prompt templates: mechanically validated (fenced blocks, headings, URLs, placeholders, completion contract stay byte-equal), originals backed up out of tree by content hash, receipt chained to the audit log. `bernstein templates restore <role>` reverses it byte-identically; savings appear in `bernstein cost --by role`. |
| `bernstein identity keydir` | Prints the install-identity key directory (JWKS) - the Ed25519 public keys that verify the RFC 9421 HTTP Message Signatures Bernstein places on its outbound agent-facing requests (also served at `/.well-known/http-message-signatures-directory`). Set `BERNSTEIN_HTTP_SIGNING_REQUIRED=1` to refuse unsigned outbound paths. |
| `bernstein security-review [<task>]` | Pattern-scans a unified diff for hardcoded secrets, unsafe `eval`/`exec`, shell injection, weak crypto, path traversal, and SQL injection without calling a model. Scans the working tree against `--base` by default, one agent's task diff when given a task id, or a piped diff with `--diff-file -`. Exits `1` on any critical/high finding so it can gate a pre-commit hook or a CI step, `2` when there is no diff to scan, and `--as-json` emits the findings for a pipeline. |
| `bernstein delegation verify <run>` | Reconstructs the `principal -> orchestrator -> sub-agent` delegation chain for a run from HMAC-chained per-hop receipts and confirms it is intact offline; exits non-zero on any tamper or deleted hop. |
| `bernstein artifact list\|show <task> [<key>]` | Renders a task's agent-posted artifacts (markdown reports, tables, preview links, and normalized SARIF findings) with per-version verification state. Finding addresses bind the rule, normalized artifact URI, source snippet, relative region shape, scanner version, pinned ruleset/feed, invocation, and target. They stay stable when blank lines move an unchanged snippet, when the same source is checked out with different line endings, and when a path arrives in Windows separator or decomposed-Unicode form. Each artifact is content-addressed, spine-anchored, and journal-chained; a version whose stored blob or recorded finding address does not recompute renders as tampered rather than as content. `bernstein audit verify` walks every artifact, so a mismatch fails naming the key and journal position. |
| `bernstein skills package install\|update\|verify\|status\|conformance` | Installs the bundled cross-vendor `bernstein-run` agent skill into a host's skill directory (`--host claude\|codex\|copilot\|cursor\|gemini`, or `--dest`) and anchors a content-addressed install receipt in the lineage spine and audit chain; `verify` re-hashes the installed tree and proves it against the receipt; `update` supersedes a prior install with a receipt binding the prior content address to the new one; `status` verifies every host install at once; `conformance` installs into several hosts against one install, replays the skill's self-check contract per host, and seals the pass/fail table into a chain-anchored conformance receipt. `--record-only` anchors a plugin checkout the host installed itself. See [agent sessions](../integrations/agent-session.md). |
| `bernstein datasource register\|query\|verify` | Read-only SQL datasources whose results become content-addressed query receipts: `query` canonicalises the exact result set an agent saw and binds its SHA-256 into a signed lineage entry (`.jws` sidecar); `verify` proves the signature, chain anchor and stored copy offline, and `verify --re-execute` re-runs the query to report `MATCH` or `DRIFT`. DML/DDL is refused with a typed error and connection secrets never reach a receipt. See [datasources](datasources.md). |
| `bernstein bench run <suite>\|bench verify <bundle>` | Runs a content-addressed benchmark suite and emits a signed submission bundle in which every task carries the replay receipt its score was derived from. `bench verify` replays those receipts offline with no access to the submitter's machine, recomputes each score, and reports MATCH or names the exact task whose replay diverged. A flipped verdict or a corrupted receipt fails verification, so a published number is only worth the bundle a third party can re-verify. See [bench](../eval/bench.md). |
| `bernstein pool register\|list\|show\|verify` | Define and govern named sandbox pools (manifests, capability ceilings, backend allowlists) projected from the HMAC audit chain. Distinct from `bernstein limits pool`. |
| `bernstein limits pool\|tag\|rate\|queue\|status\|verify` | Govern lease-backed admission slot pools, task tag ceilings, rate limits, and priority queues projected from the admission work ledger. Distinct from `bernstein pool`. |

## Monitoring

```bash
bernstein live       # TUI dashboard
bernstein gui serve  # web GUI
bernstein status     # task summary
bernstein ps         # running agents (PID files + a live process-table cross-check, so it still lists survivors after stop --force removed the PID files)
bernstein cost       # spend by model/task
bernstein doctor     # pre-flight checks
bernstein recap      # post-run summary
bernstein export     # shareable HTML/Markdown report of the latest run
bernstein trace <ID> # agent decision trace
bernstein changelog --hours 48  # changelog from agent-produced diffs
bernstein explain <cmd>  # detailed help with examples
bernstein dry-run    # preview tasks without executing
bernstein impact deps # API breakage + downstream caller impact
bernstein aliases    # show command shortcuts
bernstein config-path    # show config file locations
bernstein init --wizard  # interactive project setup
bernstein debug bundle   # collect logs, config, and state for bug reports
bernstein skills list    # discoverable skill packs (progressive disclosure)
bernstein skills show <name>  # print a skill body with its references
```

```bash
bernstein fingerprint build --corpus-dir ~/oss-corpus  # build local similarity index
bernstein fingerprint check src/foo.py                 # check generated code against the index
```

## Deprecated command names

Five top-level names duplicated a group that already owned the same action
(#3138). The group spelling is canonical from 3.x on; the top-level spelling
still resolves, prints a deprecation warning on stderr, and is unregistered in
v4.0.0. Flags and arguments are unchanged unless noted.

| Deprecated | Use instead | Notes |
|---|---|---|
| `bernstein estimate` | `bernstein cost estimate` | Same flags. |
| `bernstein cost-envelopes show` | `bernstein cost envelopes show` | Same subcommand and flags. |
| `bernstein artifacts list\|show` | `bernstein artifact list\|show` | Same arguments. `artifact list` with no task keeps its own meaning (lineage-spine keys). |
| `bernstein skill provenance\|verify` | `bernstein skills provenance\|verify` | Same arguments. |
| `bernstein debug-bundle` | `bernstein debug bundle` | **Not a rename.** Different builder: `--output` becomes `--out`, `--yes` is unnecessary (no confirmation prompt), and `--extended` has no equivalent. See [debug bundle](debug-bundle.md). |

`bernstein pool` is **not** deprecated. It projects the sandbox-pool registry
from the audit chain; `bernstein limits pool` governs admission slot pools in
the work ledger. The two address different stores, so folding one into the
other would change results rather than just the name.

Four more top-level names were folded into the domain group that already
owned the surface (#3140). Same rule: the fold spelling is canonical, the old
spelling still resolves with a stderr warning, and it is unregistered in
v4.0.0.

| Deprecated | Use instead | Notes |
|---|---|---|
| `bernstein quickstart` | `bernstein demo --flask-todo` | Same demo. The retained alias keeps its own adapter auto-detection; `demo --flask-todo` runs on the mock adapter unless `--real` is passed. |
| `bernstein init-wizard` | `bernstein init --wizard` | Same wizard. `bernstein i` is the shortcut. |
| `bernstein validate` | `bernstein plan validate` | Same argument and flags. |
| `bernstein routine <sub>` | `bernstein schedule routine <sub>` | Same subcommands (`export`, `provision`, `register`, `bindings`) and flags. |

The warning goes to stderr only, so `bernstein cost-envelopes show --json | jq`
and friends keep working while a script is migrated.
