# 502 — CI/CD pipeline: GitHub Actions, PyPI publishing, Dependabot

**Role:** devops
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** low

## Problem
Zero CI. No tests run on push/PR. No automated releases. No dependency security scanning. Every commit to master is untested. PyPI package doesn't exist.

## Implementation

### Phase 1: 3 files (immediate)

**`.github/workflows/ci.yml`** — runs on push to master + all PRs:
- Job 1: `ruff check src/` + `ruff format --check src/`
- Job 2: `pyright` type checking
- Job 3: `pytest tests/ -x -q` on Python 3.12 + 3.13
- Uses `astral-sh/setup-uv@v7` with `enable-cache: true`
- All 3 jobs run in parallel (~60s each)

**`.github/workflows/publish.yml`** — runs on tag push `v*`:
- Job 1: `uv build` → upload artifact
- Job 2: publish to PyPI via trusted publishing (OIDC, no secrets stored)
- Sigstore attestations generated automatically
- Two separate jobs: build (runs code) and publish (only has id-token permission)

**`.github/dependabot.yml`** — weekly updates:
- pip ecosystem: grouped minor/patch, separate major
- github-actions ecosystem: weekly

### Phase 2: When users arrive
- Add lockfile verification (`uv lock && git diff --exit-code uv.lock`)
- Add coverage reporting (`pytest --cov=src/bernstein`)

### What to skip (overkill for single-maintainer)
- Cross-platform matrix (Linux CI + trust stdlib for Mac/Win)
- Bandit/security scanning (premature)
- CodeCov/Coveralls (use --cov locally)
- semantic-release (manual git tag is fine)
- Renovate (Dependabot sufficient)

## Files
- .github/workflows/ci.yml (new)
- .github/workflows/publish.yml (new)
- .github/dependabot.yml (new)

## Completion signals
- path_exists: .github/workflows/ci.yml
- path_exists: .github/workflows/publish.yml
- path_exists: .github/dependabot.yml
