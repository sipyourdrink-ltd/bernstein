# 714 — Contributing Guide + Good First Issues

**Role:** docs
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

No CONTRIBUTING.md means no contributors. The adapter pattern is perfect for "good first issue" contributions — each adapter is self-contained, well-defined, and doesn't require understanding the whole system. We need to make contribution frictionless.

## Design

### CONTRIBUTING.md
- How to set up dev environment (uv, ruff, pyright)
- How to run tests (`uv run python scripts/run_tests.py -x`)
- How to add a new adapter (step-by-step template)
- How to add a new CI parser (template)
- Code standards (already in CLAUDE.md, link to it)
- PR process

### Good First Issues on GitHub
Create 10+ issues labeled "good first issue":
- Add Aider adapter (#704)
- Add Cursor adapter (#705)
- Add OpenCode adapter
- Add Goose adapter
- Add Cline adapter
- Add GitLab CI parser
- Add CircleCI parser
- Add Jenkins log parser
- Add notification formatter for Microsoft Teams
- Add notification formatter for email

### Templates
- `templates/adapters/TEMPLATE.py` — skeleton adapter
- `templates/ci-parsers/TEMPLATE.py` — skeleton CI parser

## Files to modify

- `CONTRIBUTING.md` (new)
- `.github/ISSUE_TEMPLATE/` (bug, feature, adapter)
- `templates/adapters/TEMPLATE.py` (new)

## Completion signal

- CONTRIBUTING.md exists and is comprehensive
- 10+ good first issues created on GitHub
- Adapter template makes writing a new adapter < 30 min
