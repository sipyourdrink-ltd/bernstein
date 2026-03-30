# Feature Coverage Matrix

This document tracks documentation parity between shipped capabilities and public-facing docs surfaces. It is a living document updated as features are added or documentation is expanded.

**Legend:**
- **Full** — feature documented with usage examples and flags
- **Brief** — mentioned but without detail
- **No** — not mentioned at all
- **N/A** — not applicable to this surface

Last audited: 2026-03-30

## Core Orchestration

| Feature | CLI Command | README | GETTING_STARTED | Site Page | Status |
|---------|-------------|--------|-----------------|-----------|--------|
| Goal-based orchestration | `bernstein -g "..."` | Full | Full | Full (index) | OK |
| Seed file (YAML) | `bernstein` (auto-discovers) | Full | Full | Full (getting-started) | OK |
| Dry-run mode | `--dry-run` | Brief | Full | No | Partial |
| Headless mode | `--headless` | Brief | Full | No | Partial |
| Plan-only mode | `--plan-only` | Brief | No | No | Gap |
| From-plan mode | `--from-plan` | Brief | No | No | Gap |
| Auto-approve / approval modes | `--auto-approve`, `--approval` | No | No | No | Gap |
| Merge strategy | `--merge pr\|direct` | No | No | No | Gap |
| Task server API | HTTP endpoints | Full | Full | Full (api) | OK |
| Multi-repo workspace | `workspace` group | Brief | Full | No | Partial |
| Configuration | `config set/get/list/validate` | No | No | No | Gap |

## Agent Management

| Feature | CLI Command | README | GETTING_STARTED | Site Page | Status |
|---------|-------------|--------|-----------------|-----------|--------|
| Agent catalog sync | `agents sync` | Full | Brief | No | Partial |
| Agent listing | `agents list` | Full | Brief | No | Partial |
| Agent validation | `agents validate` | Full | Brief | No | Partial |
| Agent showcase | `agents showcase` | No | No | No | Gap |
| Agent matching | `agents match` | No | No | No | Gap |
| Agent discovery | `agents discover` | No | No | No | Gap |
| Adapter registry | `--cli` flag | Full | Brief | Full (adapters) | OK |

## Monitoring & Observability

| Feature | CLI Command | README | GETTING_STARTED | Site Page | Status |
|---------|-------------|--------|-----------------|-----------|--------|
| Process listing | `ps` | Full | Full | No | Partial |
| Health check | `doctor` | Full | Full | No | Partial |
| Live TUI dashboard | `live` | Full | Full | No | Partial |
| Web dashboard | `dashboard` | Brief | Full | No | Partial |
| Cost tracking | `cost` | Full | Full | No | Partial |
| Execution trace | `trace` | Full | Brief | No | Partial |
| Execution replay | `replay` | Full | Brief | No | Partial |
| Log tailing | `logs` | Brief | Full | No | Partial |
| Task backlog view | `plan` | Brief | No | No | Gap |
| Retrospective report | `retro` | Brief | Full | No | Partial |
| Recap (post-run summary) | `recap` | No | No | No | Gap |
| Prometheus metrics | `/metrics` endpoint | Brief | Brief | No | Partial |

## Governance & Audit

| Feature | CLI Command | README | GETTING_STARTED | Site Page | Status |
|---------|-------------|--------|-----------------|-----------|--------|
| HMAC chain verification | `audit verify-hmac` | Full | No | No | Gap |
| Audit log query | `audit query` | Full | No | No | Gap |
| Audit log viewer | `audit show` | No | No | No | Gap |
| Merkle seal | `audit seal` | No | No | No | Gap |
| Merkle verify | `audit verify` | No | No | No | Gap |
| WAL integrity check | `verify --wal-integrity` | Full | No | No | Gap |
| Determinism check | `verify --determinism` | Full | No | No | Gap |
| Memory audit | `verify --memory-audit` | No | No | No | Gap |
| Formal verification | `verify --formal` | No | No | No | Gap |
| Run manifest | `manifest show/list/diff` | Brief | No | No | Gap |

## Evolution & Self-Improvement

| Feature | CLI Command | README | GETTING_STARTED | Site Page | Status |
|---------|-------------|--------|-----------------|-----------|--------|
| Evolve mode | `--evolve` | Full | Full | No | Partial |
| Evolve run (manual) | `evolve run` | Brief | Full | No | Partial |
| Evolve review | `evolve review` | Full | Full | No | Partial |
| Evolve approve | `evolve approve` | Full | Full | No | Partial |
| Evolve status | `evolve status` | No | No | No | Gap |
| Evolve export | `evolve export` | No | No | No | Gap |
| Creative ideation | `ideate` | Brief | Full | No | Partial |

## CI/CD Integration

| Feature | CLI Command / Config | README | GETTING_STARTED | Site Page | Status |
|---------|---------------------|--------|-----------------|-----------|--------|
| CI autofix | `ci fix` | Full | No | No | Gap |
| CI watch | `ci watch` | Full | No | No | Gap |
| GitHub App | `github setup/test-webhook` | Full | No | No | Gap |
| GitHub Action | (external) | Brief | No | No | Gap |
| CI pipeline | `.github/workflows/ci.yml` | Badge | N/A | N/A | OK |
| Codecov gating | `codecov.yml` (85%/70%) | Badge | N/A | N/A | OK |
| AI PR review (GitHub Models) | `ai-pr-review.yml` | N/A | N/A | N/A | OK |
| AI PR review (Gemini CLI) | `ai-pr-review-gemini.yml` | N/A | N/A | N/A | OK |
| Telegram notifications | `telegram-notify.yml` | N/A | N/A | N/A | OK |
| PR auto-labeling | `labeler.yml` | N/A | N/A | N/A | OK |
| PR size warnings | `pr-size.yml` | N/A | N/A | N/A | OK |
| Stale cleanup | `stale.yml` | N/A | N/A | N/A | OK |
| Dependabot auto-merge | `dependabot-auto-merge.yml` | N/A | N/A | N/A | OK |
| Semgrep SAST | `semgrep.yml` | N/A | N/A | N/A | OK |
| License compliance | `license-compliance.yml` | N/A | N/A | N/A | OK |
| Release Drafter | `release-drafter.yml` | N/A | N/A | N/A | OK |
| Spelling (typos) | CI job | N/A | N/A | N/A | OK |
| Dead code (Vulture) | CI job | N/A | N/A | N/A | OK |
| Workflow lint (actionlint) | CI job | N/A | N/A | N/A | OK |

## Benchmarking & Evaluation

| Feature | CLI Command | README | GETTING_STARTED | Site Page | Status |
|---------|-------------|--------|-----------------|-----------|--------|
| Benchmark run | `benchmark run` | Full | Full | No | Partial |
| Benchmark compare | `benchmark compare` | Full | No | No | Gap |
| SWE-bench harness | `benchmark swe-bench` | No | No | No | Gap |
| Eval run | `eval run` | No | No | No | Gap |
| Eval report | `eval report` | No | No | No | Gap |
| Eval failures | `eval failures` | No | No | No | Gap |

## Advanced Features

| Feature | CLI Command | README | GETTING_STARTED | Site Page | Status |
|---------|-------------|--------|-----------------|-----------|--------|
| Chaos engineering | `chaos agent-kill/rate-limit/file-remove/status/slo` | No | No | No | Gap |
| Gateway proxy | `gateway start/replay` | No | No | No | Gap |
| Workflow DSL | `workflow validate/list/show` | Brief | No | No | Gap |
| MCP server mode | `mcp` | No | No | No | Gap |
| Task quarantine | `quarantine list/clear` | No | No | No | Gap |
| File watch | `watch` | No | No | No | Gap |
| Voice commands | `listen` | No | No | No | Gap |
| Session checkpoint | `checkpoint` | Brief | No | No | Gap |
| Session wrap-up | `wrap-up` | No | No | No | Gap |
| Task diff | `diff` | No | No | No | Gap |
| Self-update | `self-update` | No | No | No | Gap |
| Cluster worker | `worker` | No | No | No | Gap |
| Quickstart | `quickstart` | No | No | No | Gap |
| Shell completions | `completions` | No | No | No | Gap |
| Git hooks | `install-hooks` | No | No | No | Gap |
| Plugin system | `plugins` | Brief | No | No | Gap |
| Demo mode | `demo` | Full | Full | No | Partial |

## Task Management CLI

| Feature | CLI Command | README | GETTING_STARTED | Site Page | Status |
|---------|-------------|--------|-----------------|-----------|--------|
| Add task | `add-task` | No | No | No | Gap |
| Cancel task | `cancel` | Full | No | No | Gap |
| Sync backlog | `sync` | No | No | No | Gap |
| Review trigger | `review` | No | No | No | Gap |
| Approve task | `approve` | No | No | No | Gap |
| Reject task | `reject` | No | No | No | Gap |
| Pending tasks | `pending` | No | No | No | Gap |
| List tasks | `list-tasks` | No | No | No | Gap |

## Unregistered Modules (Dead Code)

| Feature | Module | Status | Decision |
|---------|--------|--------|----------|
| SSO authentication | `auth_cmd.py` | Ready to ship | Wire into main.py when server routes are tested |
| Task explanation | `explain_cmd.py` | WIP | Routing heuristic may not match live router |
| Event triggers | `triggers_cmd.py` | WIP | Hard-coded server URL in `fire` command |
| Task delegation | `delegate_cmd.py` | Ready to ship | Wire into main.py |

## Summary

| Category | Total Features | Fully Documented | Partial | Gap |
|----------|---------------|------------------|---------|-----|
| Core Orchestration | 11 | 3 | 4 | 4 |
| Agent Management | 7 | 2 | 3 | 3 (discovery/showcase/match undocumented) |
| Monitoring | 12 | 0 | 9 | 3 |
| Governance & Audit | 10 | 0 | 0 | 10 |
| Evolution | 7 | 0 | 5 | 2 |
| CI/CD | 19 | 15 | 0 | 4 |
| Benchmarking | 6 | 0 | 1 | 5 |
| Advanced Features | 17 | 0 | 2 | 15 |
| Task Management CLI | 8 | 0 | 0 | 8 |
| **Total** | **97** | **20** | **24** | **54** |
