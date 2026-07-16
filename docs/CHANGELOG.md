# Changelog

All notable project changes are tracked here (code + docs).

## [Unreleased]

## [3.5.0] - 2026-07-16

Hardens audit identity across MCP protocol change and replay detection of provider-side context rewrites, plus a security dependency bump and CI reliability work. Full notes: [`docs/release-notes/v3.5.0.md`](release-notes/v3.5.0.md).

## [3.4.4] - 2026-07-12

Adds the experimental MCP Tasks protocol surface to the Bernstein MCP server, plus release-pipeline hardening. Full notes: [`docs/release-notes/v3.4.4.md`](release-notes/v3.4.4.md).

## [3.4.3] - 2026-07-12

Fixes the MCP registry listing publish by carrying the required OCI image label on the published container. Full notes: [`docs/release-notes/v3.4.3.md`](release-notes/v3.4.3.md).

## [3.4.2] - 2026-07-12

Wires cost-aware batch and cache policies into the live run loop, runs adapter conformance on a real Windows CI runner, and makes the distribution image verifiable. Full notes: [`docs/release-notes/v3.4.2.md`](release-notes/v3.4.2.md).

## [3.4.1] - 2026-07-12

Completes v3.4.0 follow-up: Windows parity coverage and a security-hardening and static-analysis cleanup pass. Full notes: [`docs/release-notes/v3.4.1.md`](release-notes/v3.4.1.md).

## [3.4.0] - 2026-07-12

Ships typed activity records, detached runs, tournament selection, cost-aware dispatch, in-process gates, a spec-to-graph pipeline, workload identity, and an MCP Tasks surface as signed, replayable records. Full notes: [`docs/release-notes/v3.4.0.md`](release-notes/v3.4.0.md).

## [3.3.0] - 2026-07-11

Operator-capability release: authenticated dashboard with scoped tokens and governance receipts, the agy adapter with a nightly conformance canary, and the run review board over sealed evidence bundles. Full notes: [`docs/release-notes/v3.3.0.md`](release-notes/v3.3.0.md).

## [3.2.0] - 2026-07-10

Resumability and availability: interrupted runs continue from a verified ledger, provider outages reroute along declared fallback chains with a receipt per decision, and certified local endpoints join the worker pool. Full notes: [`docs/release-notes/v3.2.0.md`](release-notes/v3.2.0.md).

## [3.1.0] - 2026-07-07

Task completion produces a sealed, content-addressed verification evidence bundle that verifies against the same audit chain. Full notes: [`docs/release-notes/v3.1.0.md`](release-notes/v3.1.0.md).

## [3.0.0] - 2026-07-06

Verifiability release: every major surface ships its primary artifact as a proof (signed lineage receipt, HMAC-chained journal entry, content-addressed record, or deterministic projection). Full notes: [`docs/release-notes/v3.0.0.md`](release-notes/v3.0.0.md).

## [2.16.1] - 2026-07-06

Windows: adapter binaries installed as `.cmd`/`.bat` shims now spawn correctly. Full notes: [`docs/release-notes/v2.16.1.md`](release-notes/v2.16.1.md).

## [2.16.0] - 2026-07-05

Output-economy and worker-reliability release: response-style profiles, ledger-attributed savings, a three-arm cost-and-quality A/B harness, proactive context compaction with signed receipts, and schema-enforced worker outcomes. Full notes: [`docs/release-notes/v2.16.0.md`](release-notes/v2.16.0.md).

## [2.15.0] - 2026-07-05

Hold/release lease API, explicit per-task `max_turns`, per-agent run instrumentation, and a council-of-agents redesign, built around five community contributions. Full notes: [`docs/release-notes/v2.15.0.md`](release-notes/v2.15.0.md).

## [2.14.1] - 2026-07-04

Security and hygiene patch: log-injection sanitisation across the task server and no sensitive-data logging in the openai_agents adapter. Full notes: [`docs/release-notes/v2.14.1.md`](release-notes/v2.14.1.md).

## [2.14.0] - 2026-07-04

Multi-provider orchestration hardening: a batch of correctness and run-safety fixes for real-codebase runs across mixed providers. Full notes: [`docs/release-notes/v2.14.0.md`](release-notes/v2.14.0.md).

## [2.13.0] - 2026-07-02

Run-safety guardrails (opt-in GitHub backlog sync, protected default branch) and per-role endpoint configuration. Full notes: [`docs/release-notes/v2.13.0.md`](release-notes/v2.13.0.md).

## [2.12.0] - 2026-07-02

Worker isolation, replay integrity (compaction-aware step fingerprint, effort level as a replay dimension), and OWASP security-taxonomy audit mapping. Full notes: [`docs/release-notes/v2.12.0.md`](release-notes/v2.12.0.md).

## [2.11.0] - 2026-07-02

Optional sampling and endpoint parameters on the openai_agents runner, fail-closed API-key env allowlisting, and a hardened Docker sandbox path. Full notes: [`docs/release-notes/v2.11.0.md`](release-notes/v2.11.0.md).

## [2.10.0] - 2026-07-02

Real end-to-end Docker sandbox provisioning, `bernstein run --worker <role>`, and fixes for role-template rendering and `--model` propagation to child tasks. Full notes: [`docs/release-notes/v2.10.0.md`](release-notes/v2.10.0.md).

## [2.9.0] - 2026-06-30

GitHub Copilot CLI adapter runs in non-interactive print mode, plus a security bump and a refurb cleanup. Full notes: [`docs/release-notes/v2.9.0.md`](release-notes/v2.9.0.md).

## [2.8.5] - 2026-06-29

Maintenance release: dependency and CI-action updates and a fix for the scheduled review-bot sweep. Full notes: [`docs/release-notes/v2.8.5.md`](release-notes/v2.8.5.md).

## [2.8.2] - 2026-06-27

Reliability and maintenance: Codex ChatGPT OAuth login works again, worktree GC no longer loses unmerged work, and `worktrees unlock` recovers a stuck GC lock. Full notes: [`docs/release-notes/v2.8.2.md`](release-notes/v2.8.2.md).

## [2.7.0] - 2026-05-24

Maintenance release: deterministic skill-routing tools, Sonar hotspot cleanup, and CI and publish-pipeline reliability work. No dedicated release-notes page; see the `v2.7.0` git tag.

## [2.6.0] - 2026-05-22

Bidirectional Slack and Discord chat drivers with verifiable approvals, per-step replay with a hash-chained journal, operator-registered recurring goals, a signed supervisor surface, a signed skill catalog, and image-attachment provenance. Full notes: [`docs/release-notes/v2.6.0.md`](release-notes/v2.6.0.md).

## [2.5.1] - 2026-05-20

Restores the auto patch-publish pipeline after a broken workflow_run dispatcher silently swallowed releases, and ships the accumulated security, observability, routes, and API fixes. Full notes: [`docs/release-notes/v2.5.1.md`](release-notes/v2.5.1.md).

## [2.5.0] - Interoperability surfaces, host portability, deterministic replay

22 commits since v2.4.0. Full notes: [`docs/release-notes/v2.5.0.md`](release-notes/v2.5.0.md).

### Added

- A2A capability cards as a first-class interop primitive: `bernstein interop a2a card` / `verify`, signature plus expiry plus trusted-issuer verification on consume, and the signed lineage chain carried through the A2A envelope with a cross-organisation boundary marker (#1698).
- Hardened MCP client: capability-card validation before each tool call, retry-with-continuation on dropped streams, streamed-output cancellation, per-server cost metering, and schema-violation containment that degrades a misbehaving server instead of failing the run (#1692).
- MCP server protocol-surface gaps closed to match the hardened client (#1696).
- Tiered MCP tool exposure behind a context-budget knob (#1685).
- `bernstein desktop-register --host <name>` installs Bernstein into Claude Desktop and Claude Code via a per-host adapter (#1697).
- Portable side-channel telemetry behind one Sentry-compatible `BERNSTEIN_TELEMETRY_DSN`, plus `bernstein telemetry probe` for backend verification (#1691).
- Deterministic session-id binding for replay isolation (#1684).
- Supervisor respawn budget with park-on-exhaustion (#1683).
- Versioned migrations module for on-disk state (#1689).
- Memorable deterministic run names in user-facing surfaces (#1682, #1626).
- Per-adapter strategy enums for resume, dangerous-mode, and event channel (#1690).
- Permission-rule prefilter on lifecycle hooks before spawn (#1680).
- Strict structured-output schemas with a user-field blacklist (#1681).
- Consensus scoring with detected-by provenance on review findings (#1686).
- Tiered, cost-tuned memory compaction (#1687).

### Changed

- Runtime `python:3.13-slim` Docker digest bumped to `e544a7f`, staying on the pinned 3.13 line (#1699).

### Fixed

- `TaskCreate` / `TaskSelfCreate` validate `scope` and `complexity` at the request boundary and return `422` for empty or out-of-range values, instead of raising `ValueError` in the task store and surfacing an unhandled `500` on `POST /tasks` and `POST /tasks/batch` (#1700).
- Shipped package no longer hardcodes operator-private infrastructure hosts as defaults; observability and telemetry backends soft-fail or no-op when unset, with a regression test asserting zero operator-private host, IP, or DSN matches in `src/` (#1694).
- Dependency audit ignores the disputed, fix-less pyjwt advisory PYSEC-2025-183 (CVE-2025-45768), pulled in transitively via `mcp`, with the rationale recorded inline (#1695).
- Agent-context files (`AGENTS.md`, `CLAUDE.md`, `CONVENTIONS.md`, `.goosehints`, cursor module map) re-synced for the `interop` and `substrate` modules; duplicate spell-check allow-list key removed; MCP client test fixture no longer relies on a spell-check allow-list entry (#1701, #1702, #1693).

## [2.4.0] - Observability surfaces, single-writer run state, declarative planning gates

33 commits since v2.3.1. Full notes: [`docs/release-notes/v2.4.0.md`](release-notes/v2.4.0.md).

### Added

- Unified `bernstein doctor observe` umbrella that runs the Sonar, GlitchTip, Dependency-Track, and GitHub Code Scanning probes in order and renders one aggregated table with delta-since-last-check; supports `--json` and `--watch`, each backend soft-fails to `SKIPPED` when unset, deltas cache under `.sdd/observability/<backend>.json`. Adds a per-PR sticky summary workflow and a daily trends snapshot workflow that re-renders `docs/observability/trends.md` (#1650).
- Spec-quality gate (`bernstein spec check` / `bernstein spec auto-fix`): a deterministic, library-only rule set (acceptance-criteria, out-of-scope, tested-via, no-TODO, no-placeholder, ref-paths-exist) that refuses to advance a failing spec, routes through a bounded auto-fix loop, and raises `SpecQualityUnresolvedError` when the budget is exhausted; rules pluggable via the `bernstein.spec_quality_rules` entry-point group (#1652).
- Three-layer skill customization (BASE / TEAM / USER) under XDG paths with a per-field deterministic merge spec; `bernstein skills list --layered` and `bernstein skills show <name> --per-layer` surface layer-of-origin and the merged/raw diff (#1654).
- `bernstein doctor sonar` subcommand surfacing coverage, code smells by severity, bugs, vulnerabilities, security hotspots, and cognitive-complexity hotspots from a configured SonarQube server; advisory baseline cache and parent-doctor nudge when open smells exceed the threshold or vulnerabilities regress (#1648).
- `bernstein doctor glitchtip` subcommand surfacing last-24h issue counts by severity, a 7-day trend, and top unresolved issues; optional baseline cache and parent-doctor nudge when new unresolved issues appear (#1646).
- Sticky PR Sonar comment workflow and daily GlitchTip alert sweep workflow (06:30 UTC) that mirrors fatal-level issues into sticky GitHub issues labelled `glitchtip-alert` and auto-closes them when the GlitchTip side resolves (#1646, #1648).
- Canonical stream-signal protocol (`COMPLETED`, `FAILED`, `QUESTION`, `PLAN_DRAFT`, `PLAN_READY`, `BLOCKED`) parseable from any wrapped CLI stdout; optional `stream_signal_parser` hook on `CLIAdapter`; `ConformanceReport` soft-warns on missing terminal signals (#1638).
- Single-writer `RunActor` with one async event queue, monotonic seq numbers, and a bounded `ReplayBuffer` that emits an explicit `Gap{up_to_seq}` marker on eviction; approval gate gains an opt-in `session_id` kwarg that mirrors approval events through `run_actor_registry` (#1641).
- Empirical-confidence ledger: append-only SQLite store of per-decision outcomes plus a sample-size-gated `ConfidenceQuery` (default 5) wired into the model recommender ahead of the capability-tier heuristic and the bandit arm (#1653).
- Declarative task DAG: `Task.parallel_safe` and `Task.story_id` fields, `[T<id>] [P] [USn]` backlog parser, `core/orchestration/task_dag.py` with `topological_iter_with_parallel` yielding ready batches, and `bernstein plan dag` / `bernstein tasks dag` CLI renderers (#1655).

### Changed

- HTTP approval replies now require a single-use 16-byte server-minted `nonce`; mismatches surface `409 NONCE_MISMATCH` and replays against an evicted approval surface `410 NONCE_EXPIRED` (#1642).
- Sonar scan workflow switched from a direct trigger to `workflow_run` on the CI workflow; the scan now consumes the existing `coverage-report` artifact instead of re-running the full test suite under a single non-sharded `pytest --cov` (#1645).
- `bernstein approve-tool` / `bernstein reject-tool` read the on-disk pending-approval record and thread the nonce back through `resolve()` (#1642).

### Fixed

- Re-add `str()` coercion inside the `OSError` / `TimeoutExpired` handler of `git_context._run_git` so callers passing a `Path` in the `argv` list (`test_context`, `test_context_builder`, `test_failure_reduction` via `cochange_files`) do not crash the debug formatter with `expected str instance, PosixPath found` (#1644).
- Apply `ruff format` to `core/quality/review_pipeline/review_gate.py` after #1638 collapsed several string and comprehension wrappings under the 120-character line length, fixing `ruff format --check` on main (#1640).
- Default empty `nonce` body field to an empty string at the schema layer so a missing field flows through the handler and surfaces as `409 NONCE_MISMATCH` instead of `422` (#1642).
- Move `Iterator` and `Path` imports under `TYPE_CHECKING` in `core/orchestration/task_dag.py`, replace `== True` with `is True` in `tests/unit/tasks/test_parallel_flag.py`, and run `ruff format` across the four files added or touched by #1655, fixing the Lint job that turned main red after the task-DAG merge (#1657).
- Widen the Schemathesis smoke step timeout to stop the property-based API smoke run being cancelled mid-flight under the normal main merge cadence (#1659).
- Pin the published runtime image and the demo image back to `python:3.13-slim` by digest (both had drifted to `python:3.14-slim` while their comments read 3.12), matching the repository python policy and adapter dependency constraints (#1664).
- Repair the `sonar-scan` `workflow_run` trigger: make `workflow_dispatch` resolve the most recent successful CI run on main and pull its `coverage-report` artifact so a manual bootstrap scan carries full Python coverage instead of scanning coverage-less (#1665).
- Stop the review-bot-ack gate from cancelling its own required status check: scope the concurrency group per-PR and per-head-sha with `cancel-in-progress: false` so each commit's gate run completes against its own sha and a `CANCELLED` conclusion no longer stalls the merge queue (#1666).

### Documentation

- Doc-drift refresh reconciling 16 `docs/concepts/` and `docs/gui/` documents with current source-of-truth public surfaces (renamed CLI subcommands, signatures, and config knobs); `docs/sdd/` verified in sync (#1677).

### Internal

- Refurb auto-fix wave 4: FURB184 197 -> 34, FURB138 42 -> 8, FURB124 29 -> 3, FURB142 16 -> 0, FURB113 23 -> 21 in `src/`; plus a `ruff format` pass over 36 files to wrap `E501` long-line comprehensions and four targeted fixes for broken `seen in seen` self-referential dedup comprehensions (#1643).
- Refurb cluster D: FURB139 / FURB143 / FURB179 strings and enumerate, 16 autofixes (#1647).
- Refurb cluster E: FURB182 / FURB183 / FURB142 / FURB101 miscellaneous, 33 autofixes across 21 files; refurb now reports 0 alerts for these rules in `src/` (#1649).
- Refurb cluster B: FURB109 / FURB108 / FURB126 control flow, 53 autofixes across 44 files; pure control-flow and literal rewrites with no behavioural change (#1651).
- Review-bot acknowledgement gate caught seven CodeRabbit must-address findings on #1646: HTTP status validation, `gh issue` subprocess `check=True`, doc clarification on soft-fail conditions, narrower import-time exception handling, logging of unexpected fetch failures, `IntRange(min=1)` on `--top-n`, and dropping a truthy fallback in `summarise_severity` / `_bucket_trend_by_day` that was inflating zero counts to one.
- Adds a CI workflow-health sweep summary at `docs/ci/workflow-health-2026-05-20.md` covering all 47 registered workflows (#1666).

### Dependencies

- Update dependency python to 3.13 and bump the `python:3.13-slim` and `gcr.io/oss-fuzz-base/base-builder-python` docker digests (#1663, #1678, #1670).
- Bump `actions/setup-python` 5 -> 6, `peter-evans/create-pull-request` to 7.0.11 / 8.1.1, and `marocchino/sticky-pull-request-comment` to v2.9.4 / v3 (#1668, #1662, #1667, #1661, #1671, #1669).

## [2.3.1] - Maintenance

4 commits since v2.3.0. Full notes: [`docs/release-notes/v2.3.1.md`](release-notes/v2.3.1.md).

### Fixed

- Restore numeric and key coercions removed by the refurb FURB123 pass, and reapply 19 deferred review-bot findings from the 2026-05-19 catch-up (#1615, #1618).
- Soft-fail the cross-repo landing-mirror dispatch on PAT scope errors so the docs-drift pipeline no longer blocks on a 403 (#1617).
- Wrap `sentry_sdk.init` in a best-effort try/except so a malformed `GLITCHTIP_DSN` cannot crash the CLI on import (#1618).
- Treat schema-invalid snapshot sidecars as unreadable metadata (return None and warn) instead of raising through `SnapshotStore.get` / `list` (#1618).
- Map `UrlSchemeError` to `TransportError` in `SseTransport.connect` and `StreamableHttpTransport.connect`; map `UrlSchemeError` to `NullAlertSink` fallback in lineage-alert `sink_from_config` (#1618).
- Reject negative `--days` / `older_than_days` in `bernstein git gc` before constructing `SnapshotStore` and before computing the cutoff (#1618).
- Catch OSError around GitHub App private-key reads and surface `TrackerUnavailable`; skip GraphQL items whose `content.__typename` is not Issue/PullRequest/DraftIssue rather than emitting empty tickets (#1618).
- Validate sign inputs as a pair and read the private key before assembling the bundle in `bernstein bundle` so invalid CLI input never mutates on-disk state (#1618).

### Internal

- Bulk refurb auto-fix wave 3: FURB123 (147 sites), FURB138 (57 sites), FURB113 (5 leftovers). One FURB123 site reverted (bytes-coercion inside an `isinstance(bytearray)` branch). FURB123 down to 0, FURB138 down to 49, FURB113 down to 26 (#1615).
- Widen the sonar-scan job timeout to 60 minutes with per-step caps (sync 15m, coverage 30m, scan 10m); pin `astral-sh/setup-uv@v8.1.0` with caching (#1616).
- Generate the SBOM from an isolated venv that contains only the project and its resolved dependencies, so the output reflects bernstein's dependency graph rather than the runner base image (#1618).
- Add `docs/operations/glitchtip-setup.md` covering DSN provisioning, env-var export, and end-to-end event verification (#1616).
- Record 14 review-bot findings already resolved on source PR branches and 11 deferred for design judgement in `docs/review-bot/deferred-2026-05-19.md` (#1618).

## [2.3.0] - Tracker-adapter family

127 commits since v2.2.0. Full notes: [`docs/release-notes/v2.3.0.md`](release-notes/v2.3.0.md).

### Highlights

- 10 tracker adapters land under a single `TrackerContract` (Asana, ClickUp, GitHub Projects v2, GitLab Issues, Jira Cloud, Jira DC, Linear, Plane, ServiceNow, plus webhook ingestion).
- Tracker plugin hookspec + registry + CLI for third-party tracker integrations (#1599).
- Issue -> plan-comment -> PR orchestration pipeline (#1600); tracker comments as multi-agent handoff bus (#1606).
- Review-bot acknowledgement gate: CodeRabbit / Sourcery must-address findings block merge until addressed or acknowledged (#1583).
- Signed lineage v2 audit log of tracker state moves (#1602).
- Playwright-based self-testing sandbox for UI/web agent runs (#1603).
- Secrets broker for short-lived per-task tokens (#1605).
- Bulk refurb auto-fix waves 1 + 2 across `src/` (#1558, #1582).

### CI

- **Bootstrap composite action for `astral-sh/setup-uv` (post-checkout).** Added `.github/actions/bootstrap/action.yml` wrapping `astral-sh/setup-uv` behind one pinned-SHA call. Inputs cover `python-version`, `enable-uv-cache`, `cache-key-suffix`, and a `setup-uv` toggle. The composite must be invoked AFTER `step-security/harden-runner` + `actions/checkout`, because a local composite action cannot resolve until the repository is checked out onto the runner. Each calling job inlines the harden-runner and checkout steps as before, then calls the composite for Python/uv setup. Net effect: pinned-SHA bumps for the uv setup now happen in one file instead of every job that runs uv.
- **Install-path smoke matrix against the built wheel.** Added `install-smoke-pipx` (matrix: ubuntu-latest x macos-latest x Python 3.12 / 3.13, matching `requires-python = ">=3.12"`) and `install-smoke-uv` (leaner: ubuntu-latest + macos-latest, Python 3.12) jobs to `.github/workflows/ci.yml`. Both jobs install from the wheel produced by the `dist-size` job (never editable), then run `bernstein --version`, `bernstein --help`, and an `importlib.resources` probe against the pipx- or uv-managed interpreter to confirm `console_scripts`, entry-point loading, and `package-data` (MCP tool schemas, force-included default templates) survive the build. Wheel size is gated at 25 MB inside the smoke jobs (independent of the tighter 10 MB day-to-day ceiling enforced by `dist-size`). Both jobs are wired into the `CI gate` required-check rollup so a regression on the pipx or `uv tool install` path now blocks merge instead of surfacing through user reports. Closes the regression-coverage gap on the install path documented first in README.

### Documentation

- **Per-step CLI and model routing surfaced.** Added [`docs/workflows/per-step-routing.md`](workflows/per-step-routing.md) documenting the existing per-step `cli:` / `model:` / `effort:` plan fields, the surfaces that honour them, the surfaces that drop them, and a trace-based verification recipe. `templates/bernstein.yaml` now ships a commented-out per-stage override example that points at the new page. `templates/workflows/idea-to-pr.yaml` and `templates/workflows/refactor-with-tests.yaml` carry inline comments showing where operators most often want to pin different adapters or models and the plan-YAML lift to do it. The runtime support already existed (`plan_loader._parse_step` at `plan_loader.py:255-294`, `planner.py:86-96`); this PR closes the discoverability gap raised in discussion #962.

## [2.2.0] - 2026-05-18

### Security

- **Strip invisible Unicode Tag codepoints from injected skills (spec 2026-05-17).** Public research (Feb 2026, Embrace the Red; Snyk skill-pack audit of 3,984 public files showing 36.82% with security flaws) demonstrated that invisible glyphs in the U+E0000-U+E007F Tag block are interpreted as instructions by Claude, Gemini, and Grok. Bernstein now strips every Cf-category, Tag-block, and interlinear-annotation codepoint from skill bodies before they are written into `.claude/skills/*.md` in agent worktrees. The new `bernstein.core.skills.sanitizer.strip_invisible_tags` function returns the cleaned body plus the count of stripped codepoints; the `SkillLoader` and `skills_injector` both invoke it at index time. A WARN log line plus a Prometheus counter `bernstein_skills_unicode_tags_stripped_total{source_name}` fire on every hit so operators can pinpoint a poisoned upstream source. Default ON; opt out with the hidden `--unsafe-allow-unicode-tags` CLI flag (or `BERNSTEIN_UNSAFE_ALLOW_UNICODE_TAGS=1`) only when reproducing an incident in a controlled environment.

### Added - routing

- **Per-task criterion profile (#1346).** Operators can now stamp a four-axis weight vector (`correctness`, `cost`, `latency`, `reversibility`) onto individual tasks to bias model selection.  Named presets (`safety-first`, `speed-first`, `balanced`, `cost-first`) ship in `templates/criterion_profiles/` and force-include into the wheel.  Inline dicts work too: `metadata['criterion_profile'] = {"correctness": 0.6, ...}`.  Surfaced via `bernstein add-task --criterion-profile <preset>`, `bernstein run --criterion-profile <preset>`, and `bernstein criterion-profile show <task_id> | list`.  Feature flag `BERNSTEIN_CRITERION_PROFILE=0` reverts to pre-existing routing.  Child tasks inherit the parent's profile unless explicitly overridden.

### Changed - chat bridge

- **Telegram driver simplified to a single long-poll path.** The `python-telegram-bot` v22 long-poll driver at `bernstein.core.chat.drivers.telegram` is the only Telegram driver. Configure a bot API token from `@BotFather` and a chat id; no external services. The earlier optional bridge-router architecture has been removed.
- **Telegram notification sink simplified.** `TelegramSink` accepts a live `TelegramBridge` via `config["bridge"]` or a token string via `config["token"]` and routes through the standard long-poll path.

## [2.0.0] - Web UI

Bernstein now ships a web interface. The major bump is signalling the new operator surface, not a breaking API change. v1.10.x configs, plans, adapters, audit chain, lineage, and CLI / TUI surfaces are unchanged.

Hand-curated release notes: [`docs/release-notes/v2.0.0.md`](release-notes/v2.0.0.md). Tracking issue: [#1262](https://github.com/sipyourdrink-ltd/bernstein/issues/1262).

### Added - Web UI

- **`bernstein gui serve`** boots a FastAPI server with the SPA mounted at `/ui` and the full `/api/v1/*` surface attached. Default `http://127.0.0.1:8052/ui/`. SPA bundle ships in the wheel (no Node toolchain required at install time).
- **Top-level tabs**: Tasks, Agents, Approvals, Audit, Costs, Fleet (scaffold), Settings (placeholder).
- **Per-task drawer** with tabs:
  - **Summary** - KPIs (tokens / cost / branch / approvals), plan steps from `progress_log`, drag-resize, focus trap, ESC + click-outside close (#1254).
  - **Logs** - SSE stream, ANSI rendering, virtualised list, search, level filters, throughput stats, keyboard shortcuts.
  - **Diff** - `GET /tasks/{id}/diff`; split / unified view, syntax highlight, copy + `.patch` download (#1255).
  - **Gates** - `GET /tasks/{id}/gates`; status buckets, auto-expand failures, polling that pauses on terminal tasks (#1258).
  - **Deps** - `GET /tasks/{id}/graph-neighbors`; upstream / downstream graph, polling (#1260).
  - **Trace** - `GET /tasks/{id}/trace` reading `.sdd/traces/{task_id}.jsonl`; filter chips, search, live polling while open (#1256).

### Fixed

- **Per-step `cli:` and `model:` in plan-driven runs** - three dispatch-pipeline bugs (POST payload dropping `model` / `effort`, role config.yaml clobbering per-task pin, merge gate ignoring `cli` mismatch) that silently collapsed plan steps onto the role default. Regression tests at `tests/unit/test_per_step_routing.py` (#1259).
- **Startup banner** - `bernstein run` / `bernstein conduct` regained the banner; an earlier commit removed it under a false "already printed" comment. Pinned by `tests/unit/cli/test_run_banner.py` (#1257).
- **`/openapi.json` 500** - FastAPI's OpenAPI builder tripped on `from __future__ import annotations` turning the GUI's response annotations into strings; `response_class` now declared explicitly on `/gui-meta` + `/ui` (#1253).
- **dev-proxy double-prefix** - `apiGet` is now idempotent; the Logs panel's terminal-task fallback no longer 404s on `/api/v1/api/v1/...` (#1253).

### Limitations (intentional)

- A11y audit, dark / light theme toggle UI, mobile-responsive pass, Settings screen wiring, Fleet UI, front-end test suite, Playwright e2e - all open. See [#1262](https://github.com/sipyourdrink-ltd/bernstein/issues/1262) for contributor-welcome pointers.

> **Older 1.x releases.** The 1.8.x and 1.9.x feature wave and the v1.10.2-v1.10.8 patch line are summarised in [What's New](whats-new.md); full per-tag detail lives in the git tags (`git tag --list 'v1.*'`).

## [1.10.1] - 2026-05-07

### Added - adapters

- **Devin for Terminal (Cognition).** First-class adapter with 558 lines of contract tests covering process tracking, env isolation, and timeout watchdogs. Drop-in for any plan via `cli_agent: devin_terminal`.
- **JetBrains Junie CLI.** LLM-agnostic BYOK adapter (`cli_agent: junie`) - forwards whichever provider key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.) the routed model needs and dynamically narrows the network allowlist to that provider's endpoints.
- **AWS Q Developer CLI.** First-class adapter (`cli_agent: q_dev`) using `q chat --no-interactive --trust-all-tools`. Token bootstrap via `q login` is documented in the adapter docstring; missing token cache surfaces a clear error rather than a silent hang. IAM Identity Center role inheritance noted as a deployment risk.
- **Cursor adapter rewrite.** Replaced shell to non-existent `cursor agent` binary with the real `cursor-agent` CLI surface (`-p --workspace --output-format stream-json --trust --approve-mcps --force`); 242 lines of new contract tests.

### Added - operator surfaces

- **Live terminal peek for the web dashboard (#1217).** New `GET /sessions/{id}/peek` JSON tail endpoint, plus a vanilla-JS surface at `/dashboard/peek/{id}` (single session) and `/dashboard/peek?s1=...&s2=...&s3=...&s4=...` (2x2 tile grid sized for a 390x844 phone viewport). Each tile carries a regex search box and a send-bar wired to `POST /sessions/{id}/send`, which pipes one line of operator input back into the agent's stdin via the existing `agent_ipc` registry. The bearer-auth middleware in `server_middleware.py` covers both routes unchanged.
- **Run savings summary.** Each `bernstein run` summary card now reports estimated savings vs running the same plan single-shot through the most expensive routed model.

### Fixed

- **Handoff tokens prefixed with `h_`.** `secrets.token_urlsafe()` produces a `-`-leading token in roughly 1.5% of issuances; click misparses `bernstein handoff claim TOKEN` as if `-V` were an option. Fix issues all tokens with the `h_` prefix.

### Documentation

- **Enterprise evaluation guide** - deployment shapes Bernstein already supports (laptop tool, on-prem cluster, air-gap-clean wheelhouse, MCP server mode behind a corporate egress proxy) and the audit, lineage, and operator surfaces to interrogate before bringing it inside a regulated perimeter.
- **Use-case workflows page** (`docs/use-cases.md`) - four most-asked patterns: continuous codebase audit, stale-PR triage, parallel adapter benchmarking, post-mortem evidence pack. Contributed by @zerone0x via #1048.
- Internal scheduler-LLM example bumped from `gemini-2.5-pro` to `gemini-3.1-pro`.

### Tooling

- README's CodeTrendy banner shrunk from a 104px image strip to an inline shields.io badge.
- `--max-agents` doc references replaced with the real `BERNSTEIN_MAX_AGENTS` env var (the public surface since 1.8).

## [1.10.0] - 2026-05-05

### Added - operator surface

- **Cluster-mode hardening** - native mTLS for node-to-node transport with `bernstein cluster bootstrap-ca`; real 2-process e2e test harness with 6 chaos scenarios (worker crash, central restart, network partition, token expiry, concurrent claims); 5 Prometheus metrics + 6 audit event types; documented Cloudflare Tunnel + Tailscale deployment patterns with nightly CI smoke.
- **Air-gap distribution** - `scripts/build_airgap_wheelhouse.py` resolves the pinned dep closure into a signed wheelhouse; `bernstein verify <wheelhouse>` checksum + signature verification (cosign default, GPG path); new `--profile airgap` egress gate denies adapter/MCP network calls outside an explicit allow-list; `bernstein doctor airgap` self-checks.
- **Per-artifact lineage trail** - every agent file write emits a signed record linking output (path + byte range + sha) to inputs, producer, prompt SHA, model, cost, tokens; schema v2 adds `regulatory_class` + customer-key Ed25519 signature for DORA/NIS2 evidence; tamper-loud detection in janitor with SIEM webhook + `bernstein lineage verify <run_id>`.
- **Lethal-trifecta capability matrix** - declarative tags (PRIVATE_DATA / UNTRUSTED_INPUT / EXTERNAL_COMM); spawn-time refusal of any agent whose tool chain unions all three; bypass-immune via `policy_engine.evaluate_lethal_trifecta`; phase-emit policies now ride the same matrix.

### Added - orchestration depth

- **CLM (Cyber Language Model) gateway adapter** - thin sovereign-LLM adapter wrapping `aider` against an OpenAI-compatible CLM gateway; tool-calling allowlist, streaming-assembly lineage, opt-in mTLS via Phase 2.5 launcher shim.
- **Phase pipeline** - discrete research/plan/implement/verify phase separation with distilled JSON handoffs; per-phase JSON-Schema validation registered as capability-matrix policy; R001-R005 mechanical exit gates (no-open-questions, decisions-reference-prior, acyclic graph, monotonic constraints, byte budget) with re-fire on violation; gate results land in lineage trail.
- **Action cache** - `core/persistence/action_cache.py` layered on the new `MemoStore` for deterministic replay; `bernstein cache action stats|replay <run_id>`.
- **Fingerprint memoization** - `hash(args) + hash(fn-AST)` keys; applied to cross-model verifier, knowledge-graph extractor, RAG embedder; the `test_changed_function_body_changes_key` regression closes the silent-stale-cache bug.
- **Rework-rate ledger** - file-backed `(model, effort, phase, outcome)` JSONL under `.sdd/runtime/rework/`; cascade router auto-promotes (e.g. `sonnet → opus`) once the bucket exceeds `promotion_threshold=0.30` with `min_samples=20`.
- **Best-of-N delegation** - opt-in parallel candidate spawning with judge-based selection; new `BEST_OF_N` defaults section; per-task `Task.best_of_n=K` override.
- **Swarm migration** - `bernstein migrate` map-reduce fanout over file globs; idempotent via `.sdd/runtime/swarm/<plan>.json`; 2 starter migration templates.
- **Discrete phase pipeline** - opt-in via `defaults.PHASE_PIPELINE.enabled` and per-step `phases:` field in plan YAML.

### Added - quality + planning

- **AST-aware reviewer chunking** - Python reviewer never receives a chunk that splits a function or class.
- **Abstracted code review** - intent + pseudocode summary on diffs; cheap-tier reviewer with opus disallowed; collapsible raw-diff blocks in PR body.
- **Schema-validation retry** - cross-step error accumulation with `SchemaRetryContext`; wired into manager parsing + MCP tool result decoding.
- **Spec-as-test loop** - generates executable assertions from the immutable feature contract; gates on drift.
- **Feature contract** - `.sdd/contract/features.json` with anchor over immutable fields + HMAC chain anchor; tampering surfaces `TamperingDetectedError`.
- **Incident-to-eval synthesis** - terminally-failed tasks become regression eval cases under `eval/incident_synthesizer.py`.

### Added - protocols + integrations

- **Tool-search lazy loading** - meta-tool with BM25 ranking keeps MCP tool descriptions out of context until invoked.
- **Static service manifest** - `/.well-known/agent.json` (A2A-compliant) + `/llms.txt` from a single dataclass-driven endpoint table.
- **Spawner SandboxSession routing** - non-worktree backends now exec through `SandboxSession.exec()` with per-session asyncio loop; worktree backend stays on the legacy direct-subprocess path.
- **Session handoff** - `bernstein handoff emit|claim|status`; `/handoff` chat slash-command + dashboard route; ring buffer for stream-tail replay.
- **Routine-scenario bridge** - bidirectional `RoutineProvisioner` + 8 scenario templates; `bernstein routine scenarios|export|provision|register|bindings`.
- **Agent-mode profiles** - declarative `templates/mode_profiles/{smart,deep,fast}.yaml`; deterministic family mapping (sonnet/opus → smart, haiku/qwen/ollama → fast, gpt-5*/o-series → deep).
- **cocoindex-code MCP catalog entry** - registered as opt-in (`mcp.catalog.cocoindex_code.enabled = false` by default).

### Changed

- **Model catalogue refresh** - added GPT-5.5 / GPT-5.5-mini to cost + cascade tables; refreshed top-7 adapter install commands (claude, codex, gemini, ollama, cursor, aider, opencode); `Last verified 2026-05-05` markers on every adapter docstring.
- **Default branch** - direct push to `main` is the convention everywhere; documentation + scripts updated to never reference `master`.

### Documentation

- Full doc audit covering every feature shipped this release; new pages under `docs/concepts/`, `docs/cluster/`, `docs/observability/`, `docs/compliance/`, `docs/sandbox/`, `docs/installation/`, `docs/adapters/`. Every feature page covers: one-line description, why, how-to, configuration knobs, limitations, related.

## [1.7.0] - 2026-04-14

### Added
- **Cloudflare integration platform** (twelve modules):
  - Workers RuntimeBridge (`bridges/cloudflare.py`) - agent execution on Workers + Durable Objects
  - Workflow Bridge (`bridges/cloudflare_workflow.py`) - durable multi-step workflows with auto-retry and approval gates
  - Sandbox Bridge (`bridges/cloudflare_sandbox.py`) - V8 isolate and container sandboxes for isolated code execution
  - Browser Rendering Bridge (`bridges/browser_rendering.py`) - headless web browsing, screenshots, scraping, PDF generation
  - R2 Workspace Sync (`bridges/r2_sync.py`) - content-addressed delta file sync via Cloudflare R2
  - Workers AI Provider (`core/routing/cloudflare_ai.py`) - free-tier LLM models (Llama 3.1, Mistral, Gemma, Qwen) for planning
  - D1 Analytics Client (`core/cost/d1_analytics.py`) - usage metering, billing tiers (free/pro/team/enterprise), quota enforcement
  - MCP Remote Transport (`mcp/remote_transport.py`) - streamable HTTP transport for remote MCP server access
  - Cloud CLI (`cli/commands/cloud_cmd.py`) - `bernstein cloud` subcommands: login, logout, run, status, runs, cost, deploy
  - Cloudflare Agents Adapter (`adapters/cloudflare_agents.py`) - spawn agents via `npx wrangler dev`
  - Codex-on-Cloudflare Adapter (`adapters/codex_cloudflare.py`) - run Codex in Cloudflare sandboxes
- Full Cloudflare documentation: overview, setup, bridges, adapters, Workers AI, analytics, CLI, MCP remote (8 new doc pages)

## [1.4.11] - 2026-04-03

### Added
- **Bernstein doctor** - comprehensive pre-flight health check: adapters, API keys, ports, `.sdd/` integrity, MCP servers. Auto-repair mode with `--fix`.
- **Per-agent token progress** - real-time token usage tracking per spawned agent, surfaced in `bernstein status`.
- **Context injection token budget** - explicit budgets for injected context (files, lessons, RAG chunks) with graceful truncation and priority ordering.
- **Output style customization** - configurable agent output format via markdown templates.
- **Installation mismatch detection** - detects gaps between expected and installed adapter capabilities.
- **API preconnect warmup** - connection warmup before heavy runs to reduce first-request latency.
- **Worker badge identity** - process identification visible in `bernstein ps` and Activity Monitor.
- **TUI keybinding system** - configurable keyboard shortcuts in the Textual dashboard.
- **Progressive permission prompts** - per-agent permission levels for fine-grained control.
- **Activity tracking metrics** - session-level activity statistics and agent usage patterns.
- **Away summary generation** - summarize what happened while you were away.
- **Commit attribution stats** - per-agent commit statistics.
- **Session analytics** - cumulative insights across runs.
- **Settings snapshot in traces** - agent settings preserved in execution traces.
- **Side question support** - agents can ask clarifying questions mid-task.
- **Diff folding display** - folded diff rendering in agent output.
- **Word-level diff rendering** - character-level change highlighting.
- **Contextual tips system** - in-context hints for agents.
- **Session tag system** - tag and filter runs.
- **Rename session** - session renaming command.
- **Security review command** - `bernstein security-review` for vulnerability assessment.
- **Cumulative progress tracking** - progress tracking across runs.
- **Plugin trust warning** - warns on unverified plugins.
- **Plugin error reporting** - improved error diagnostics for plugin failures.
- **Extra usage provisioning** - additional usage quota management.
- **Truecolor mode detection** - automatic terminal color capability detection.
- **Dirty flag layout caching** - caching optimizations for dirty project detection.
- **Release notes display** - show release notes on startup.

### Fixed
- Context warnings in `bernstein doctor` output for better diagnostics.
- Circuit breaker for repeated compact failures - prevents agent thrashing.

### Changed
- Documentation overhaul: README, GETTING_STARTED, ARCHITECTURE, FEATURE_MATRIX, BENCHMARKS, CHANGELOG, CONTRIBUTING all rewritten against v1.4.11 codebase.

## [1.4.9] - 2026-04-01

### Added
- Process-aware shutdown/drain improvements across CLI and core lifecycle paths.
- Cost analytics enhancements (additional endpoints/aggregation work and routing transparency updates).
- Security enhancements including sensitivity-classification and IP-allowlist related hardening.
- TUI keyboard help (`?`) shortcut support.

### Changed
- Issue triage and documentation alignment pass so docs match shipped behaviour.
- Retry, lifecycle, and observability narratives updated to better reflect current implementation boundaries.

## [1.4.0] - 2026-03-31

### Added
- **Plan Files**: loadable YAML project plans with stages and steps (`bernstein run plan.yaml`)
- **Server Supervisor**: auto-restart on crash with exponential backoff (max 5 restarts / 10 min)
- **CrashGuard Middleware**: catches unhandled exceptions → 500 instead of process death
- **Orchestrator drain mode**: loop continues while agents are active, even after stop signal
- **Quality gates**: PII scan, mutation testing, benchmark regression detection
- **Gate Runner**: parallel execution of all quality gates (asyncio)
- **Benchmark regression gate**: block merge when performance degrades beyond threshold
- **PII log redaction**: auto-installed filter scrubs emails, phones, SSNs, credit cards from all log output
- **Agent loop detection**: kills agents caught in edit-loop cycles (same file edited N+ times in window)
- **Deadlock detection**: wait-for graph cycle detection with automatic victim selection
- **Cost anomaly detection**: Z-score based cost anomaly signaling with configurable thresholds
- **Per-agent file/command permissions**: role-based matrix restricting which files and commands each role may use
- **Premium visual theme**: CRT power-off effects, gradient splash, block-art logo
- **Live boot log**: orchestrator boot progress shown in Agents panel while no agents spawned
- **Persistent memory**: SQLite-backed cross-session agent memory
- **Context handoff**: structured context briefs for subtask delegation
- **Zero-config mode**: auto-detect project type, no bernstein.yaml required
- **Worktree environment hooks**: auto-symlink node_modules, copy .env
- **FIFO merge queue**: sequential merge with git merge-tree conflict pre-check
- **Ticket Format v1**: YAML frontmatter with model routing, janitor signals, tags
- **10 adapters**: Claude, Codex, Cursor, Gemini, Kiro, OpenCode, Aider, Amp, Roo Code, Generic
- **Futuristic splash screen**: full-screen animated boot sequence
- **Plan display**: mission-briefing style execution plan approval
- **test_cli_run_params.py**: catches cli() → run() parameter sync bugs

### Fixed
- Manager always uses opus/max (was falling back to haiku via fast_path)
- Orchestrator no longer exits while agents still running
- Server failure backoff: 5s per failure instead of constant polling
- Startup crash: missing pii_scan fields in QualityGatesConfig
- .yaml/.md backward compatibility in all backlog parsers

### Changed
- Ticket format migrated from .md to .yaml (YAML frontmatter)
- Version bump 1.3.x → 1.4.0

## [1.0.3] - 2026-03-30

### Added
- State-of-the-art CI/CD pipeline: 11 new GitHub Actions workflows
- Three-tier AI PR review (GitHub Models + Gemini CLI + Bernstein deep review)
- Semgrep SAST, license compliance, spelling, dead code analysis, workflow linting
- PR auto-labeling, size warnings, stale cleanup, Dependabot auto-merge
- Release Drafter for automated changelog generation
- Telegram bot notifications on CI completion
- Codecov coverage gating (85% project / 70% patch)
- Concurrency groups on all workflows with cancel-in-progress
- CI and Codecov badges in README

### Changed
- FEATURE_MATRIX updated with CI/CD section (15 new entries)
- GETTING_STARTED expanded with CI pipeline documentation
- Manual backlog index updated with all setup tickets and status tracking

## [1.0.2] - 2026-03-28

### Changed
- Documentation audit: updated outdated model names, CLI references, API endpoints, and GitHub Action version tags
- Default branch references updated from `master` to `main` across all docs

## [1.0.0] - 2026-03-28

### Added
- ACP (Agent Communication Protocol) endpoints for agent interoperability
- A2A (Agent-to-Agent) protocol support
- Cluster mode with multi-node coordination (node registration, heartbeat, status)
- Auth routes: OIDC, SAML, CLI device flow, group mappings, user management
- Graduation system for agent promotion based on performance
- Plans routes for plan listing, approval, and rejection
- Slack integration (slash commands and events)
- Quality dashboard with per-model quality metrics
- Cost history, live cost tracking, and cost alerts endpoints
- File lock tracking via dashboard routes
- Task prioritization, force-claim, and progress reporting endpoints
- Chaos testing CLI group
- Audit CLI group
- Verify CLI command

### Changed
- Version bumped to 1.0.0 (stable release)
- Route modules expanded: acp.py, auth.py, graduation.py, plans.py, slack.py added to core/routes/

## [0.3.0] - 2026-03-28

### Added
- Checkpoint and wrap-up CLI commands for session management
- Task snapshots endpoint for viewing task state history
- Webhook alerts endpoint
- SSE event stream at `/events` for real-time dashboard updates
- Prometheus `/metrics` endpoint for observability
- Bandit-based model routing stats at `/routing/bandit`
- Cache stats endpoint at `/cache-stats`

### Changed
- CLI decomposed further: audit_cmd.py, chaos_cmd.py, checkpoint_cmd.py, verify_cmd.py, wrap_up_cmd.py
- Task server routes expanded with block, progress, and prioritize actions

## [0.2.0] - 2026-03-28

### Added
- Agent discovery system with multi-provider routing (`cli: auto`)
- Quality gates for task verification
- Rule enforcement engine
- Token monitor for real-time usage tracking
- Approval gates for high-risk operations
- MCP server integration
- Hot reload for configuration changes
- Aider, Amp, and Roo Code adapters
- Adapter manager and caching adapter layer
- Environment isolation for adapter processes
- Web dashboard with real-time SSE updates
- Workspace management for multi-repo orchestration
- GitHub App integration for webhook-driven tasks
- Auth middleware and checkpoint commands
- Delegate, trigger, and wrap-up CLI commands

### Changed
- Default CLI adapter is now `auto` (detects installed agents) instead of `claude`
- Test count badge updated: 2500+ to 4250+ (142 test files, 4257 test functions)
- Server decomposed into `core/routes/` (tasks.py, status.py, webhooks.py, costs.py, agents.py, auth.py, dashboard.py, plans.py, quality.py, graduation.py, slack.py)
- Orchestrator decomposed into tick_pipeline.py, task_lifecycle.py, agent_lifecycle.py
- CLI decomposed into helpers.py, run_cmd.py, stop_cmd.py, status_cmd.py, agents_cmd.py, evolve_cmd.py, advanced_cmd.py, and more
- TaskStore extracted to task_store.py with PostgreSQL and Redis backends
- `bernstein catalog` commands renamed to `bernstein agents` (sync, list, validate)
- Adapter listing in DESIGN.md updated to include all current adapters (removed stale kiro.py)
- Example YAML files updated: `cli: claude` changed to `cli: auto`
- All documentation references to `bernstein catalog` updated to `bernstein agents`
- Removed stale "(default)" label from Claude adapter docs (default is now `auto`)

## [0.1.0] - 2026-03-28

### Added
- License: Apache 2.0
- Per-run cost budgeting (`--budget 5.00`) with threshold warnings
- CI auto-fix pipeline with GitHub Actions log parser
- GitHub Action (`action.yml`) for CI-triggered orchestration
- MCP tool access - agents use MCP servers (stdio/SSE)
- TUI session manager (`bernstein live`) with Textual
- "The Bernstein Way" architecture tenets document
- Quickstart demo (`examples/quickstart/`)
- GitHub Action documentation (`docs/github-action.md`)
- Feature cards for cost budgeting, GitHub Action, MCP on index page
- `docs/zero-lock-in.md` - model-agnostic architecture deep dive
- `docs/CHANGELOG.md` - this file
- `docs/VERSION` - documentation version tracking

### Changed
- All license references updated to Apache 2.0 across all HTML and markdown docs
- README: quickstart section with full install → init → run flow
- README: test count badge, license badge, benchmark badge
- Getting Started: fixed test command to use isolated runner
- Comparison table: added cost budgeting and GitHub Action rows
