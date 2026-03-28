# 649 — Browser-Use Agents

**Role:** backend
**Priority:** 5 (low)
**Scope:** large
**Depends on:** #632

## Problem

Bernstein cannot orchestrate browser-based testing or visual verification workflows. Anthropic shipped Computer Use Agent in March 2026, and browser automation is adjacent to coding workflows. Without browser-use support, Bernstein cannot handle end-to-end feature development that includes visual testing.

## Design

Add browser-use agent support for visual testing workflows. Integrate with browser automation frameworks (Playwright, Puppeteer) via a dedicated browser-use adapter. The adapter manages a headless browser instance, executes navigation and interaction commands, captures screenshots for visual verification, and reports results. Use cases: verify UI changes after code modifications, run visual regression tests, interact with web-based tools during orchestration. The browser agent receives a task like "verify the login page looks correct after the CSS changes" and uses Computer Use capabilities to complete it. Support both headless mode (CI) and headed mode (local development). Screenshots captured during browser-use sessions are stored in `.sdd/runs/{run_id}/screenshots/`. Requires sandboxing (#632) for safe browser execution.

## Files to modify

- `src/bernstein/adapters/browser.py` (new)
- `src/bernstein/core/browser_session.py` (new)
- `src/bernstein/core/spawner.py`
- `templates/roles/browser-tester.md` (new)
- `tests/unit/test_browser_adapter.py` (new)

## Completion signal

- Browser-use agent can navigate to a URL and capture a screenshot
- Visual verification tasks complete with pass/fail results
- Screenshots stored in run directory for review
