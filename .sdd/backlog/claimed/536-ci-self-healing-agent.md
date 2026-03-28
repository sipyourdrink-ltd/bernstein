# 536 — CI self-healing: auto-detect and fix failing CI on push

**Role:** qa
**Priority:** 1 (critical)
**Scope:** medium

## Problem

CI has been failing for multiple commits (templates/prompts/ in .gitignore but
tests reference them; ruff not in dependencies). Nobody noticed because there's
no mechanism to detect and fix CI failures automatically. An evolve-driven
project must keep CI green at all times.

## Current failures (as of 2026-03-28)
1. `FileNotFoundError: templates/prompts/judge.md` — file is gitignored but
   tests import it. Need to either un-gitignore templates/prompts/ or mock
   template loading in tests.
2. `ruff` not found — not in project dependencies. Need to add ruff as dev
   dependency in pyproject.toml.

## Design

### Immediate fix
- Add `ruff` to `[dependency-groups]` dev in pyproject.toml
- Either un-gitignore `templates/prompts/` or create test fixtures
- Verify CI passes

### Self-healing mechanism
- Post-push hook or CI job: if tests fail, create a `ci-fix` task
- Evolve agent picks up ci-fix tasks with P0 priority
- Agent reads CI logs, diagnoses failure, creates fix PR
- `bernstein run --ci-fix` mode: only fix CI failures, nothing else

### Prevention
- Pre-commit hook: `uv run pytest tests/ -x -q` before push
- `bernstein doctor` checks: are all test dependencies available?
- CI status badge in README reflects real state

## Files to modify
- `pyproject.toml` — add ruff dependency
- `.gitignore` — reconsider templates/prompts/ exclusion
- `.github/workflows/ci.yml` — add self-healing step
- New: `src/bernstein/core/ci_fix.py` — CI log parser + fix agent

## Completion signal
- CI is green on master
- Self-healing mechanism demonstrated on at least one real failure
