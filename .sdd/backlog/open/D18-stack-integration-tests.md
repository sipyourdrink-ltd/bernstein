# D18 — Integration Tests for Popular Stacks

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
Bernstein has no integration tests verifying it works end-to-end with real project stacks. Regressions in stack-specific behavior go undetected until users report them.

## Solution
- Create `tests/integration/` directory with test configs for 5 popular stacks:
  1. **FastAPI** — Init a FastAPI project, run Bernstein with goal "add a /health endpoint", verify the endpoint file is created.
  2. **Next.js** — Init a Next.js app, run Bernstein with goal "add an about page", verify the page component exists.
  3. **Django** — Init a Django project, run Bernstein with goal "add a user list view", verify the view and URL config.
  4. **Express** — Init an Express app, run Bernstein with goal "add request logging middleware", verify middleware file.
  5. **Flask** — Init a Flask app, run Bernstein with goal "add a /status endpoint", verify the route exists.
- Each test: scaffolds a minimal project in a temp directory, runs `bernstein run`, checks output files and exit code.
- Add a CI matrix job in GitHub Actions that runs all 5 integration tests.
- Use environment variable `BERNSTEIN_TEST_API_KEY` for provider auth in CI.

## Acceptance
- [ ] `tests/integration/` contains test files for all 5 stacks
- [ ] Each test creates a temp project, runs Bernstein, and asserts on output
- [ ] All 5 tests pass locally with a valid API key
- [ ] GitHub Actions workflow includes a matrix job running all integration tests
- [ ] Tests clean up temp directories after completion
