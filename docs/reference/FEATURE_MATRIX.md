# Feature Matrix

Shipped capabilities in Bernstein, verified against `src/bernstein/`.

The "Docs status" column reflects whether the page-level reference exists
(`Full`) or whether the capability is documented in source / module
docstrings only (`Brief`).

---

## Core orchestration

| Capability | Docs status | Notes |
|---|---|---|
| Goal-based run (`-g`) | Full | Main entry flow |
| Seed-file run (`bernstein.yaml`) | Full | Auto-discovery supported |
| Plan-file execution (`stages`/`steps`) | Full | `bernstein run plan.yaml` |
| Retry + escalation plumbing | Full | In task lifecycle, with configurable retries |
| Completion verification (janitor + signals) | Full | API + getting started coverage |
| Process-aware stop/drain | Full | Graceful and force stop, drain mode |
| Multi-cell orchestration | Brief | Implemented in `multi_cell.py` |
| Fast-path execution | Brief | Trivial tasks skip LLM agent entirely (`fast_path.py`) |
| Plan mode (human approval) | Full | `--plan-only`, `--from-plan`, approval routes |
| Headless mode | Full | `--headless` for CI/overnight |
| Dry-run mode | Full | `--dry-run` previews plan without spawning |

## State and persistence

| Capability | Docs status | Notes |
|---|---|---|
| File-based state in `.sdd/` | Full | Primary operating model |
| Metrics/trace persistence | Full | Paths documented, JSONL schema |
| Lessons/memory persistence | Brief | Stored and injected |
| Storage backends (`memory/postgres/redis`) | Full | Config + doctor coverage |
| Session persistence (fast resume) | Brief | `session.py` - resume after stop/restart |
| Bulletin board (cross-agent messaging) | Brief | Append-only, used by agents for handoff |

## Observability

| Capability | Docs status | Notes |
|---|---|---|
| `/status` and task API | Full | Core API documented |
| Prometheus `/metrics` | Brief | Endpoint is real; Grafana dashboards are user-defined |
| OTLP telemetry initialization | Brief | Wiring exists in `core/observability/` |
| Retrospective reporting (`retro`) | Full | CLI coverage present |
| Cost analysis (`cost`, history/anomaly hooks) | Full | `bernstein cost`, cost anomaly detection active |
| Per-agent token progress | Brief | Tracked in `api_usage.py`, surfaced in `bernstein status` |
| Session analytics | Brief | `bernstein recap` shows session-level stats |
| Agent activity tracking | Brief | Activity metrics in `metrics/` |
| Debug bundle | Brief | `bernstein debug`, collects logs/state/config for triage |

## Safety and governance

| Capability | Docs status | Notes |
|---|---|---|
| Quality gates (lint, type-check, tests) | Full | Present in run flow; extended with coverage, benchmark, arch conformance, mutation testing gates |
| PII scan quality gate | Brief | Active, auto-installed via `log_redact.py` |
| Rule enforcement (`.bernstein/rules.yaml`) | Full | Enforcement behavior documented |
| Log redaction (PII filter) | Brief | Active |
| Audit and verification commands | Brief | `bernstein audit seal/verify`, Merkle proofs |
| HMAC-chained audit log | Brief | Tamper-evident, daily rotation |
| Execution WAL | Brief | Hash-chained, crash recovery, determinism fingerprinting |
| Circuit breaker | Full | Halts misbehaving agents, writes SHUTDOWN signal |
| Token growth monitor | Brief | Auto-intervention on runaway consumption |
| Cost anomaly detection | Brief | Z-score based, acts via task completion |
| Peak-hour scheduling | Brief | `peak_hour_router.py` - cost-aware time-of-day routing |
| Agent loop detection | Brief | Kills agents in edit-loop cycles |
| Deadlock detection | Brief | Wait-for graph, automatic victim selection |
| Cross-model verification | Brief | Different model reviews completed diffs (opt-in) |
| Behaviour anomaly detection | Brief | `core/observability/behavior_anomaly.py` - flags agents whose runtime metrics deviate statistically from baseline |
| Agent run manifest | Brief | Hashable workflow spec for SOC2 evidence |
| Context degradation detector | Brief | Monitors quality over time, restarts when degraded |
| Progressive permission prompts | Brief | Per-agent permission levels |

## Ecosystem and integrations

| Capability | Docs status | Notes |
|---|---|---|
| Agent catalog/discovery | Full | `bernstein agents sync/list/discover/match/showcase` (40+ CLI agent adapters) |
| GitHub App and CI fix flows | Full | `bernstein ci fix <url>`, `github setup` |
| Trigger sources (`github`, `slack`, `file_watch`, `webhook`) | Brief | Source adapters available |
| Plugin hooks (pluggy) | Full | SDK docs in CONTRIBUTING.md |
| Cluster/worker primitives | Full | `bernstein worker --server URL`, cluster routes documented |
| Multi-repo workspaces | Full | `workspace:` in bernstein.yaml, workspace CLI |
| MCP server mode | Brief | `bernstein mcp`, MCP server in `mcp/server.py` |
| MCP tool registry | Brief | Auto-discovery and per-task config |
| MCP catalog client | Brief | `bernstein mcp catalog browse/search/install` - installable server catalog (`core/protocols/mcp_catalog/`) |
| ACP native bridge | Full | `bernstein acp serve --stdio\|--http :PORT` - IDE-native bridge (`core/protocols/acp/`); see `reference/acp-bridge.md` |
| Protocol negotiation | Brief | `protocol_negotiation.py` - runtime protocol version handshake |
| Schema registry | Brief | `schema_registry.py` - versioned message schemas for protocols |
| Credential vault | Brief | `bernstein connect <provider>`, `bernstein creds list/revoke/test` - OS-keychain token storage (`core/security/vault/`) |
| Autofix CI daemon | Brief | `bernstein autofix start/stop/status/attach` - watches PRs, dispatches repair runs on CI failure (`core/autofix/`) |
| Dev preview | Brief | `bernstein preview start/stop/list/status` - exposes agent dev server via tunnel with configurable auth (`core/preview/`) |
| Fleet dashboard | Brief | `bernstein fleet [--web HOST:PORT]` - cross-session multi-instance view (`core/fleet/`) |
| Notification sinks | Brief | `bernstein notify test --sink <id>` - pluggable notification backends (`core/notifications/`) |
| PR review responder | Brief | `bernstein review-responder start/status/tick` - auto-responds to PR review comments (`core/review_responder/`) |
| Review pipeline DSL | Brief | `bernstein review --pipeline review.yaml` - YAML-driven multi-phase review (`core/quality/review_pipeline/`) |
| Plan archival | Brief | `bernstein plan ls/show` - list and inspect archived plans (`core/planning/lifecycle.py`) |
| Slack integration | Brief | Slash commands and events API endpoints |
| Webhook ingestion | Brief | `POST /webhooks/` for external event routing |
| Adaptive parallelism | Brief | `core/orchestration/adaptive_parallelism.py` - auto-tunes concurrency from observed success rates |
| Warm pool | Brief | `core/agents/warm_pool.py` - pre-spawned agent pool to cut spawn latency |
| Content-addressed artifact store | Brief | `core/persistence/cas_store.py` - content-addressed deduplication for artifacts |
| Workflow DSL | Brief | `bernstein workflow validate/list/show` |
| Chaos engineering | Brief | `bernstein chaos agent-kill/rate-limit/file-remove/status/slo` |
| Benchmark suite | Full | `bernstein benchmark run/compare/swe-bench` |
| Eval harness | Brief | `bernstein eval run/report/failures` |
| SWE-Bench harness | Full | Verified eval in `benchmarks/swe_bench/run.py` |
| Graduation system | Brief | Agent promotion stages, routes in `routes/graduation.py` |
| Semantic caching | Brief | `semantic_cache.py` - prompt deduplication |
| Cascade router (intra-Claude tier escalation) | Brief | Tier escalation within a single provider - see `core/routing/cascade_router.py:386` |
| Cascade fallback manager (cross-adapter failover) | Brief | Cross-adapter provider failover - see `core/routing/cascade.py:287` |
| Batch router | Brief | Task batching for non-urgent work |
| Prompt caching | Brief | SHA-256 system prefix deduplication |
| Output style customization | Brief | Configurable agent output format |
| Installation mismatch detection | Brief | Detects adapter/installation gaps |
| API preconnect warmup | Brief | Connection warmup before heavy runs |
| Worker badge identity | Brief | Process identification in `ps`/Activity Monitor |
| Keybinding system (TUI) | Brief | Configurable TUI keyboard shortcuts |
| Diff folding display | Brief | Folded diff rendering in agent output |
| Word-level diff rendering | Brief | Character-level change highlighting |
| Contextual tips system | Brief | In-context hints for agents |
| Session tag system | Brief | Tag and filter runs |
| Rename session | Brief | Session renaming command |
| Security review command | Brief | `bernstein security-review` |
| Commit attribution stats | Brief | Per-agent commit statistics |
| Away summary generation | Brief | Summarize what happened while you were away |
| Plugin trust warning | Brief | Warns on unverified plugins |
| Cumulative progress tracking | Brief | Progress tracking across runs |

## CLI commands

| Command | Docs status | Notes |
|---|---|---|
| `bernstein -g GOAL` | Full | Inline goal |
| `bernstein run plan.yaml` | Full | Plan file execution |
| `bernstein init` | Full | Workspace setup |
| `bernstein stop` | Full | Graceful/force stop |
| `bernstein live` | Full | TUI dashboard |
| `bernstein dashboard` | Full | Web dashboard |
| `bernstein status` | Full | Task summary |
| `bernstein ps` | Full | Process list |
| `bernstein cost` | Full | Spend breakdown |
| `bernstein doctor` | Full | Pre-flight health check |
| `bernstein recap` | Full | Post-run summary |
| `bernstein retro` | Full | Retrospective report |
| `bernstein trace ID` | Full | Decision trace |
| `bernstein logs` | Full | Agent log tail |
| `bernstein diff ID` | Full | Per-task git diff |
| `bernstein plan` | Full | Task backlog |
| `bernstein replay ID` | Brief | Deterministic replay |
| `bernstein checkpoint` | Brief | Session snapshot |
| `bernstein wrap-up` | Brief | End session with summary |
| `bernstein demo` | Full | Zero-config demo |
| `bernstein quickstart` | Brief | Flask TODO demo (3 tasks) |
| `bernstein agents ...` | Full | Catalog management |
| `bernstein evolve ...` | Full | Self-improvement |
| `bernstein ci fix` | Full | CI autofix |
| `bernstein github setup` | Full | GitHub App setup |
| `bernstein worker` | Brief | Join cluster as worker |
| `bernstein mcp` | Brief | Run as MCP server |
| `bernstein chaos` | Brief | Fault injection |
| `bernstein audit` | Brief | Cryptographic audit |
| `bernstein verify` | Brief | Merkle/HAMC verification |
| `bernstein benchmark` | Full | Benchmark suite |
| `bernstein eval` | Brief | Evaluation harness |
| `bernstein ideate` | Brief | Creative evolution |
| `bernstein workspace` | Full | Multi-repo workspace |
| `bernstein config` | Brief | Configuration management |
| `bernstein quarantine` | Brief | Cross-run task quarantine |
| `bernstein cache` | Brief | Response cache management |
| `bernstein test-adapter` | Brief | Adapter smoke test |
| `bernstein add-task` | Brief | Inject task via CLI |
| `bernstein cancel` | Brief | Cancel task |
| `bernstein review/approve/reject/pending` | Brief | Review workflow |
| `bernstein sync` | Brief | Sync backlog with server |
| `bernstein manifest` | Brief | Run manifest inspection |
| `bernstein gateway` | Brief | MCP gateway proxy |
| `bernstein workflow` | Brief | Workflow DSL |
| `bernstein watch` | Brief | Directory file watcher |
| `bernstein listen` | Brief | Voice commands (experimental) |
| `bernstein completions` | Brief | Shell completion scripts |
| `bernstein self-update` | Brief | Upgrade from PyPI |
| `bernstein plugins` | Brief | List active plugins |
| `bernstein install-hooks` | Brief | Install git hooks |
| `bernstein debug` | Brief | Generate debug bundle for triage |
| `bernstein acp serve` | Full | ACP bridge (`--stdio` or `--http :PORT`) |
| `bernstein autofix ...` | Brief | CI autofix daemon (start/stop/status/attach) |
| `bernstein connect` | Brief | Credential vault setup for a provider |
| `bernstein creds ...` | Brief | Credential management (list/revoke/test) |
| `bernstein preview ...` | Brief | Dev server preview (start/stop/list/status) |
| `bernstein fleet` | Brief | Fleet dashboard (optionally `--web HOST:PORT`) |
| `bernstein mcp catalog ...` | Brief | MCP catalog browser (browse/search/install) |
| `bernstein notify test` | Brief | Notification sink smoke test |
| `bernstein plan ls/show` | Brief | List and inspect archived plans |
| `bernstein review-responder ...` | Brief | PR review responder (start/status/tick) |
| `bernstein review --pipeline` | Brief | Review with YAML pipeline DSL |

---

## Cloud / Cloudflare

| Capability | Docs status | Notes |
|---|---|---|
| Workers RuntimeBridge | Full | `bridges/cloudflare.py` - agents on Workers + Durable Objects |
| Workflow Bridge (durable execution) | Full | `bridges/cloudflare_workflow.py` - auto-retry, approval gates |
| Sandbox Bridge (V8/container isolation) | Full | `bridges/cloudflare_sandbox.py` - isolated code execution |
| Browser Rendering Bridge | Full | `bridges/browser_rendering.py` - screenshots, scraping, PDFs |
| R2 Workspace Sync | Full | `bridges/r2_sync.py` - content-addressed delta sync |
| Workers AI Provider (free LLMs) | Full | `core/routing/cloudflare_ai.py` - Llama, Mistral, Gemma, Qwen |
| D1 Analytics & Billing | Full | `core/cost/d1_analytics.py` - usage metering, billing tiers |
| MCP Remote Transport | Full | `mcp/remote_transport.py` - streamable HTTP for remote MCP |
| Cloud CLI (`bernstein cloud`) | Full | `cli/commands/cloud_cmd.py` - login, run, status, cost, deploy |
| Cloudflare Agents Adapter | Full | `adapters/cloudflare_agents.py` - wrangler dev integration |
| Codex-on-Cloudflare Adapter | Full | `adapters/codex_cloudflare.py` - Codex in CF sandboxes |
