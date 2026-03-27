# Fix test regressions from previous agent runs

**Role:** qa
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium

## Problem
Previous agent runs modified code but left some tests in an inconsistent state. The test suite passes (1050) but there may be tests that test the wrong thing or mock things that changed.

## Tasks
1. Run full test suite: `uv run pytest tests/ -x -q --tb=short`
2. Check for tests that mock non-existent attributes (e.g. test_janitor mocking `call_llm`)
3. Check for tests that assert stale values
4. Fix any broken tests
5. Ensure coverage doesn't decrease

## Files
- tests/unit/*.py — fix broken tests
- Any source files that tests reference

## Acceptance criteria
- All tests pass with no warnings about mocking non-existent attributes
- No tests skip or xfail without documented reason
