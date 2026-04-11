# Bernstein Examples

Working examples showing how to use Bernstein for multi-agent orchestration.

## Quick Start

```bash
# Simplest possible — one goal, Bernstein figures out the rest
bernstein run examples/simple.yaml

# Full config — all knobs exposed
bernstein run examples/full.yaml
```

## Plan Library (24 patterns)

Self-contained plans in `plans/` that run against any compatible project.
Each plan defines stages, steps with specialist roles, and machine-checkable
completion signals.

```bash
bernstein run examples/plans/<plan>.yaml
```

### Infrastructure & Deployment

| Plan | Description | Budget | Agents |
|------|-------------|--------|--------|
| [ci-cd-pipeline](plans/ci-cd-pipeline.yaml) | GitHub Actions CI/CD with testing gates and staged deployment | $15 | 4 |
| [zero-downtime-deploy](plans/zero-downtime-deploy.yaml) | Blue-green, rolling updates, and canary releases with Kubernetes | $25 | 5 |
| [database-migration](plans/database-migration.yaml) | Safe schema migration with idempotent scripts and rollback | $15 | 4 |
| [microservice-extraction](plans/microservice-extraction.yaml) | Extract bounded domain from monolith with zero downtime | $30 | 5 |
| [monorepo-feature](plans/monorepo-feature.yaml) | Multi-package monorepo feature with shared dependencies | $25 | 5 |
| [cobol-modernization](plans/cobol-modernization.yaml) | COBOL to Java migration with discovery and verification | $40 | 6 |

### Backend & APIs

| Plan | Description | Budget | Agents |
|------|-------------|--------|--------|
| [auth-system](plans/auth-system.yaml) | JWT + OAuth/SAML with hardened security and rate limiting | $18 | 4 |
| [flask-api](plans/flask-api.yaml) | Production Flask REST API with JWT auth and tests | $15 | 4 |
| [graphql-migration](plans/graphql-migration.yaml) | REST to GraphQL migration using strangler fig pattern | $20 | 4 |
| [api-versioning](plans/api-versioning.yaml) | Multi-version API strategy with deprecation path | $15 | 4 |
| [mobile-bff](plans/mobile-bff.yaml) | Backend-for-frontend optimized for mobile clients | $20 | 4 |
| [data-pipeline](plans/data-pipeline.yaml) | ETL with connectors, Polars transforms, and lineage tracking | $20 | 4 |
| [search-implementation](plans/search-implementation.yaml) | Full-text search with Meilisearch and sync pipeline | $18 | 4 |

### Architecture & Patterns

| Plan | Description | Budget | Agents |
|------|-------------|--------|--------|
| [event-driven-refactor](plans/event-driven-refactor.yaml) | Async event bus with sagas and idempotent handlers | $25 | 5 |
| [caching-layer](plans/caching-layer.yaml) | Cache-aside pattern with invalidation and monitoring | $18 | 4 |
| [cli-tool](plans/cli-tool.yaml) | Python CLI tool with Click, config management, and tests | $12 | 3 |

### Quality & Compliance

| Plan | Description | Budget | Agents |
|------|-------------|--------|--------|
| [testing-overhaul](plans/testing-overhaul.yaml) | Complete test infrastructure with fixtures and CI gates | $20 | 5 |
| [performance-optimization](plans/performance-optimization.yaml) | Profiling, bottleneck removal, and load testing | $20 | 4 |
| [compliance-remediation](plans/compliance-remediation.yaml) | Security audit, vulnerability fixes, and compliance suite | $18 | 4 |
| [accessibility-audit](plans/accessibility-audit.yaml) | WCAG compliance, remediation, and automated testing | $20 | 5 |
| [tech-debt-sprint](plans/tech-debt-sprint.yaml) | Systematic refactoring with code metrics and quality gates | $20 | 5 |
| [observability-stack](plans/observability-stack.yaml) | Structured logging, Prometheus, OpenTelemetry, Grafana | $18 | 4 |

### Documentation & Onboarding

| Plan | Description | Budget | Agents |
|------|-------------|--------|--------|
| [onboarding-docs](plans/onboarding-docs.yaml) | Architecture docs, ADRs, API references, and getting-started | $15 | 4 |
| [i18n-rollout](plans/i18n-rollout.yaml) | Multi-language support with translation management | $18 | 4 |

## Other Examples

### Goal-based (no stages)

- **[simple.yaml](simple.yaml)** — Minimal: one goal, Bernstein plans everything
- **[full.yaml](full.yaml)** — All configuration fields documented
- **[rag-system.yaml](rag-system.yaml)** — RAG system with constraints and context files

### Working Projects

- **[quickstart/](quickstart/)** — Flask TODO API that Bernstein fixes (validation, errors, tests)
- **[todo-app/](todo-app/)** — Sample target project for running plans against
- **[cli-tool/](cli-tool/)** — Sample CLI project skeleton

### Configuration Examples

- **[multi-model/](multi-model/)** — Using multiple agents with different models
- **[model_policy/](model_policy/)** — Model routing and cost policies

### Plugin Examples

- **[plugins/](plugins/)** — Custom plugins: Slack/Discord notifiers, Jira/Linear integration, custom routers, quality gates, metrics, and more

## Plan Anatomy

Every plan in the library follows this structure:

```yaml
name: "Plan Name"
description: "What this plan builds or changes"

cli: auto          # agent backend (auto|claude|codex|gemini)
budget: "$20"      # spending cap
max_agents: 4      # max concurrent agents
constraints:       # tech requirements passed to agents
  - "Python 3.12+"

stages:
  - name: "Stage Name"
    depends_on: []   # runs after these stages complete
    steps:
      - title: "What to build"
        role: backend          # specialist role
        scope: medium          # small|medium|large
        complexity: medium     # low|medium|high
        description: "Detailed instructions for the agent"
        files: ["src/app.py"]  # files this step owns
        completion_signals:    # machine-checkable verification
          - type: path_exists
            path: "src/app.py"
          - type: test_passes
            command: "pytest tests/ -x -q"
```

## Writing Your Own Plans

1. Copy `templates/plan.yaml` or any plan from `plans/` as a starting point
2. Define stages with clear dependencies
3. Each step = one task for one agent. Keep steps focused.
4. Add `completion_signals` so Bernstein can verify success automatically
5. Run: `bernstein run your-plan.yaml`
