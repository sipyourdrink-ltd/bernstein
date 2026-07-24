# Feature Matrix

The exhaustive capability index the README links to as its full feature
matrix. Every row is verified against `src/bernstein/` on the current `main`.

The "Docs status" column reflects whether a page-level reference exists
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
| Fast-path execution | Brief | Trivial tasks skip the LLM agent entirely (`fast_path.py`) |
| Plan mode (human approval) | Full | `--plan-only`, `--from-plan`, approval routes |
| Headless mode | Full | `--headless` for CI/overnight |
| Dry-run mode | Full | `--dry-run` previews the plan without spawning |
| Typed activity boundary | Full | One hash-in/hash-out contract for coding, research, browser, and ops agents (`core/orchestration/activity.py`) |
| Missions (multi-phase goals) | Full | Phases run under isolated budget envelopes; a halted phase seals a halt receipt and leaves runnable siblings active (`core/orchestration/missions.py`) |
| Durable task suspend/resume | Full | A waiting task parks with an attested receipt that frees its seat, sandbox, and budget; resume reconstructs byte-identically (`core/tasks/suspension.py`) |
| Tournament runs | Brief | Parallel attempts selected by deterministic evaluators; the winner carries a signed selection receipt (`core/tournament/`) |
| Fleet steering | Brief | Pause, resume, guidance, redirect, and abort each land a signed steering receipt before any effect runs (`core/orchestration/steering.py`) |
| Detached run service | Brief | Submit a goal, disconnect, and reattach later against a supervised run service (`core/run_service/`) |
| Named resource pools | Brief | Lease-backed admission with chain-anchored grant and release receipts (`core/admission/`) |
| Spec-to-graph compile | Full | `bernstein plan compile` runs the draft/approve/compile pipeline offline into a gated task graph with a chain-anchored receipt (`plan_compile_cmd.py`) |

## State and persistence

| Capability | Docs status | Notes |
|---|---|---|
| File-based state in `.sdd/` | Full | Primary operating model |
| Metrics/trace persistence | Full | Paths documented, JSONL schema |
| Lessons/memory persistence | Brief | Stored and injected at spawn time |
| Storage backends (`memory/postgres/redis`) | Full | Config + doctor coverage |
| Session persistence (fast resume) | Brief | `session.py` resumes after stop/restart |
| Bulletin board (cross-agent messaging) | Brief | Append-only, used by agents for handoff |
| Content-addressed artifact store | Brief | Content-addressed deduplication for artifacts (`core/persistence/cas_store.py`) |
| Cloud artifact sinks | Full | Local, S3, GCS, Azure Blob, and R2 sinks behind an async `ArtifactSink` protocol |

## Observability

| Capability | Docs status | Notes |
|---|---|---|
| `/status` and task API | Full | Core API documented |
| Prometheus `/metrics` | Brief | Endpoint is real; Grafana dashboards are user-defined |
| OTLP telemetry initialization | Brief | Wiring exists in `core/observability/` |
| OTel GenAI span projection | Brief | `trace project` projects a run journal into signed OTel GenAI spans; `trace verify-projection` recomputes span ids |
| Live OTLP bridge | Full | The signed span projection streams to any OpenTelemetry collector live or via `telemetry export-otel`; `telemetry verify-span` recomputes a span id from its journal entry (`core/telemetry/`) |
| Retrospective reporting (`retro`) | Full | CLI coverage present |
| Cost analysis (`cost`, history/anomaly hooks) | Full | `bernstein cost`, cost anomaly detection active |
| Per-agent token progress | Brief | Tracked in `api_usage.py`, surfaced in `bernstein status` |
| Session analytics | Brief | `bernstein recap` shows session-level stats |
| Agent activity tracking | Brief | Activity metrics in `metrics/` |
| Debug bundle | Brief | `bernstein debug` collects logs/state/config for triage |

## Safety and governance

| Capability | Docs status | Notes |
|---|---|---|
| Quality gates (lint, type-check, tests) | Full | In the run flow; extended with coverage, benchmark, arch-conformance, and mutation-testing gates |
| PII scan quality gate | Brief | Active, auto-installed via `log_redact.py` |
| Rule enforcement (`.bernstein/rules.yaml`) | Full | Enforcement behavior documented |
| Log redaction (PII filter) | Brief | Active |
| Lethal-trifecta capability gate | Full | Taint-aware egress denial: a chain that unions private data, tainted input, and external comms is refused even when static tags would pass (`core/security/capability_matrix.py`) |
| Circuit breaker | Full | Halts misbehaving agents, writes SHUTDOWN signal |
| Token growth monitor | Brief | Auto-intervention on runaway consumption |
| Cost anomaly detection | Brief | Z-score based, acts via task completion |
| Peak-hour scheduling | Brief | `peak_hour_router.py` for cost-aware time-of-day routing |
| Agent loop detection | Brief | Kills agents in edit-loop cycles |
| Deadlock detection | Brief | Wait-for graph, automatic victim selection |
| Cross-model verification | Brief | A different model reviews completed diffs (opt-in) |
| Behaviour anomaly detection | Brief | Flags agents whose runtime metrics deviate statistically from baseline (`core/observability/behavior_anomaly.py`) |
| Context degradation detector | Brief | Monitors quality over time, restarts when degraded |
| Progressive permission prompts | Brief | Per-agent permission levels |

## Verifiability and provenance

| Capability | Docs status | Notes |
|---|---|---|
| Unified lineage spine | Full | The journal head is sealed into a single lineage root at run finalization; artifact provenance and replay identity share that root (`core/lineage/`) |
| Always-on event journal | Full | Merkle-chained per-run `EventJournal`; the head hash is the run identity (`core/replay/journal.py`) |
| HMAC-chained audit log | Full | Tamper-evident, daily rotation, cross-process append lock (`core/security/audit_chain.py`) |
| Execution WAL | Brief | Hash-chained write-ahead log for crash recovery and determinism fingerprinting |
| Deterministic replay + fork | Full | `replay --verify` recomputes the journal head; `fork --from-step` rebuilds state at a journal step into an isolated run (`core/replay/`) |
| Audit-chain export | Full | Projects a chain range into COSE_Sign1, in-toto, and DSSE receipts re-verifiable offline with no Bernstein imports (`core/security/audit_export.py`) |
| Provenance trust classes | Full | Every tool result carries a trust class; effective trust is the minimum over the lineage closure, recomputed offline by `audit taint` (`core/lineage/provenance.py`) |
| Gate adjudication records | Brief | Gate panel decisions are recomputable; `gate verify` confirms the inputs hash |
| In-process verification gates | Brief | Verification-gate hooks rendered into capable adapters so the check runs inside the agent (`adapters/hook_gate_render.py`) |
| Evidence bundles | Full | Sealed verification-evidence bundle, projectable as a tracker comment (`core/evidence/bundle.py`) |
| Intent capsules | Full | Approval compiles the goal into a signed capsule; a deterministic drift monitor escalates divergence, verified by `intent verify` (`core/security/intent_capsule.py`) |
| Query receipts (datasources) | Full | Read-only SQL results become content-addressed signed receipts; `datasource verify --re-execute` reports MATCH or DRIFT (`core/datasources/`) |
| Compaction receipts | Full | Context and template compaction is recorded as a chain-anchored, reversible receipt (`core/tokens/compaction_receipt.py`) |
| Tamper-evident memory | Full | Memory entries are hash-chained with provenance; `memory verify/why/forget` proves authorship, traces origin, and tombstones (`core/memory/chain.py`) |
| Review / autofix / escalation / consent / webhook-node receipts | Brief | Signed, journal-anchored receipts verified offline (`review-receipt verify`, `escalation verify`, `webhook verify`) |
| Stall escalation receipts | Brief | A stalled worker produces a signed escalation receipt embedding the last audit entries and a deterministic recommended action (`supervisor escalate`) |
| C2PA content credentials | Full | Artifact lineage projected into signed C2PA credentials (`credential emit/verify`) |
| Skill install receipts | Full | Install and usage links recomputable via `skill verify` / `skill provenance` |
| Adapter security-floor receipts | Full | A below-floor adapter spawn is refused with a content-addressed, chain-anchored refusal receipt (`adapters/security_floor.py`) |
| Endpoint certification | Full | Local-model workers are conformance-tested and issued a signed certification (`core/endpoints/certification.py`) |
| SLA violation receipts | Full | Per-goal SLA contracts evaluated read-only each tick; a breach becomes a signed, offline-verifiable violation receipt (`core/planning/sla_store.py`) |
| Signed daily mission digests | Full | Each day's mission progress is a signed digest projecting that day's chain; `mission digest verify` re-derives it (`core/orchestration/mission_digest.py`) |
| Agent run manifest | Brief | Hashable workflow spec for SOC 2 evidence |
| RBAC / budget / seat projections | Brief | Access and budget verdicts re-derivable via `governance verify` |
| Unified event feed | Brief | Chain-projected event feed with a typed grammar and per-event receipts (`core/events/feed.py`) |

## Identity and delegation

| Capability | Docs status | Notes |
|---|---|---|
| Native subagent delegation | Brief | The scheduler delegates leaf execution to native subagents; schema-validated results anchor as `subagent.delegation` journal entries, chain verified via `delegation verify` (`core/agents/subagent_delegation.py`) |
| Attenuated capability tokens | Brief | Each delegation hop is a signed, scope-attenuating token, so the principal to orchestrator to sub-agent authority chain verifies offline (`core/security/capability_tokens.py`) |
| Signed agent cards | Full | `AgentIdentityCard` signed for A2A federation and served at `/.well-known/agent-card.json` (`core/security/agent_card_signer.py`) |
| A2A node + message receipts | Full | Callable JSON-RPC node; a completed task returns an artifact whose parts carry a lineage receipt verified by `a2a verify` (`core/interop/a2a_card.py`) |
| HTTP message signatures | Full | RFC 9421 Ed25519 signatures on outbound agent-facing requests; keys served as JWKS via `identity keydir` (`core/identity/http_signing.py`) |
| SPIFFE workload identity | Full | SPIFFE/SVID workload identity with `spiffe id` and `spiffe verify-binding` (`core/identity/spiffe/`) |

## Payments and cost governance

| Capability | Docs status | Notes |
|---|---|---|
| Spending mandates | Full | Authorized-spend mandates enforced before a paid action, with signed spend receipts (`core/payments/`) |
| Authorized-action mandates | Brief | `mandate emit/verify/revoke` binds, proves, and revokes an authorized-action mandate |
| Cost-policy receipts | Full | The live dispatch gate records batch, cache, and model policy verdicts as receipts (`core/cost/scheduling/receipt.py`) |
| Budget envelopes | Brief | Per-phase and per-task budget envelopes with rollup by envelope (`core/cost/cost_rollup_by_envelope.py`) |

## Ecosystem and integrations

| Capability | Docs status | Notes |
|---|---|---|
| Agent catalog/discovery | Full | `bernstein agents sync/list/discover/match/showcase` (40+ CLI agent adapters) |
| Browser / computer-use adapter | Full | Adapter family for autonomous browser and computer-use agents; every action is a content-addressed lineage anchor (`adapters/computer_use.py`) |
| GitHub App and CI fix flows | Full | `bernstein ci fix <url>`, `github setup` |
| Trigger sources | Brief | `github`, `gitlab`, `slack`, `discord`, `file_watch`, `webhook`, `odata`, and `schedule` source adapters (`core/trigger_sources/`) |
| OData trigger source | Brief | Polls an OData endpoint into normalized trigger events (`core/trigger_sources/odata_poll.py`) |
| Webhook node | Brief | Outbound webhook node with recomputable inbound-event and node hashes (`core/trigger_sources/webhook_node.py`, `webhook verify`) |
| Automation bridge | Full | Signed trigger receipts and chain-anchored status callbacks for external automations (`core/trigger_sources/automation_platforms.py`) |
| Plugin hooks (pluggy) | Full | SDK docs in CONTRIBUTING.md |
| Cluster/worker primitives | Full | `bernstein worker --server URL`, cluster routes documented |
| Multi-repo workspaces | Full | `workspace:` in bernstein.yaml, workspace CLI |
| MCP server mode | Brief | `bernstein mcp`, MCP server in `mcp/server.py` |
| MCP tool registry | Brief | Auto-discovery and per-task config |
| MCP catalog client | Brief | `bernstein mcp catalog browse/search/install` installable server catalog (`core/protocols/mcp_catalog/`) |
| MCP input contracts | Brief | Schema-validated, deny-by-default MCP tool-call input firewall (`mcp/input_validation.py`) |
| Stateless MCP anchoring | Brief | A stateless MCP client can poll a run and verify it offline; calls anchor as `mcp.stateless_call` journal entries (`core/protocols/mcp/stateless_core.py`) |
| Runtime capability cards (MCP) | Brief | Per-server capability cards for the MCP server (`mcp/capability.py`) |
| ACP native bridge | Full | `bernstein acp serve --stdio\|--http :PORT` IDE-native bridge (`core/protocols/acp/`); see `reference/acp-bridge.md` |
| Protocol negotiation | Brief | `protocol_negotiation.py` runtime protocol-version handshake |
| Schema registry | Brief | `schema_registry.py` versioned message schemas for protocols |
| Credential vault | Brief | `bernstein connect <provider>`, `bernstein creds list/revoke/test` OS-keychain token storage (`core/security/vault/`) |
| Autofix CI daemon | Brief | `bernstein autofix start/stop/status/attach` watches PRs and dispatches repair runs on CI failure (`core/autofix/`) |
| Dev preview | Brief | `bernstein preview start/stop/list/status` exposes an agent dev server via tunnel with configurable auth (`core/preview/`) |
| Fleet dashboard | Brief | `bernstein fleet [--web HOST:PORT]` cross-session multi-instance view (`core/fleet/`) |
| Notification sinks | Brief | `bernstein notify test --sink <id>` pluggable notification backends (`core/notifications/`) |
| PR review responder | Brief | `bernstein review-responder start/status/tick` auto-responds to PR review comments (`core/review_responder/`) |
| Review pipeline DSL | Brief | `bernstein review --pipeline review.yaml` YAML-driven multi-phase review (`core/quality/review_pipeline/`) |
| Plan archival | Brief | `bernstein plan ls/show` list and inspect archived plans (`core/planning/lifecycle.py`) |
| Slack integration | Brief | Slash commands and events API endpoints |
| Webhook ingestion | Brief | `POST /webhooks/` for external event routing |
| Adaptive parallelism | Brief | Auto-tunes concurrency from observed success rates (`core/orchestration/adaptive_parallelism.py`) |
| Warm pool | Brief | Pre-spawned agent pool to cut spawn latency (`core/agents/warm_pool.py`) |
| Pluggable sandbox backends | Full | Worktree, Docker, E2B, and Modal backends behind a `SandboxSession` protocol |
| microVM sandbox backend | Brief | Isolation tier with content-addressed snapshots for deterministic fork-and-race (`--sandbox microvm`) |
| Named sandbox pools | Full | Chain-projected pool manifests with capability and egress ceilings and signed worker enrolment (`core/sandbox/pool.py`) |
| Cache policy engine | Full | Content-addressed key recipes, drift expiry, and fleet dedup with signed duplicate-of receipts (`core/persistence/cache_policy.py`) |
| Sovereign deployment profile | Full | Signed residency-posture attestation; posture drift at spawn is a signed refusal (`core/security/deployment_profile.py`) |
| Workflow DSL | Brief | `bernstein workflow validate/list/show` |
| Chaos engineering | Brief | `bernstein chaos agent-kill/rate-limit/file-remove/status/slo` |
| Benchmark suite | Full | `bernstein benchmark run/compare/swe-bench` |
| Eval harness | Brief | `bernstein eval run/report/failures` |
| SWE-Bench harness | Full | Verified eval in `benchmarks/swe_bench/run.py` |
| Graduation system | Brief | Agent promotion stages, routes in `routes/graduation.py` |
| Semantic caching | Brief | `semantic_cache.py` prompt deduplication |
| Cascade router (intra-Claude tier escalation) | Brief | Tier escalation within a single provider (`core/routing/cascade_router.py`) |
| Cascade fallback manager (cross-adapter failover) | Brief | Cross-adapter provider failover (`core/routing/cascade.py`) |
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
| `bernstein plan compile` | Full | Compile a spec into a gated task graph offline |
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
| `bernstein audit` | Brief | Cryptographic audit (`seal/verify/export/taint/pack/slice`) |
| `bernstein verify` | Brief | Merkle/HMAC verification |
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
| `bernstein fleet steer` | Brief | Mid-run steering: pause/resume/guidance/redirect/abort |
| `bernstein mcp catalog ...` | Brief | MCP catalog browser (browse/search/install) |
| `bernstein notify test` | Brief | Notification sink smoke test |
| `bernstein plan ls/show` | Brief | List and inspect archived plans |
| `bernstein review-responder ...` | Brief | PR review responder (start/status/tick) |
| `bernstein review --pipeline` | Brief | Review with YAML pipeline DSL |
| `bernstein fork --run --from-step` | Brief | Fork a run at a journal step into a new isolated run |
| `bernstein gate verify` | Brief | Recompute a gate panel's inputs hash and confirm the adjudication |
| `bernstein mandate emit/verify/revoke` | Brief | Bind, prove, and revoke authorized-action mandates |
| `bernstein payment-mandate issue/show/spend` | Full | Issue and spend against authorized-spend mandates with signed receipts |
| `bernstein governance verify` | Brief | Recompute access and budget verdicts for a run |
| `bernstein webhook verify` | Brief | Recompute inbound-event and outbound webhook-node hashes |
| `bernstein review-receipt emit/verify` | Brief | Bind and offline-verify PR review receipts (issue + plan + tool calls + diff) |
| `bernstein escalation show/verify` | Brief | Project and reconstruct escalation receipts from the journal |
| `bernstein supervisor status/escalate` | Brief | Supervise stalled workers and seal stall escalation receipts |
| `bernstein delegation verify` | Brief | Reconstruct and verify a run's delegation chain |
| `bernstein credential emit/verify` | Brief | Project an artifact's lineage into a signed C2PA credential and verify it |
| `bernstein skill provenance/verify` | Brief | Recompute a skill's install receipt and usage-provenance graph |
| `bernstein schedule verify/audit`, `schedule show --at` | Brief | Replay recorded fires, chain-check fire receipts, project a schedule at a time |
| `bernstein sla add/list/show/verify/report` | Full | Attach per-goal SLA contracts; a breach is an offline-verifiable violation receipt |
| `bernstein trace project/verify-projection` | Brief | Project a run journal into signed OTel GenAI spans and verify the projection |
| `bernstein telemetry export-otel/verify-span` | Full | Stream the span projection to an OTLP collector and verify a single span offline |
| `bernstein thread verify` | Brief | Prove a streamed TUI thread equals its executed journal |
| `bernstein memory verify/why/forget` | Full | Prove authorship, trace origin, and tombstone a memory entry |
| `bernstein replay --verify/--from-step` | Brief | Recompute the journal head or rebuild state to a step |
| `bernstein intent show/verify` | Full | Project and recompute an intent capsule's conformance offline |
| `bernstein a2a verify/publish` | Full | Verify an A2A message receipt and publish the agent card |
| `bernstein activity verify` | Full | Verify a typed activity's replay hashes (`activity browser run` for browser checks) |
| `bernstein datasource register/query/verify` | Full | Register read-only SQL datasources and verify content-addressed query receipts |
| `bernstein evidence show/verify` | Brief | Project and verify a sealed evidence bundle |
| `bernstein events query/verify` | Brief | Query the unified event feed and verify its chain projection |
| `bernstein endpoints certify/verify` | Full | Conformance-certify a local-model endpoint and verify its certification |
| `bernstein ledger verify/anchor/fetch` | Full | Verify, anchor, and fetch work-ledger segments |
| `bernstein mission define/status/verify` | Full | Define multi-phase missions and verify mission status (`mission digest verify` for digests) |
| `bernstein tournament show/verify` | Brief | Inspect a tournament run and verify its selection receipt |
| `bernstein spiffe id/verify-binding` | Full | Print the SPIFFE id and verify a workload-identity binding |
| `bernstein spec check/auto-fix` | Full | Evaluate and auto-fix a spec against the quality checklist |
| `bernstein run-service submit/attach/status` | Brief | Submit a detached run, then reattach to it later |
| `bernstein compaction log` | Full | Inspect chain-anchored compaction receipts |
| `bernstein identity keydir/decode/verify` | Full | Print the JWKS key directory and decode/verify install identity |
| `bernstein pool register/list/show/verify` | Brief | Manage lease-backed named resource pools |

---

## Cloud / Cloudflare

| Capability | Docs status | Notes |
|---|---|---|
| Workers RuntimeBridge | Full | `bridges/cloudflare.py` agents on Workers + Durable Objects |
| Workflow Bridge (durable execution) | Full | `bridges/cloudflare_workflow.py` auto-retry, approval gates |
| Sandbox Bridge (V8/container isolation) | Full | `bridges/cloudflare_sandbox.py` isolated code execution |
| Browser Rendering Bridge | Full | `bridges/browser_rendering.py` screenshots, scraping, PDFs |
| R2 Workspace Sync | Full | `bridges/r2_sync.py` content-addressed delta sync |
| Workers AI Provider (free LLMs) | Full | `core/routing/cloudflare_ai.py` Llama, Mistral, Gemma, Qwen |
| D1 Analytics & Billing | Full | `core/cost/d1_analytics.py` usage metering, billing tiers |
| MCP Remote Transport | Full | `mcp/remote_transport.py` streamable HTTP for remote MCP |
| Cloud CLI (`bernstein cloud`) | Full | `cli/commands/cloud_cmd.py` login, run, status, cost, deploy |
| Cloudflare Agents Adapter | Brief | `adapters/cloudflare_agents.py` experimental; refuses fast with an actionable error rather than running |
| Codex-on-Cloudflare Adapter | Brief | `adapters/codex_cloudflare.py` experimental; targets a REST API that does not yet exist and refuses fast |
