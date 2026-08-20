# Feature Matrix

The exhaustive capability index the README links to as its full feature
matrix. Every row is verified against `src/bernstein/` on the current `main`.

The "Docs status" column reflects whether a page-level reference exists
(`Full`) or whether the capability is documented in source / module
docstrings only (`Brief`). `Full` rows link to their reference page.

Rows still marked `Brief` are deliberate: the capability is reachable from
the surface named in its Notes column, but is documented in source and
module docstrings rather than on its own reference page. Every row names a
surface an operator can invoke. `tests/unit/test_cli_server_route_parity.py`
additionally holds the CLI's server calls to the registered route table.

The "Maturity" column says how far a capability has been proven, which is a
different question from whether a page documents it:

| Maturity | Meaning |
|---|---|
| 5 | End-to-end evidence, no open correctness issues, docs current |
| 4 | Tested and documented, minor gaps |
| 3 | Works, known gaps |
| 2 | A first-run path is broken |
| 1 | Stub |

The score describes the subsystem behind the row, so rows served by the same
code usually carry the same number. `bernstein audit` and the audit-chain
capability rows move together; so do a CLI verb and the capability it drives.

`tests/unit/test_feature_matrix_drift.py` holds the CLI-commands section to
the commands the code registers: a new command with no row here fails that
test, and a row naming a command the CLI no longer registers fails it too.

---

## Core orchestration

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| Goal-based run (`-g`) | Full | 3 | Main entry flow |
| Seed-file run (`bernstein.yaml`) | Full | 3 | Auto-discovery supported |
| Plan-file execution (`stages`/`steps`) | Full | 3 | `bernstein run plan.yaml` |
| Retry + escalation plumbing | Full | 3 | In task lifecycle, with configurable retries |
| Completion verification (janitor + signals) | Full | 3 | API + getting started coverage |
| Process-aware stop/drain | Full | 3 | Graceful and force stop, drain mode |
| [Multi-cell orchestration](../orchestration/multi-cell.md) | Full | 3 | Implemented in `multi_cell.py` |
| [Fast-path execution](../architecture/fast-path-execution.md) | Full | 3 | Trivial tasks skip the LLM agent entirely (`fast_path.py`) |
| Plan mode (human approval) | Full | 3 | `--plan-only`, `--from-plan`, approval routes |
| Headless mode | Full | 3 | `--headless` for CI/overnight |
| Dry-run mode | Full | 3 | `--dry-run` previews the plan without spawning |
| [Typed activity boundary](../operations/activity-boundary.md) | Full | 3 | One hash-in/hash-out contract for coding, research, browser, data, and ops activities, verified by `bernstein activity verify <run>` (`core/orchestration/activity.py`). Browser is the one non-coding modality with a CLI dispatch verb (`bernstein activity browser run`); the rest are Python-API only. Dispatching a non-coding modality from a seed, plan, or backlog file is not wired yet; see [Reachability today](../operations/activity-boundary.md#reachability-today) |
| Missions (multi-phase goals) | Full | 3 | Phases run under isolated budget envelopes; a halted phase seals a halt receipt and leaves runnable siblings active (`core/orchestration/missions.py`) |
| Durable task suspend/resume | Full | 3 | A waiting task parks with an attested receipt that frees its seat, sandbox, and budget; resume reconstructs byte-identically (`core/tasks/suspension.py`) |
| [Tournament runs](../operations/tournament-runs.md) | Full | 3 | Parallel attempts selected by deterministic evaluators; the winner carries a signed selection receipt (`core/tournament/`) |
| [Fleet steering](../operations/fleet-steering.md) | Full | 3 | Pause, resume, guidance, redirect, and abort each land a signed steering receipt before any effect runs (`core/orchestration/steering.py`) |
| [Detached run service](../operations/run-service.md) | Full | 3 | Submit a goal, disconnect, and reattach later against a supervised run service (`core/run_service/`) |
| [Named resource pools](../operations/named-resource-pools.md) | Full | 3 | Lease-backed admission with chain-anchored grant and release receipts (`core/admission/`) |
| Spec-to-graph compile | Full | 3 | `bernstein plan compile` runs the draft/approve/compile pipeline offline into a gated task graph with a chain-anchored receipt (`plan_compile_cmd.py`) |

## State and persistence

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| File-based state in `.sdd/` | Full | 3 | Primary operating model |
| Metrics/trace persistence | Full | 3 | Paths documented, JSONL schema |
| [Lessons/memory persistence](../concepts/lesson-persistence.md) | Full | 4 | Stored and injected at spawn time |
| Storage backends (`memory/postgres/redis`) | Full | 3 | Config + doctor coverage |
| [Session persistence (fast resume)](../operations/session-fast-resume.md) | Full | 3 | `session.py` resumes after stop/restart |
| [Bulletin board (cross-agent messaging)](../orchestration/bulletin-board.md) | Full | 3 | Append-only, used by agents for handoff |
| [Content-addressed artifact store](../architecture/cas-store.md) | Full | 3 | Content-addressed deduplication for artifacts (`core/persistence/cas_store.py`) |
| Cloud artifact sinks | Full | 3 | Local, S3, GCS, Azure Blob, and R2 sinks behind an async `ArtifactSink` protocol |

## Observability

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| `/status` and task API | Full | 3 | Core API documented |
| [Prometheus `/metrics`](../operations/observability-overview.md) | Full | 3 | Endpoint is real; Grafana dashboards are user-defined |
| [OTLP telemetry initialization](../operations/observability-overview.md) | Full | 3 | Wiring exists in `core/observability/` |
| [OTel GenAI span projection](../operations/observability-overview.md) | Full | 3 | `trace project` projects a run journal into signed OTel GenAI spans; `trace verify-projection` recomputes span ids |
| Live OTLP bridge | Full | 3 | The signed span projection streams to any OpenTelemetry collector live or via `telemetry export-otel`; `telemetry verify-span` recomputes a span id from its journal entry (`core/telemetry/`) |
| Retrospective reporting (`retro`) | Full | 3 | CLI coverage present |
| Cost analysis (`cost`, history/anomaly hooks) | Full | 4 | `bernstein cost`, cost anomaly detection active |
| [Per-agent token progress](../observability/token-progress.md) | Full | 3 | Tracked in `api_usage.py`, surfaced in `bernstein status` |
| [Session analytics](../operations/session-analytics.md) | Full | 3 | `bernstein recap` shows session-level stats |
| [Debug bundle](../operations/debug-bundle.md) | Full | 4 | `bernstein debug` collects logs/state/config for triage |

## Safety and governance

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| Quality gates (lint, type-check, tests) | Full | 3 | In the run flow; extended with coverage, benchmark, arch-conformance, and mutation-testing gates |
| [PII scan quality gate](../security/pii-scan-gate.md) | Full | 3 | Active, auto-installed via `log_redact.py` |
| Rule enforcement (`.bernstein/rules.yaml`) | Full | 3 | Enforcement behavior documented |
| [Log redaction (PII filter)](../security/log-redaction.md) | Full | 3 | Active |
| Lethal-trifecta capability gate | Full | 3 | Taint-aware egress denial: a chain that unions private data, tainted input, and external comms is refused even when static tags would pass (`core/security/capability_matrix.py`) |
| Circuit breaker | Full | 3 | Halts misbehaving agents, writes SHUTDOWN signal |
| [Token growth monitor](../operations/token-growth-monitor.md) | Full | 3 | Auto-intervention on runaway consumption |
| [Cost anomaly detection](../operations/cost-anomaly-detection.md) | Full | 3 | Z-score based, acts via task completion |
| [Agent loop detection](../operations/agent-loop-detection.md) | Full | 3 | Kills agents in edit-loop cycles |
| [Deadlock detection](../architecture/deadlock-detection.md) | Full | 3 | Wait-for graph, automatic victim selection |
| [Cross-model verification](../architecture/quality-pipeline.md) | Full | 3 | A different model reviews completed diffs (opt-in) |
| [Behaviour anomaly detection](../operations/observability-overview.md) | Full | 3 | Flags agents whose runtime metrics deviate statistically from baseline (`core/observability/behavior_anomaly.py`) |
| [Context degradation detector](../architecture/context-degradation-detector.md) | Full | 3 | Monitors quality over time, restarts when degraded |
| Agent trust tiers | Brief | 3 | `bernstein agents trust`; tiers accrue from task outcomes in `.sdd/trust/` and map to an `AgentPermissions` profile (`core/agents/agent_trust.py`) |
| [Volunteer project manifest](volunteer-manifest.md) | Full | 3 | A project's opt-in policy — OSI licence, acceptance gates, path scope, egress, sandbox floor — loaded and content-addressed by `core/volunteer/manifest.py`; validate one with `bernstein volunteer verify` |
| [Volunteer sandbox profile](volunteer-sandbox.md) | Full | 3 | Deny-all-egress containment derived from the manifest and the donor's own limits; refusals are records, not log lines (`core/volunteer/sandbox_profile.py`) |
| [Volunteer issue text](volunteer-issue-text.md) | Full | 3 | Untrusted issue title and body normalised into one delimited block before it becomes an agent prompt — HTML comments (closed and unterminated) stripped, invisible and bidirectional characters dropped, NFKC, and a content-derived fence the text cannot forge (`core/volunteer/issue_sanitize.py`) |

## Verifiability and provenance

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| Unified lineage spine | Full | 4 | When the finalization path emits an external journal seal, artifact provenance and finished-journal identity share that independently committed root (`core/lineage/`) |
| Always-on event journal | Full | 3 | Merkle-chained per-run `EventJournal`; the head identifies the surviving state, while an external seal establishes complete finished-journal identity (`core/replay/journal.py`) |
| HMAC-chained audit log | Full | 4 | Tamper-evident, daily rotation, cross-process append lock (`core/security/audit_chain.py`) |
| [Execution WAL](../architecture/state-persistence.md) | Full | 3 | Hash-chained write-ahead log for crash recovery and determinism fingerprinting |
| Deterministic replay + fork | Full | 3 | `replay --verify` recomputes the journal head; `fork --from-step` rebuilds state at a journal step into an isolated run (`core/replay/`) |
| Audit-chain export | Full | 4 | Projects a chain range into COSE_Sign1, in-toto, and DSSE receipts re-verifiable offline with no Bernstein imports (`core/security/audit_export.py`) |
| Provenance trust classes | Full | 4 | Every tool result carries a trust class; effective trust is the minimum over the lineage closure, recomputed offline by `audit taint` (`core/lineage/provenance.py`) |
| [Gate adjudication records](../operations/gate-adjudication.md) | Full | 3 | Gate panel decisions are recomputable; `gate verify` confirms the inputs hash |
| [In-process verification gates](../operations/hook-gate.md) | Full | 3 | Verification-gate hooks rendered into capable adapters so the check runs inside the agent (`adapters/hook_gate_render.py`) |
| Evidence bundles | Full | 3 | Sealed verification-evidence bundle, projectable as a tracker comment (`core/evidence/bundle.py`) |
| Intent capsules | Full | 3 | Approval compiles the goal into a signed capsule; a deterministic drift monitor escalates divergence, verified by `intent verify` (`core/security/intent_capsule.py`) |
| Query receipts (datasources) | Full | 3 | Read-only SQL results become content-addressed signed receipts; `datasource verify --re-execute` reports MATCH or DRIFT (`core/datasources/`) |
| Compaction receipts | Full | 3 | Context and template compaction is recorded as a chain-anchored, reversible receipt (`core/tokens/compaction_receipt.py`) |
| Tamper-evident memory | Full | 4 | Memory entries are hash-chained with provenance; `memory verify/why/forget` proves authorship, traces origin, and tombstones (`core/memory/chain.py`) |
| [Review / autofix / escalation / consent / webhook-node receipts](../operations/review-receipts.md) | Full | 3 | Signed, journal-anchored receipts verified offline (`review-receipt verify`, `escalation verify`, `webhook verify`) |
| Result receipt bundles | Brief | 3 | A worker submission's patch, gate logs, task ref, and sandbox selection sealed into one DSSE / in-toto envelope; `receipt verify` recomputes it offline and names the field that diverged (`core/security/result_receipt_bundle.py`) |
| [Stall escalation receipts](../operations/stall-escalation.md) | Full | 3 | A stalled worker produces a signed escalation receipt embedding the last audit entries and a deterministic recommended action (`supervisor escalate`) |
| C2PA content credentials | Full | 3 | Artifact lineage projected into signed C2PA credentials (`credential emit/verify`) |
| Skill install receipts | Full | 3 | Install and usage links recomputable via `skill verify` / `skill provenance` |
| Adapter security-floor receipts | Full | 3 | A below-floor adapter spawn is refused with a content-addressed, chain-anchored refusal receipt (`adapters/security_floor.py`) |
| Endpoint certification | Full | 3 | Local-model workers are conformance-tested and issued a signed certification (`core/endpoints/certification.py`) |
| SLA violation receipts | Full | 3 | Per-goal SLA contracts evaluated read-only each tick; a breach becomes a signed, offline-verifiable violation receipt (`core/planning/sla_store.py`) |
| Signed daily mission digests | Full | 3 | Each day's mission progress is a signed digest projecting that day's chain; `mission digest verify` re-derives it (`core/orchestration/mission_digest.py`) |
| [Agent run manifest](../operations/run-manifest.md) | Full | 3 | Hashable workflow spec for SOC 2 evidence |
| [RBAC / budget / seat projections](../operations/governance.md) | Full | 3 | Access and budget verdicts re-derivable via `governance verify` |
| [Unified event feed](../events/grammar.md) | Full | 3 | Chain-projected event feed with a typed grammar and per-event receipts (`core/events/feed.py`) |

## Identity and delegation

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| [Native subagent delegation](../architecture/subagent-delegation.md) | Full | 4 | The scheduler delegates leaf execution to native subagents; schema-validated results anchor as `subagent.delegation` journal entries, chain verified via `delegation verify` (`core/agents/subagent_delegation.py`) |
| [Attenuated capability tokens](../operations/security-and-identity.md) | Full | 4 | Each delegation hop is a signed, scope-attenuating token, so the principal to orchestrator to sub-agent authority chain verifies offline (`core/security/capability_tokens.py`) |
| Signed agent cards | Full | 4 | `AgentIdentityCard` signed for A2A federation and served at `/.well-known/agent-card.json` (`core/security/agent_card_signer.py`) |
| A2A node + message receipts | Full | 4 | Callable JSON-RPC node; a completed task returns an artifact whose parts carry a lineage receipt verified by `a2a verify` (`core/interop/a2a_card.py`) |
| HTTP message signatures | Full | 4 | RFC 9421 Ed25519 signatures on outbound agent-facing requests; keys served as JWKS via `identity keydir` (`core/identity/http_signing.py`) |
| SPIFFE workload identity | Full | 4 | SPIFFE/SVID workload identity with `spiffe id` and `spiffe verify-binding` (`core/identity/spiffe/`) |

## Payments and cost governance

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| Spending mandates | Full | 3 | Authorized-spend mandates enforced before a paid action, with signed spend receipts (`core/payments/`) |
| [Authorized-action mandates](../operations/spending-mandates.md) | Full | 3 | `mandate emit/verify/revoke` binds, proves, and revokes an authorized-action mandate |
| Cost-policy receipts | Full | 4 | The live dispatch gate records batch, cache, and model policy verdicts as receipts (`core/cost/scheduling/receipt.py`) |
| [Budget envelopes](../operations/cost-envelopes.md) | Full | 4 | Per-phase and per-task budget envelopes with rollup by envelope (`core/cost/cost_rollup_by_envelope.py`) |

## Ecosystem and integrations

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| Agent catalog/discovery | Full | 3 | `bernstein agents sync/list/discover/match/showcase` (40+ CLI agent adapters) |
| Browser / computer-use adapter | Full | 3 | Adapter family for autonomous browser and computer-use agents; every action is a content-addressed lineage anchor (`adapters/computer_use.py`) |
| GitHub App and CI fix flows | Full | 3 | `bernstein ci fix <url>`, `github setup` |
| [Trigger sources](../operations/trigger-sources.md) | Full | 3 | `github`, `gitlab`, `slack`, `discord`, `file_watch`, `webhook`, `odata`, and `schedule` source adapters (`core/trigger_sources/`) |
| [OData trigger source](../operations/odata.md) | Full | 3 | Polls an OData endpoint into normalized trigger events (`core/trigger_sources/odata_poll.py`) |
| [Webhook node](../operations/webhook-node.md) | Full | 3 | Outbound webhook node with recomputable inbound-event and node hashes (`core/trigger_sources/webhook_node.py`, `webhook verify`) |
| Automation bridge | Full | 3 | Signed trigger receipts and chain-anchored status callbacks for external automations (`core/trigger_sources/automation_platforms.py`) |
| Plugin hooks (pluggy) | Full | 3 | SDK docs in CONTRIBUTING.md |
| Cluster/worker primitives | Full | 3 | `bernstein worker --server URL`, cluster routes documented. A worker registering against a live cluster-enabled server is covered by `tests/integration/test_first_run_long_running_surfaces.py`, which is the same code path the `bernstein worker` CLI row rests on. |
| Multi-repo workspaces | Full | 3 | `workspace:` in bernstein.yaml, workspace CLI |
| [MCP server mode](../mcp/server.md) | Full | 3 | `bernstein mcp`, MCP server in `mcp/server.py` |
| [MCP tool registry](../integrations/mcp-server-injection.md) | Full | 3 | Auto-discovery and per-task config |
| [MCP catalog client](mcp-catalog.md) | Full | 3 | `bernstein mcp catalog browse/search/install` installable server catalog (`core/protocols/mcp_catalog/`) |
| [MCP input contracts](../mcp/input-validation.md) | Full | 3 | Schema-validated, deny-by-default MCP tool-call input firewall (`mcp/input_validation.py`); the enforced schema is also the advertised `inputSchema` |
| [Stateless MCP anchoring](../mcp/server.md) | Full | 3 | A stateless MCP client can poll a run and verify it offline; calls anchor as `mcp.stateless_call` journal entries (`core/protocols/mcp/stateless_core.py`) |
| [Runtime capability cards (MCP)](../mcp/server.md) | Full | 3 | Per-server capability cards for the MCP server (`mcp/capability.py`) |
| ACP native bridge | Full | 3 | `bernstein acp serve --stdio\|--http :PORT` IDE-native bridge (`core/protocols/acp/`); see `reference/acp-bridge.md` |
| [Protocol negotiation](../architecture/protocol-negotiation.md) | Full | 3 | `protocol_negotiation.py` runtime protocol-version handshake |
| [Schema registry](../architecture/schema-registry.md) | Full | 3 | `schema_registry.py` versioned message schemas for protocols |
| [Credential vault](../operations/secrets.md) | Full | 3 | `bernstein connect <provider>`, `bernstein creds list/revoke/test` OS-keychain token storage (`core/security/vault/`) |
| [Autofix CI daemon](../operations/autofix.md) | Full | 3 | `bernstein autofix start/stop/status/attach` watches PRs and dispatches repair runs on CI failure (`core/autofix/`) |
| [Dev preview](../operations/preview.md) | Full | 3 | `bernstein preview start/stop/list/status` exposes an agent dev server via tunnel with configurable auth (`core/preview/`) |
| [Fleet dashboard](../operations/fleet.md) | Full | 3 | `bernstein fleet [--web HOST:PORT]` cross-session multi-instance view (`core/fleet/`) |
| [Notification sinks](../operations/notifications.md) | Full | 3 | `bernstein notify test --sink <id>` pluggable notification backends (`core/notifications/`) |
| [PR review responder](../operations/review-responder.md) | Full | 3 | `bernstein review-responder start/status/tick` auto-responds to PR review comments (`core/review_responder/`) |
| [Review pipeline DSL](../operations/review-pipeline.md) | Full | 3 | `bernstein review --pipeline review.yaml` YAML-driven multi-phase review (`core/quality/review_pipeline/`) |
| [Plan archival](../operations/plan-archival.md) | Full | 3 | `bernstein plan ls/show` list and inspect archived plans (`core/planning/lifecycle.py`) |
| [Slack integration](../operations/slack-webhooks.md) | Full | 3 | Slash commands and events API endpoints |
| [Webhook ingestion](../integrations/automation-bridge.md) | Full | 3 | `POST /webhooks/` for external event routing |
| [Adaptive parallelism](../architecture/adaptive-parallelism.md) | Full | 3 | Auto-tunes concurrency from observed success rates (`core/orchestration/adaptive_parallelism.py`) |
| [Warm pool](../architecture/warm-pool.md) | Full | 3 | Pre-spawned agent pool to cut spawn latency (`core/agents/warm_pool.py`) |
| Pluggable sandbox backends | Full | 3 | Worktree, Docker, E2B, and Modal backends behind a `SandboxSession` protocol |
| microVM sandbox backend | Brief | 3 | Isolation tier with content-addressed snapshots for deterministic fork-and-race (`--sandbox microvm`) |
| Named sandbox pools | Full | 3 | Chain-projected pool manifests with capability and egress ceilings and signed worker enrolment (`core/sandbox/pool.py`) |
| Cache policy engine | Full | 3 | Content-addressed key recipes, drift expiry, and fleet dedup with signed duplicate-of receipts (`core/persistence/cache_policy.py`) |
| Sovereign deployment profile | Full | 3 | Signed residency-posture attestation; posture drift at spawn is a signed refusal (`core/security/deployment_profile.py`) |
| [Workflow DSL](../operations/workflow-manifests.md) | Full | 3 | `bernstein workflow validate/list/show` |
| [Chaos engineering](../operations/chaos-engineering.md) | Full | 3 | `bernstein chaos agent-kill/file-remove/status/slo` |
| Benchmark suite | Full | 4 | `bernstein eval run/compare/swe-bench/programbench` |
| [Eval harness](../eval/golden-harness.md) | Full | 4 | `bernstein eval run/report/failures` |
| SWE-Bench harness | Full | 4 | Verified eval in `benchmarks/swe_bench/run.py` |
| [Graduation system](../operations/graduation.md) | Full | 3 | Agent promotion stages, routes in `routes/graduation.py`. The documented REST surface is driven end to end against a real server by `tests/integration/test_first_run_graduation_surface.py`: policies, record-event, eligibility at the sandbox threshold, and promotion to `shadow`. |
| [Semantic caching](../concepts/semantic-caching.md) | Full | 3 | `semantic_cache.py` prompt deduplication |
| [Cascade router (intra-Claude tier escalation)](../architecture/model-routing.md) | Full | 3 | Tier escalation within a single provider (`core/routing/cascade_router.py`) |
| [Cascade fallback manager (cross-adapter failover)](../architecture/model-routing.md) | Full | 3 | Cross-adapter provider failover (`core/routing/cascade.py`) |
| [Batch router](../architecture/batch-routing.md) | Full | 3 | Task batching for non-urgent work |
| [Prompt caching](../operations/performance-tuning.md) | Full | 3 | SHA-256 system prefix deduplication |
| Output style customization | Brief | 3 | `.bernstein/output-styles/*.md`, selected by `output_style:` in `bernstein.yaml`, injected into the spawn prompt |
| [Installation mismatch detection](../operations/doctor.md) | Full | 4 | Detects adapter/installation gaps |
| [Worker badge identity](../operations/worker-process-identity.md) | Full | 3 | Process identification in `ps`/Activity Monitor |
| [Keybinding system (TUI)](../operations/tui-keybindings.md) | Full | 3 | Configurable TUI keyboard shortcuts. `tests/unit/test_first_run_keybinding_overrides.py` resolves the documented three layers and then presses the overridden key against a running Textual app, asserting the bound action fires and the pre-override key no longer does. |
| Diff folding display | Brief | 3 | `bernstein diff --fold`; the TUI agent log also folds long diffs in its historical tail |
| Word-level diff rendering | Brief | 3 | `bernstein diff --word-diff` highlights only the tokens that changed |
| Contextual tips system | Brief | 3 | One cooldown-limited hint after an interactive command; `BERNSTEIN_NO_TIPS` opts out |
| Security review command | Brief | 3 | `bernstein security-review` pattern-scans a diff for secrets, injection, and weak crypto |
| [Commit attribution stats](../operations/commit-attribution.md) | Full | 3 | Per-agent commit statistics, via `bernstein report commits` |
| Away summary generation | Brief | 3 | `bernstein recap --since 6h` builds the report from workspace files, no server needed |
| Plugin trust warning | Brief | 3 | Trust tier and score per plugin in `bernstein plugins` (`--trust-details` for the signal breakdown) |
| [Cumulative progress tracking](../observability/cumulative-progress.md) | Full | 3 | Progress tracking across runs |

## CLI commands

| Command | Docs status | Maturity | Notes |
|---|---|---|---|
| `bernstein -g GOAL` | Full | 3 | Inline goal |
| `bernstein run plan.yaml` | Full | 3 | Plan file execution |
| `bernstein init` | Full | 3 | Workspace setup. The documented first run is covered by `tests/integration/test_first_run_documented_path.py`, which runs the command from an empty directory and asserts the created artifacts plus the key output lines documented in `first-run.md`. |
| `bernstein stop` | Full | 3 | Graceful/force stop |
| `bernstein live` | Full | 3 | TUI dashboard. Readiness is the first rendered frame, identified by the `AGENTS` and `TASKS` pane headers; `tests/integration/test_first_run_long_running_surfaces.py` starts it from an empty workspace, waits for that frame, and asserts a traceback-free exit on `SIGINT`. |
| `bernstein dashboard` | Full | 3 | Web dashboard |
| `bernstein status` | Full | 3 | Task summary |
| `bernstein ps` | Full | 3 | Process list |
| `bernstein cost` | Full | 4 | Spend breakdown |
| `bernstein doctor` | Full | 4 | Pre-flight health check |
| `bernstein recap` | Full | 3 | Post-run summary |
| `bernstein retro` | Full | 3 | Retrospective report |
| `bernstein report commits/incident/postmortem` | Brief | 3 | Per-run markdown summaries: `commits` is per-agent commit attribution ([reference](../operations/commit-attribution.md)); `incident` correlates a timeline from logs, metrics, and traces; `postmortem` writes a structured report for a failed run. The group has no reference page of its own; `cli-reference.md` and `bernstein report --help` carry it |
| `bernstein trace ID` | Full | 3 | Decision trace |
| `bernstein logs` | Full | 3 | Agent log tail |
| `bernstein diff ID` | Full | 3 | Per-task git diff |
| `bernstein plan` | Full | 3 | Task backlog |
| `bernstein plan compile` | Full | 3 | Compile a spec into a gated task graph offline |
| [`bernstein replay ID`](../operations/replay.md) | Full | 3 | Deterministic replay |
| [`bernstein checkpoint`](../operations/checkpoint.md) | Full | 3 | Session snapshot |
| [`bernstein wrap-up`](../operations/wrap-up.md) | Full | 3 | End session with summary |
| `bernstein demo` | Full | 2 | **Preview.** Zero-config demo. A cold first run exits 1 with `Task server on port 8055 did not respond within 10.0s` and an empty `server.log`: a first-ever import of the package can exceed the server-readiness budget while Python byte-compiles it. The real budget is 30s (`_SERVER_READY_TIMEOUT_S`); the `10.0s` in the message is a stale hardcoded string — #3905. A second run on a warm cache starts the server in under a second. On a Windows console using a legacy code page the command also aborts with `UnicodeEncodeError` on the `✓` it prints first. |
| [`bernstein demo --flask-todo`](../getting-started/quickstart-demo.md) | Full | 2 | **Preview.** Flask TODO demo (3 tasks). `bernstein quickstart` is a deprecated alias, removed in v4.0.0. The run does not complete a first run: all three seeded tasks fail and the task server can die and restart mid-run. The reporting is no longer part of it — the summary counts against the seeded total and the command exits non-zero (#3902). |
| `bernstein agents ...` | Full | 3 | Catalog management |
| `bernstein evolve ...` | Full | 2 | **Preview.** A clean directory exits before the evolution loop starts because `.sdd/` is missing; initialise a Bernstein workspace first. |
| `bernstein ci fix` | Full | 3 | CI autofix |
| `bernstein github setup` | Full | 3 | GitHub App setup |
| [`bernstein worker`](../operations/cluster-mode.md) | Full | 3 | Join cluster as worker. Readiness is the `Registered as node <id>` line; `tests/integration/test_first_run_long_running_surfaces.py` registers one against a live cluster-enabled server and separately asserts the non-git-workspace refusal names its reason. |
| [`bernstein mcp`](../mcp/server.md) | Full | 3 | Run as MCP server |
| [`bernstein chaos`](../operations/chaos-engineering.md) | Full | 3 | Fault injection |
| [`bernstein audit`](../security/audit-log.md) | Full | 4 | Cryptographic audit chain. Seventeen subcommands: `seal`, `verify`, `verify-hmac`, `verify-gates`, `verify-multitenant`, `verify-suspension`, `show`, `query`, `diagnose`, `receipt`, `taint`, `capabilities`, `ack-tear`, `export`, `pack`, `slice`, `archive` |
| [`bernstein verify`](cli/verify.md) | Full | 4 | Merkle/HMAC verification |
| `bernstein benchmark` | Full | 4 | Deprecated alias for `bernstein eval`, removed in v4.0.0 |
| [`bernstein eval`](../eval/golden-harness.md) | Full | 4 | Evaluation harness |
| `bernstein workspace` | Full | 3 | Multi-repo workspace |
| [`bernstein config`](../operations/global-config.md) | Full | 3 | Configuration management |
| `bernstein quarantine` | Brief | 3 | Cross-run task quarantine read from `.sdd/runtime/quarantine.json` (`list`, `clear`) |
| [`bernstein cache`](../concepts/semantic-caching.md) | Full | 3 | Response cache management |
| [`bernstein test-adapter`](../adapters/test-adapter.md) | Full | 3 | Adapter smoke test |
| [`bernstein add-task`](cli/task-lifecycle.md) | Full | 3 | Inject task via CLI |
| [`bernstein cancel`](cli/task-lifecycle.md) | Full | 3 | Cancel task |
| [`bernstein review/approve/reject/pending`](cli/task-lifecycle.md) | Full | 3 | Review workflow |
| [`bernstein sync`](../operations/backlog-sync.md) | Full | 3 | Sync backlog with server |
| [`bernstein manifest`](../operations/run-manifest.md) | Full | 3 | Run manifest inspection |
| [`bernstein gateway`](../operations/mcp-gateway.md) | Full | 3 | MCP gateway proxy |
| [`bernstein workflow`](../operations/workflow-manifests.md) | Full | 3 | Workflow DSL |
| [`bernstein watch`](../operations/watch.md) | Full | 3 | Directory file watcher |
| [`bernstein listen`](../operations/voice-control.md) | Full | 2 | **Preview.** Voice commands are experimental. A base install exits with the `pip install 'bernstein[voice]'` hint; a usable first run also requires microphone/audio support and downloads the selected Whisper model on first use. |
| [`bernstein completions`](../operations/shell-completions.md) | Full | 3 | Shell completion scripts |
| [`bernstein self`](../operations/updates.md) | Full | 3 | Provenance-verified update lifecycle: signed feed, chain-anchored advisory, pre-install wheel verification, pin, receipted rollback |
| [`bernstein self-update`](../operations/self-update.md) | Full | 3 | Compatibility alias for `bernstein self` |
| [`bernstein plugins`](../integrations/plugin-sdk.md) | Full | 3 | List active plugins |
| [`bernstein install-hooks`](../contributing/git-hooks.md) | Full | 3 | Install git hooks |
| [`bernstein debug`](../operations/debug-bundle.md) | Full | 4 | Generate debug bundle for triage |
| `bernstein acp serve` | Full | 3 | ACP bridge (`--stdio` or `--http :PORT`) |
| [`bernstein autofix ...`](../operations/autofix.md) | Full | 3 | CI autofix daemon (start/stop/status/attach) |
| [`bernstein connect`](../operations/secrets.md) | Full | 3 | Credential vault setup for a provider |
| [`bernstein creds ...`](../operations/secrets.md) | Full | 3 | Credential management (list/revoke/test) |
| [`bernstein preview ...`](../operations/preview.md) | Full | 3 | Dev server preview (start/stop/list/status) |
| [`bernstein fleet`](../operations/fleet.md) | Full | 3 | Fleet dashboard (optionally `--web HOST:PORT`) |
| [`bernstein fleet steer`](../operations/fleet-steering.md) | Full | 3 | Mid-run steering: pause/resume/guidance/redirect/abort |
| [`bernstein mcp catalog ...`](mcp-catalog.md) | Full | 3 | MCP catalog browser (browse/search/install) |
| [`bernstein notify test`](../operations/notifications.md) | Full | 3 | Notification sink smoke test |
| [`bernstein plan ls/show`](../operations/plan-archival.md) | Full | 3 | List and inspect archived plans |
| [`bernstein review-responder ...`](../operations/review-responder.md) | Full | 3 | PR review responder (start/status/tick) |
| [`bernstein review --pipeline`](../operations/review-pipeline.md) | Full | 3 | Review with YAML pipeline DSL |
| [`bernstein fork --run --from-step`](../operations/fork-from-step.md) | Full | 3 | Fork a run at a journal step into a new isolated run |
| [`bernstein gate verify`](../operations/gate-adjudication.md) | Full | 3 | Recompute a gate panel's inputs hash and confirm the adjudication |
| [`bernstein mandate emit/verify/revoke`](../operations/spending-mandates.md) | Full | 3 | Bind, prove, and revoke authorized-action mandates |
| `bernstein payment-mandate issue/show/spend` | Full | 3 | Issue and spend against authorized-spend mandates with signed receipts |
| [`bernstein governance verify`](../operations/governance.md) | Full | 3 | Recompute access and budget verdicts for a run |
| [`bernstein webhook verify`](../operations/webhook-node.md) | Full | 3 | Recompute inbound-event and outbound webhook-node hashes |
| [`bernstein review-receipt emit/verify`](../operations/review-receipts.md) | Full | 3 | Bind and offline-verify PR review receipts (issue + plan + tool calls + diff) |
| [`bernstein receipt create/verify`](receipt.md) | Full | 3 | Sign a result receipt bundle from a JSON spec and verify one offline, naming the exact field that diverged |
| [`bernstein escalation show/verify`](../operations/stall-escalation.md) | Full | 3 | Project and reconstruct escalation receipts from the journal |
| [`bernstein supervisor status/escalate`](../api/supervisor.md) | Full | 3 | Supervise stalled workers and seal stall escalation receipts |
| [`bernstein delegation verify`](../operations/delegation-verify.md) | Full | 4 | Reconstruct and verify a run's delegation chain |
| [`bernstein credential emit/verify`](../operations/content-credentials.md) | Full | 3 | Project an artifact's lineage into a signed C2PA credential and verify it |
| [`bernstein skills provenance/verify`](../operations/skill-provenance.md) | Full | 3 | Recompute a skill's install receipt and usage-provenance graph |
| [`bernstein schedule verify/audit`, `schedule show --at`](../operations/schedule.md) | Full | 3 | Replay recorded fires, chain-check fire receipts, project a schedule at a time |
| `bernstein sla add/list/show/verify/report` | Full | 3 | Attach per-goal SLA contracts; a breach is an offline-verifiable violation receipt |
| [`bernstein trace project/verify-projection`](../observability/otel-span-projection.md) | Full | 3 | Project a run journal into signed OTel GenAI spans and verify the projection |
| `bernstein telemetry export-otel/verify-span` | Full | 3 | Stream the span projection to an OTLP collector and verify a single span offline |
| [`bernstein thread verify`](../operations/deterministic-replay.md) | Full | 3 | Prove a streamed TUI thread equals its executed journal |
| `bernstein memory verify/why/forget` | Full | 4 | Prove authorship, trace origin, and tombstone a memory entry |
| [`bernstein replay --verify/--from-step`](../operations/deterministic-replay.md) | Full | 3 | Recompute the journal head or rebuild state to a step |
| [`bernstein lineage verify/walk/chain/replay/export`](../lineage.md) | Full | 4 | Per-artifact lineage spine: recompute a run's Merkle chain and HMAC tags, walk an artifact back to its producer, replay the spine in append order, and export a run's chain as a regulator artefact (`tracker-audit`, `forks`, `conflicts`, `resolve`, `merge`, `reindex`, `gate`, and `v2` complete the group) |
| `bernstein intent show/verify` | Full | 3 | Project and recompute an intent capsule's conformance offline |
| `bernstein a2a verify/publish` | Full | 3 | Verify an A2A message receipt and publish the agent card |
| `bernstein activity verify` | Full | 3 | Verify a typed activity's replay hashes (`activity browser run` for browser checks) |
| `bernstein datasource register/query/verify` | Full | 3 | Register read-only SQL datasources and verify content-addressed query receipts |
| [`bernstein evidence show/verify`](../operations/evidence-bundles.md) | Full | 3 | Project and verify a sealed evidence bundle |
| [`bernstein events query/verify`](../events/grammar.md) | Full | 3 | Query the unified event feed and verify its chain projection |
| `bernstein endpoints certify/verify` | Full | 3 | Conformance-certify a local-model endpoint and verify its certification |
| `bernstein ledger verify/anchor/fetch` | Full | 3 | Verify, anchor, and fetch work-ledger segments |
| `bernstein mission define/status/verify` | Full | 3 | Define multi-phase missions and verify mission status (`mission digest verify` for digests) |
| [`bernstein tournament show/verify`](../operations/tournament-runs.md) | Full | 3 | Inspect a tournament run and verify its selection receipt |
| `bernstein spiffe id/verify-binding` | Full | 4 | Print the SPIFFE id and verify a workload-identity binding |
| `bernstein spec check/auto-fix` | Full | 3 | Evaluate and auto-fix a spec against the quality checklist |
| [`bernstein run-service submit/attach/status`](../operations/run-service.md) | Full | 3 | Submit a detached run, then reattach to it later |
| `bernstein compaction log` | Full | 3 | Inspect chain-anchored compaction receipts |
| `bernstein identity keydir/decode/verify` | Full | 4 | Print the JWKS key directory and decode/verify install identity |
| [`bernstein pool register/list/show/verify`](../operations/sandbox-pools.md) | Full | 3 | Manage lease-backed named resource pools |
| [`bernstein volunteer verify`](volunteer-manifest.md) | Full | 3 | Validate a project's `.bernstein/volunteer.json` and print the manifest digest a receipt binds to |

## Cloud / Cloudflare
> **How a row graduates:** A row graduates out of Preview when its maturity score increases to ≥ 3 (or when a first-run smoke test lands and the marker is intentionally removed).

| Capability | Docs status | Maturity | Notes |
|---|---|---|---|
| Workers RuntimeBridge | Full | 2 | **Preview.** Hosted API (`api.bernstein.run`) is unreachable; Workers run against your own Cloudflare account. `bridges/cloudflare.py` agents on Workers + Durable Objects |
| Workflow Bridge (durable execution) | Full | 2 | **Preview.** Hosted API (`api.bernstein.run`) is unreachable; Workflows run against your own Cloudflare account. `bridges/cloudflare_workflow.py` auto-retry, approval gates |
| Browser Rendering Bridge | Full | 2 | **Preview.** Hosted API (`api.bernstein.run`) is unreachable. `bridges/browser_rendering.py` screenshots, scraping, PDFs |
| R2 Workspace Sync | Full | 2 | **Preview.** Hosted API (`api.bernstein.run`) is unreachable; R2 sync runs against your own Cloudflare account. `bridges/r2_sync.py` content-addressed delta sync |
| Workers AI Provider (free LLMs) | Full | 2 | **Preview.** Hosted API (`api.bernstein.run`) is unreachable. `core/routing/cloudflare_ai.py` Llama, Mistral, Gemma, Qwen |
| D1 Analytics & Billing | Full | 2 | **Preview.** Hosted API (`api.bernstein.run`) is unreachable; D1 runs against your own Cloudflare account. `core/cost/d1_analytics.py` usage metering, billing tiers |
| MCP Remote Transport | Full | 2 | **Preview.** Hosted API (`api.bernstein.run`) is unreachable. `mcp/remote_transport.py` streamable HTTP for remote MCP |
| Cloud CLI (`bernstein cloud`) | Full | 2 | **Preview.** `bernstein cloud login/run/status/runs/cost` report the service unreachable and exit non-zero. `cli/commands/cloud_cmd.py` login, run, status, cost, deploy |
| Codex-on-Cloudflare Adapter | Brief | 1 | **Preview.** Targets a REST API that does not yet exist and refuses fast. `adapters/codex_cloudflare.py` experimental |
