# Feature Matrix

Shipped capabilities in Bernstein v1.8.4, verified against `src/bernstein/`.

Legend:

- **Shipped** — implemented and usable now
- **Partial** — implemented with scope boundaries or incomplete docs
- **Roadmap** — not complete enough to present as a production feature

---

## Core orchestration

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| Goal-based run (`-g`) | Shipped | Full | Main entry flow |
| Seed-file run (`bernstein.yaml`) | Shipped | Full | Auto-discovery supported |
| Plan-file execution (`stages`/`steps`) | Shipped | Full | `bernstein run plan.yaml` |
| Retry + escalation plumbing | Shipped | Full | In task lifecycle, with configurable retries |
| Completion verification (janitor + signals) | Shipped | Full | API + getting started coverage |
| Process-aware stop/drain | Shipped | Full | Graceful and force stop, drain mode |
| Multi-cell orchestration | Partial | Brief | Implemented in `multi_cell.py`, limited user-level docs |
| Fast-path execution | Shipped | Brief | Trivial tasks skip LLM agent entirely (`fast_path.py`) |
| Plan mode (human approval) | Shipped | Full | `--plan-only`, `--from-plan`, approval routes |
| Headless mode | Shipped | Full | `--headless` for CI/overnight |
| Dry-run mode | Shipped | Full | `--dry-run` previews plan without spawning |

## State and persistence

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| File-based state in `.sdd/` | Shipped | Full | Primary operating model |
| Metrics/trace persistence | Shipped | Full | Paths documented, JSONL schema |
| Lessons/memory persistence | Partial | Brief | Stored and injected, evolving UX |
| Storage backends (`memory/postgres/redis`) | Shipped | Full | Config + doctor coverage |
| Session persistence (fast resume) | Shipped | Brief | `session.py` — resume after stop/restart |
| Bulletin board (cross-agent messaging) | Shipped | Brief | Append-only, used by agents for handoff |

## Observability

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| `/status` and task API | Shipped | Full | Core API documented |
| Prometheus `/metrics` | Shipped | Brief | Endpoint is real; Grafana dashboards are user-defined |
| OTLP telemetry initialization | Partial | Brief | Wiring exists; deployment guidance still shallow |
| Retrospective reporting (`retro`) | Shipped | Full | CLI coverage present |
| Cost analysis (`cost`, history/anomaly hooks) | Shipped | Full | `bernstein cost`, cost anomaly detection active |
| Per-agent token progress | Shipped | Partial | Tracked in `api_usage.py`, surfaced in `bernstein status` |
| Session analytics | Shipped | Brief | `bernstein recap` shows session-level stats |
| Agent activity tracking | Shipped | Brief | Activity metrics in `metrics/` |
| Debug bundle | Shipped | Brief | `bernstein debug`, collects logs/state/config for triage |

## Safety and governance

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| Quality gates (lint, type-check, tests) | Shipped | Full | Present in run flow; extended with coverage, benchmark, arch conformance, mutation testing gates |
| PII scan quality gate | Shipped | Brief | Active, auto-installed via `log_redact.py` |
| Rule enforcement (`.bernstein/rules.yaml`) | Shipped | Full | Enforcement behavior documented |
| Log redaction (PII filter) | Shipped | Brief | Active but under-documented |
| Audit and verification commands | Shipped | Brief | `bernstein audit seal/verify`, Merkle proofs |
| HMAC-chained audit log | Shipped | Brief | Tamper-evident, daily rotation |
| Execution WAL | Shipped | Brief | Hash-chained, crash recovery, determinism fingerprinting |
| Circuit breaker | Shipped | Full | Halts misbehaving agents, writes SHUTDOWN signal |
| Token growth monitor | Shipped | Brief | Auto-intervention on runaway consumption |
| Cost anomaly detection | Shipped | Brief | Z-score based, acts via task completion |
| Peak-hour scheduling | Shipped | Brief | `peak_hour_router.py` — cost-aware time-of-day routing |
| Agent loop detection | Shipped | Brief | Kills agents in edit-loop cycles |
| Deadlock detection | Shipped | Brief | Wait-for graph, automatic victim selection |
| Cross-model verification | Shipped | Brief | Different model reviews completed diffs |
| Agent run manifest | Shipped | Brief | Hashable workflow spec for SOC2 evidence |
| Context degradation detector | Shipped | Roadmap-level docs | Monitors quality over time, restarts when degraded |
| Progressive permission prompts | Shipped | Brief | Per-agent permission levels |

## Ecosystem and integrations

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| Agent catalog/discovery | Shipped | Full | `bernstein agents sync/list/discover/match/showcase` (18 CLI agent adapters) |
| GitHub App and CI fix flows | Shipped | Full | `bernstein ci fix <url>`, `github setup` |
| Trigger sources (`github`, `slack`, `file_watch`, `webhook`) | Partial | Brief | Source adapters exist; authoring docs need detail |
| Plugin hooks (pluggy) | Shipped | Full | SDK docs in CONTRIBUTING.md |
| Cluster/worker primitives | Partial | Full | `bernstein worker --server URL`, cluster routes documented |
| Multi-repo workspaces | Shipped | Full | `workspace:` in bernstein.yaml, workspace CLI |
| MCP server mode | Shipped | Brief | `bernstein mcp`, MCP server in `mcp/server.py` |
| MCP tool registry | Shipped | Brief | Auto-discovery and per-task config |
| Protocol negotiation | Shipped | Brief | `protocol_negotiation.py` — runtime protocol version handshake |
| Schema registry | Shipped | Brief | `schema_registry.py` — versioned message schemas for protocols |
| Slack integration | Shipped | Brief | Slash commands and events API endpoints |
| Webhook ingestion | Shipped | Brief | `POST /webhooks/` for external event routing |
| Adaptive parallelism | Partial | Roadmap-level docs | `adaptive_parallelism.py` exists |
| Workflow DSL | Shipped | Brief | `bernstein workflow validate/list/show` |
| Chaos engineering | Shipped | Brief | `bernstein chaos agent-kill/rate-limit/file-remove/status/slo` |
| Benchmark suite | Shipped | Full | `bernstein benchmark run/compare/swe-bench` |
| Eval harness | Shipped | Brief | `bernstein eval run/report/failures` |
| SWE-Bench harness | Shipped | Full | Verified eval in `benchmarks/swe_bench/run.py` |
| Graduation system | Partial | Brief | Agent promotion stages, routes in `routes/graduation.py` |
| Semantic caching | Shipped | Brief | `semantic_cache.py` — prompt deduplication |
| Cascade router | Shipped | Brief | Cost-aware model cascading |
| Batch router | Shipped | Brief | Task batching for non-urgent work |
| Prompt caching | Shipped | Brief | SHA-256 system prefix deduplication |
| Output style customization | Shipped | Roadmap-level docs | Configurable agent output format |
| Installation mismatch detection | Shipped | Roadmap-level docs | Detects adapter/installation gaps |
| API preconnect warmup | Shipped | Roadmap-level docs | Connection warmup before heavy runs |
| Worker badge identity | Shipped | Roadmap-level docs | Process identification in `ps`/Activity Monitor |
| Keybinding system (TUI) | Shipped | Roadmap-level docs | Configurable TUI keyboard shortcuts |
| Diff folding display | Shipped | Roadmap-level docs | Folded diff rendering in agent output |
| Word-level diff rendering | Shipped | Roadmap-level docs | Character-level change highlighting |
| Contextual tips system | Shipped | Roadmap-level docs | In-context hints for agents |
| Session tag system | Shipped | Roadmap-level docs | Tag and filter runs |
| Rename session | Shipped | Roadmap-level docs | Session renaming command |
| Security review command | Shipped | Roadmap-level docs | `bernstein security-review` |
| Commit attribution stats | Shipped | Roadmap-level docs | Per-agent commit statistics |
| Away summary generation | Shipped | Roadmap-level docs | Summarize what happened while you were away |
| Plugin trust warning | Shipped | Roadmap-level docs | Warns on unverified plugins |
| Cumulative progress tracking | Shipped | Roadmap-level docs | Progress tracking across runs |

## CLI commands

| Command | Runtime status | Docs status | Notes |
|---|---|---|---|
| `bernstein -g GOAL` | Shipped | Full | Inline goal |
| `bernstein run plan.yaml` | Shipped | Full | Plan file execution |
| `bernstein init` | Shipped | Full | Workspace setup |
| `bernstein stop` | Shipped | Full | Graceful/force stop |
| `bernstein live` | Shipped | Full | TUI dashboard |
| `bernstein dashboard` | Shipped | Full | Web dashboard |
| `bernstein status` | Shipped | Full | Task summary |
| `bernstein ps` | Shipped | Full | Process list |
| `bernstein cost` | Shipped | Full | Spend breakdown |
| `bernstein doctor` | Shipped | Full | Pre-flight health check |
| `bernstein recap` | Shipped | Full | Post-run summary |
| `bernstein retro` | Shipped | Full | Retrospective report |
| `bernstein trace ID` | Shipped | Full | Decision trace |
| `bernstein logs` | Shipped | Full | Agent log tail |
| `bernstein diff ID` | Shipped | Full | Per-task git diff |
| `bernstein plan` | Shipped | Full | Task backlog |
| `bernstein replay ID` | Shipped | Brief | Deterministic replay |
| `bernstein checkpoint` | Shipped | Brief | Session snapshot |
| `bernstein wrap-up` | Shipped | Brief | End session with summary |
| `bernstein demo` | Shipped | Full | Zero-config demo |
| `bernstein quickstart` | Shipped | Brief | Flask TODO demo (3 tasks) |
| `bernstein agents ...` | Shipped | Full | Catalog management |
| `bernstein evolve ...` | Shipped | Full | Self-improvement |
| `bernstein ci fix` | Shipped | Full | CI autofix |
| `bernstein github setup` | Shipped | Full | GitHub App setup |
| `bernstein worker` | Shipped | Brief | Join cluster as worker |
| `bernstein mcp` | Shipped | Brief | Run as MCP server |
| `bernstein chaos` | Shipped | Brief | Fault injection |
| `bernstein audit` | Shipped | Brief | Cryptographic audit |
| `bernstein verify` | Shipped | Brief | Merkle/HAMC verification |
| `bernstein benchmark` | Shipped | Full | Benchmark suite |
| `bernstein eval` | Shipped | Brief | Evaluation harness |
| `bernstein ideate` | Shipped | Brief | Creative evolution |
| `bernstein workspace` | Shipped | Full | Multi-repo workspace |
| `bernstein config` | Shipped | Brief | Configuration management |
| `bernstein quarantine` | Shipped | Brief | Cross-run task quarantine |
| `bernstein cache` | Shipped | Brief | Response cache management |
| `bernstein test-adapter` | Shipped | Brief | Adapter smoke test |
| `bernstein add-task` | Shipped | Brief | Inject task via CLI |
| `bernstein cancel` | Shipped | Brief | Cancel task |
| `bernstein review/approve/reject/pending` | Shipped | Brief | Review workflow |
| `bernstein sync` | Shipped | Brief | Sync backlog with server |
| `bernstein manifest` | Shipped | Brief | Run manifest inspection |
| `bernstein gateway` | Shipped | Brief | MCP gateway proxy |
| `bernstein workflow` | Shipped | Brief | Workflow DSL |
| `bernstein watch` | Shipped | Brief | Directory file watcher |
| `bernstein listen` | Shipped | Brief | Voice commands (experimental) |
| `bernstein completions` | Shipped | Brief | Shell completion scripts |
| `bernstein self-update` | Shipped | Brief | Upgrade from PyPI |
| `bernstein plugins` | Shipped | Brief | List active plugins |
| `bernstein install-hooks` | Shipped | Brief | Install git hooks |
| `bernstein debug` | Shipped | Brief | Generate debug bundle for triage |

---

## Cloud / Cloudflare

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| Workers RuntimeBridge | Shipped | Full | `bridges/cloudflare.py` — agents on Workers + Durable Objects |
| Workflow Bridge (durable execution) | Shipped | Full | `bridges/cloudflare_workflow.py` — auto-retry, approval gates |
| Sandbox Bridge (V8/container isolation) | Shipped | Full | `bridges/cloudflare_sandbox.py` — isolated code execution |
| Browser Rendering Bridge | Shipped | Full | `bridges/browser_rendering.py` — screenshots, scraping, PDFs |
| R2 Workspace Sync | Shipped | Full | `bridges/r2_sync.py` — content-addressed delta sync |
| Workers AI Provider (free LLMs) | Shipped | Full | `core/routing/cloudflare_ai.py` — Llama, Mistral, Gemma, Qwen |
| D1 Analytics & Billing | Shipped | Full | `core/cost/d1_analytics.py` — usage metering, billing tiers |
| Vectorize Semantic Cache | Shipped | Full | `core/memory/vectorize_cache.py` — embedding-based response cache |
| MCP Remote Transport | Shipped | Full | `mcp/remote_transport.py` — streamable HTTP for remote MCP |
| Cloud CLI (`bernstein cloud`) | Shipped | Full | `cli/commands/cloud_cmd.py` — login, run, status, cost, deploy |
| Cloudflare Agents Adapter | Shipped | Full | `adapters/cloudflare_agents.py` — wrangler dev integration |
| Codex-on-Cloudflare Adapter | Shipped | Full | `adapters/codex_cloudflare.py` — Codex in CF sandboxes |

---

## Highest-priority doc gaps

1. Deep examples for retry/escalation and fallback behavior.
2. Explicit observability guide for Prometheus + OTLP deployment patterns.
3. End-to-end docs for trigger rule authoring and source configuration.
4. Clear operator runbooks for audit/verification command families.
5. User-facing docs for recently shipped TUI/UX features (output styles, diff folding, session tags, away summary, contextual tips).
