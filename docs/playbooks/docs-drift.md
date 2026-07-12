# Docs drift playbook

This playbook enumerates every documentation file in the bernstein repository,
points each one at a concrete source of truth in code (or marks it static), and
records the drift signal that should trigger a refresh.

## Why this exists

Code and docs drift apart over time. This playbook gives future agents
mechanically discoverable check-points so the gap can be detected and closed
without re-reading every file. The companion CI workflow at
`.github/workflows/docs-drift.yml` consumes this file via
`scripts/check_docs_drift.py` and reports drift on every push to `main` and on
every pull request.

## How agents pick up drift

Convention: when you change code under `src/bernstein/<package>/`, also update
the doc rows in the table below that name that package as their source of
truth. The CI workflow will block merges to `main` on uncorrected drift; on
pull requests it posts a non-blocking comment so the change can land in the
same PR.

Drift remediation paths used by the rows below:

| Remediation token | What it runs |
|-------------------|--------------|
| `agents-md-sync` | `uv run bernstein agents-md sync` then `uv run bernstein agents-md verify` |
| `gen-agents-md` | `uv run python scripts/gen_agents_md.py --update` (legacy detailed module map; do not run if `agents-md-sync` is the active source of truth) |
| `manual-prose` | Re-read the source-of-truth module and adjust the prose by hand; no script generates this content |
| `manual-cmd` | The doc lists a CLI command surface; re-run `bernstein <cmd> --help` and reconcile |
| `gen-benchmarks` | `uv run python scripts/generate_benchmark_docs.py` |
| `static` | No code source; check only repo URL / contact / license updates |

## Doc inventory

### Root (`./`)

| Doc | Source of truth | Drift signal | Remediation |
|-----|-----------------|--------------|-------------|
| `README.md` | `src/bernstein/cli/main.py`, `src/bernstein/cli/commands/`, `src/bernstein/adapters/registry.py`, `pyproject.toml` (`[project.scripts]`, `[project.optional-dependencies]`) | New top-level command, adapter added or removed, install method changed, optional extra added | `manual-prose` |
| `AGENTS.md` | `src/bernstein/` package layout (auto-derived); curated content under `.sdd/agents-md/` | New top-level package or module under `src/bernstein/`; any change to the canonical IR | `agents-md-sync` |
| `CLAUDE.md` | Mirror of canonical IR via `bernstein agents-md sync` | Drift versus AGENTS.md canonical | `agents-md-sync` |
| `CONVENTIONS.md` | Mirror of canonical IR for Aider via `bernstein agents-md sync` | Drift versus AGENTS.md canonical | `agents-md-sync` |
| `CODE_OF_CONDUCT.md` | None (Contributor Covenant 2.1 verbatim) | Repo URL change, contact email change | `static` |
| `CONTRIBUTING.md` | `src/bernstein/adapters/registry.py`, `src/bernstein/adapters/base.py`, `templates/roles/`, `scripts/run_tests.py`, `.importlinter` | New adapter contract method, new role added under `templates/roles/`, change to lint / type-check pipeline | `manual-prose` |
| `SECURITY.md` | `pyproject.toml` version, security policy contacts | Bounty program changes, scope changes, new in-scope target | `manual-prose` |
| `CHANGELOG.md` | Release-please managed (`release-please-config.json`, `release-please-manifest.json`) plus hand-curated `## Unreleased` section | New release tag, manual entry needed for behaviour-visible code change | `manual-prose` |
| `CONTRIBUTORS.md` | None (hand-curated list of named contributors) | New contributor merged a PR | `static` |

### `docs/` top-level

| Doc | Source of truth | Drift signal | Remediation |
|-----|-----------------|--------------|-------------|
| `docs/index.md` | All docs subdirs (each linked entry must resolve) | Linked page renamed / deleted, getting-started / installation / operations / reference / architecture path moved | `manual-prose` |
| `docs/adapter-deferred.md` | `src/bernstein/adapters/registry.py` (negative-space: agents NOT integrated) | A previously-deferred agent now has a stable CLI binary | `manual-prose` |
| `docs/agents-md.md` | `src/bernstein/cli/commands/agents_md_cmd.py`, `src/bernstein/core/knowledge/agents_md_bridge.py`, `src/bernstein/core/knowledge/agents_md_generator.py` | New target format added to the canonical IR, sync command options change | `manual-prose` |
| `docs/CHANGELOG.md` | Mirror of root `CHANGELOG.md` for mkdocs | Root changelog edited | `manual-prose` |
| `docs/CODE_REVIEW.md` | `src/bernstein/core/quality/`, `src/bernstein/core/review/`, `src/bernstein/core/review_responder/` | Review pipeline stage added, reviewer-role policy change | `manual-prose` |
| `docs/ENTERPRISE.md` | `src/bernstein/core/compliance/`, `src/bernstein/core/security/`, audit / lineage / air-gap surface | New regulator mapping, new compliance pack target, audit export schema change | `manual-prose` |
| `docs/lineage.md` | `src/bernstein/core/lineage/`, `src/bernstein/core/persistence/lineage.py`, `src/bernstein/cli/commands/lineage_cmd.py` | Lineage record schema change, signature algorithm change, new verify CLI subcommand | `manual-prose` |
| `docs/llm-citation-surface.md` | None (positioning note about how the project surfaces in LLM citations) | External citation pattern audited | `static` |
| `docs/routine-scenarios.md` | `src/bernstein/core/planning/routine_bridge.py`, `src/bernstein/cli/commands/routine_cmd.py`, `src/bernstein/mcp/routine_tools.py` | Routine <-> Scenario bridge surface change | `manual-prose` |
| `docs/telemetry.md` | `src/bernstein/core/telemetry/`, `src/bernstein/cli/commands/telemetry_cmd.py` | New telemetry event, opt-out matrix change, retention policy change | `manual-prose` |
| `docs/use-cases.md` | `src/bernstein/cli/main.py` (command list) plus operator workflow CLIs | New operator workflow command (`autofix`, `review-responder`, `dep-impact`, etc.) | `manual-prose` |
| `docs/whats-new.md` | Release tags, hand-curated highlights | New release published | `manual-prose` |

### `docs/concepts/`

Each file in this folder pairs a Bernstein architectural concept with a concrete
module path. The drift signal in every row is "the named source module has
been moved, renamed, deleted, or its public surface changed."

| Doc | Source of truth | Drift signal | Remediation |
|-----|-----------------|--------------|-------------|
| `abstracted-code-review.md` | `src/bernstein/core/quality/review_pipeline/abstract_diff.py`, `src/bernstein/core/review_responder/pr_gen.py` | Diff abstraction class renamed, PR-generation signature change | `manual-prose` |
| `action-cache.md` | `src/bernstein/core/persistence/action_cache.py`, `src/bernstein/core/persistence/fingerprint.py`, `src/bernstein/cli/commands/cache_cmd.py` | Cache record schema change, fingerprint inputs change | `manual-prose` |
| `agent-mode-profiles.md` | `src/bernstein/core/routing/mode_profile.py`, `src/bernstein/core/agents/spawner_prompt.py` | New mode profile, profile selection input change | `manual-prose` |
| `artifact-lineage.md` | `src/bernstein/core/persistence/lineage.py`, `src/bernstein/cli/commands/lineage_cmd.py` | Lineage record schema change | `manual-prose` |
| `ast-aware-chunking.md` | `src/bernstein/core/quality/review_pipeline/ast_chunker.py`, `src/bernstein/core/knowledge/ast_symbol_graph.py` | Chunker public surface change | `manual-prose` |
| `best-of-n.md` | `src/bernstein/core/orchestration/best_of_n.py`, `src/bernstein/core/orchestration/tick_pipeline.py` | New routing input, tick-pipeline hook change | `manual-prose` |
| `feature-contract.md` | `src/bernstein/core/planning/feature_contract.py`, `src/bernstein/core/security/audit.py`, `src/bernstein/core/quality/janitor.py` | Feature-contract schema change, audit hook signature change | `manual-prose` |
| `fingerprint-memoization.md` | `src/bernstein/core/persistence/fingerprint.py` | Memoization key inputs change | `manual-prose` |
| `jsonl-memory-log.md` | `src/bernstein/core/memory/jsonl_log.py`, `src/bernstein/core/memory/sqlite_store.py`, `src/bernstein/cli/commands/memory_cmd.py` | Event schema change, store backend swap | `manual-prose` |
| `orchestrator-hardening.md` | `src/bernstein/core/orchestration/orchestrator.py`, `src/bernstein/core/orchestration/adaptive_parallelism.py`, `src/bernstein/core/orchestration/tick_budget.py`, `src/bernstein/core/cost/cost_tracker.py` | New hardening primitive, budget knob added | `manual-prose` |
| `phase-pipeline.md` | `src/bernstein/core/orchestration/phase_pipeline.py`, `src/bernstein/core/planning/plan_loader.py`, `src/bernstein/core/routing/router.py` | New phase, phase contract change | `manual-prose` |
| `sandbox-selector.md` | `src/bernstein/core/sandbox/selector.py`, `src/bernstein/core/sandbox/registry.py` | New sandbox backend registered, selector heuristic change | `manual-prose` |
| `scaffold.md` | `src/bernstein/cli/commands/scaffold_cmd.py`, `src/bernstein/cli/scaffold/templates.py` | New scaffold template, CLI flag change | `manual-prose` |
| `schema-validation-retry.md` | `src/bernstein/core/tasks/schema_retry.py` | Retry policy change, schema validator change | `manual-prose` |
| `spec-as-test.md` | `src/bernstein/core/planning/spec_assertions.py`, `src/bernstein/core/orchestration/drain.py`, `src/bernstein/cli/run_cmd.py` | Spec-assertion schema change, drain-hook signature change | `manual-prose` |
| `swarm-migration.md` | `src/bernstein/core/tasks/swarm_migration.py`, `src/bernstein/cli/commands/migrate_cmd.py` | Migration entry point change, CLI flag change | `manual-prose` |
| `task-budgets.md` | `src/bernstein/core/cost/budget_countdown.py` | Budget-format function renamed | `manual-prose` |
| `team-hub.md` | `src/bernstein/core/plugins_core/team_hub_loader.py`, `src/bernstein/core/plugins_core/team_hub_manifest.py` | Manifest schema change | `manual-prose` |
| `wiki-build.md` | `src/bernstein/cli/commands/wiki_cmd.py`, `src/bernstein/core/knowledge/ast_symbol_graph.py`, `src/bernstein/core/knowledge/wiki_renderer.py` | Wiki-build CLI flag change, renderer output schema change | `manual-prose` |

### `docs/gui/`

Source of truth for this group is `src/bernstein/gui/` (FastAPI + SPA), `web/`
(Vite + React + Tailwind), and the GUI mount logic.

| Doc | Source of truth | Drift signal | Remediation |
|-----|-----------------|--------------|-------------|
| `index.md` | `src/bernstein/gui/__init__.py`, `src/bernstein/gui/cli.py`, `web/` | New tab added to the SPA, `gui-meta` route change | `manual-prose` |
| `install.md` | `src/bernstein/gui/cli.py`, `pyproject.toml` `[project.optional-dependencies]` (`gui` extra) | New runtime dep in `gui` extra, install-time check change | `manual-prose` |
| `configuration.md` | `src/bernstein/core/security/auth_middleware.py`, `src/bernstein/cli/run_bootstrap.py`, `src/bernstein/adapters/mock.py`, `src/bernstein/core/fleet/` | Auth source change, idle-mode change, fleet wiring change | `manual-prose` |
| `screens.md` | `web/src/` SPA component tree | New per-task drawer tab, new top-level tab | `manual-prose` |
| `playground.md` | `src/bernstein/adapters/mock.py`, `src/bernstein/cli/run_bootstrap.py` (`--idle`) | Mock-idle option change | `manual-prose` |
| `troubleshooting.md` | `src/bernstein/gui/cli.py` (`_check_gui_extras`), static-assets gating | Error-message change in the extras check | `manual-prose` |
| `mobile.md` | `web/` responsive breakpoints | Layout breakpoint change | `manual-prose` |

### `docs/sdd/`

| Doc | Source of truth | Drift signal | Remediation |
|-----|-----------------|--------------|-------------|
| `ticket_schema.md` | `.sdd/backlog/` ticket files; ticket consumers under `src/bernstein/core/planning/` | New required ticket field, label taxonomy change | `manual-prose` |

### Benchmarks

| Doc | Source of truth | Drift signal | Remediation |
|-----|-----------------|--------------|-------------|
| `docs/benchmarks/BENCHMARKS.md` | `src/bernstein/benchmark/`, `scripts/generate_benchmark_docs.py`, simulation harness inputs | New benchmark added, methodology change | `gen-benchmarks` (regenerate) or `manual-prose` |

## Data-freshness drift (time-stamped metrics)

Some docs include time-stamped factual metrics (stars, downloads, dates) that
grow stale even when the code does not change. These lines look like
`as of YYYY-MM-DD: ...` or `(YYYY-MM-DD)` in a table heading. The drift gate
here is purely temporal: the underlying numbers were correct on the recorded
date and are expected to drift between scheduled refreshes.

To enumerate the known time-stamped lines, run:

```bash
rg -n 'as of 20\d\d-\d\d-\d\d|\(20\d\d-\d\d-\d\d\)' README.md docs/
```

The current inventory is:

| File | Stale-prone substring shape | Data source | Refresh command |
|------|-----------------------------|-------------|-----------------|
| `README.md` (intro line) | `as of YYYY-MM-DD: N stars, N forks, ~N pypi downloads/day (~Nk/month)` | GitHub API, PyPI | `gh api repos/sipyourdrink-ltd/bernstein --jq '{stargazers_count, forks_count}'` and `curl -sS https://pypistats.org/api/packages/bernstein/recent \| jq .data` |
| `README.md` (regulatory anchors) | `### regulatory anchors (as of YYYY-MM-DD)` | Regulator publications | Manual review of cited regulations |

### Refresh commands

For the README intro stats line:

```bash
gh api repos/sipyourdrink-ltd/bernstein --jq '{stargazers_count, forks_count}'
curl -sS https://pypistats.org/api/packages/bernstein/recent | jq .data
```

### Staleness policy

- An `as of YYYY-MM-DD` marker older than 30 days is considered stale and
  emits a soft warning from `scripts/check_data_freshness.py`.
- A marker older than 60 days is a hard fail only on the weekly scheduled run
  of the `docs-data-freshness` job. Instead of failing pushes to `main`, the
  scheduled run opens or updates a single marker-tagged tracking issue so a
  stale marker never reddens unrelated main pushes.
- Pushes to `main` still run the checker in soft (non-strict) mode for the
  inline warning, but never fail on staleness.
- The `docs-data-freshness` check is advisory and is not part of the canary
  list of required checks.

## Cross-repo

The public website and several long-form pages live in
`sipyourdrink-ltd/bernstein-landing`.
These pages mirror or extend the bernstein docs; when bernstein docs change,
the landing copy may need a follow-up edit.

| Landing page | Mirrors / extends |
|--------------|-------------------|
| `app/cli-quickstart/page.tsx` | `README.md` install + first-run, `docs/getting-started/install.md`, `docs/getting-started/first-run.md` |
| `app/docs/cli/page.tsx` | `README.md` operator-commands table, `docs/reference/cli/` |
| `app/why-bernstein/page.tsx` | `README.md` "at a glance" + capabilities, `docs/whats-new.md` |
| `app/compare/` | `docs/compare/` comparison memos |
| Blog (`app/blog/`) | Long-form essays that may cite `docs/concepts/` or `docs/architecture/` |

The CI drift gate dispatches a `docs-mirror-sync` workflow in bernstein-landing
on every push to `main` (only if the `LANDING_REPO_PAT` secret is set). The
landing-side workflow is responsible for opening its own follow-up PR with the
mirrored content.

## Adding a new doc

When you add a new file under `docs/` (or a new root-level doc):

1. Add a row to the appropriate table above.
2. Set the source-of-truth column to a concrete module path under `src/`, or
   to `static` if there is no code source.
3. Update `scripts/check_docs_drift.py` only if the new doc needs a custom
   check beyond "the named source-of-truth file still exists." The default
   path-existence check covers the common case.
4. If the new doc is operator-facing, link it from `docs/index.md`.

## Adding a new code module

When you add a new top-level package under `src/bernstein/` or a new module
that becomes a source of truth for a doc:

1. Run `uv run bernstein agents-md sync` so the canonical AGENTS.md / CLAUDE.md
   / CONVENTIONS.md / `.goosehints` / `.cursor/rules/*.mdc` pick up the new
   module entry.
2. Run `uv run bernstein agents-md verify` to confirm all five outputs agree.
3. If the new module has a concept-doc-worthy public surface, add a file under
   `docs/concepts/` and a corresponding row in this playbook.
