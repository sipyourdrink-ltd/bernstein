# Workflow Registry

This registry maps workflow specs in `docs/workflows/` to current implementation status in the codebase.

Last updated: 2026-04-03

---

## Canonical workflow specs

| Workflow spec | File | Runtime status | Notes |
|---|---|---|---|
| CI failure routing | `WORKFLOW-ci-failure-routing.md` | Partial | Core plumbing exists; depends on webhook/app setup |
| Event-driven triggers | `WORKFLOW-event-driven-triggers.md` | Partial | TriggerManager + source adapters are implemented |
| Env var isolation | `WORKFLOW-env-var-isolation.md` | Shipped | Adapter env filtering is active |
| Rate-limit-aware scheduling | `WORKFLOW-rate-limit-aware-scheduling.md` | Partial | Tracking/routing logic exists; tuning remains workload-specific |
| Self review before PR | `WORKFLOW-self-review-before-pr.md` | Partial | Reviewer/verifier paths exist; policy depends on config |
| Extension publishing | `WORKFLOW-extension-publishing.md` | Spec-only | Process spec; not orchestration core |
| Extension UX | `WORKFLOW-extension-ux.md` | Spec-only | Product UX spec |
| Protocol compatibility matrix | `WORKFLOW-protocol-compatibility-matrix.md` | Partial | Docs and checks exist; matrix should not be treated as source-of-truth for runtime health |
| Compatibility table generation | `WORKFLOW-compatibility-table-generation.md` | Partial | Documentation/support workflow |
| Release breaking-change detection | `WORKFLOW-release-breaking-change-detection.md` | Partial | CI/release process workflow |
| Context collapse with drain retry | `WORKFLOW-context-collapse-drain-retry.md` | Draft | T493 — bounded drain retry loop for spawn prompt context overflow |

Archived/deprecated reference docs remain under `docs/workflows/archive/`.

---

## Code ownership map (current)

### Trigger and event workflows

- `src/bernstein/core/trigger_manager.py`
- `src/bernstein/core/trigger_sources/github.py`
- `src/bernstein/core/trigger_sources/slack.py`
- `src/bernstein/core/trigger_sources/file_watch.py`
- `src/bernstein/core/trigger_sources/webhook.py`
- `src/bernstein/core/routes/webhooks.py`
- `src/bernstein/core/routes/slack.py`

Trigger config path: `.sdd/config/triggers.yaml`

### CI and GitHub workflows

- `src/bernstein/core/ci_fix.py`
- `src/bernstein/core/ci_log_parser.py`
- `src/bernstein/github_app/mapper.py`
- `src/bernstein/github_app/webhooks.py`

### Context collapse and prompt budget workflows

- `src/bernstein/core/context_collapse.py` — 3-stage collapse pipeline (truncate, drop, strip)
- `src/bernstein/core/context_compression.py` — PromptCompressor fallback
- `src/bernstein/core/spawn_prompt.py` — prompt assembly + collapse integration
- `src/bernstein/core/tick_pipeline.py` — tick-level collapse entry point
- `src/bernstein/core/auto_compact.py` — circuit breaker for runtime compaction

### Retry, scheduling, and lifecycle workflows

- `src/bernstein/core/task_lifecycle.py`
- `src/bernstein/core/task_completion.py`
- `src/bernstein/core/rate_limit_tracker.py`
- `src/bernstein/core/router.py`
- `src/bernstein/core/cascade_router.py`

### Review and quality workflows

- `src/bernstein/core/cross_model_verifier.py`
- `src/bernstein/core/reviewer.py`
- `src/bernstein/core/quality_gates.py`
- `src/bernstein/core/approval.py`
- `src/bernstein/core/janitor.py`

---

## Workflow maturity model

Use this interpretation when reading workflow specs:

- `Shipped`: implementation exists and is expected in normal runs.
- `Partial`: implementation exists, but behavior depends on config/environment and may require operator setup.
- `Spec-only`: workflow is documented for process/roadmap alignment, not guaranteed as turnkey runtime behavior.

---

## Maintenance rules

When updating this registry:

1. Prefer code paths and route modules as source of truth.
2. Do not mark a workflow `Shipped` unless it is active without one-off patches.
3. For trigger-related docs, use current source adapter names (`github`, `slack`, `file_watch`, `webhook`).
