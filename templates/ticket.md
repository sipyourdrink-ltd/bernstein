---
# ── Bernstein Ticket Format v1 ──
# YAML frontmatter is parsed by the orchestrator for routing and verification.
# Markdown body is passed to the implementing agent as context.

id: "XXXX"                    # Unique ticket ID (e.g. R01, W6-03, F201)
title: "Short descriptive title"
status: open                  # open | claimed | in_progress | done | closed | blocked
type: feature                 # feature | bugfix | refactor | test | docs | security | research
priority: 2                   # 1=critical, 2=normal, 3=nice-to-have
scope: medium                 # small (<30min) | medium (30-90min) | large (90min+)
complexity: medium             # low | medium | high
role: backend                 # backend | qa | security | frontend | docs | architect | manager
model: auto                   # auto | opus | sonnet | haiku | codex | gemini — which model to use
effort: normal                # low | normal | high | max — model effort/reasoning level
estimated_minutes: 45          # Estimated time for agent to complete

# ── Routing & Dependencies ──
depends_on: []                 # List of ticket IDs that must complete first
blocks: []                     # List of ticket IDs blocked by this ticket
tags: []                       # Freeform tags for filtering: [dx, security, enterprise, adoption]

# ── Janitor Signals (machine-checkable completion criteria) ──
janitor_signals:
  - type: path_exists          # path_exists | file_contains | glob_exists | test_passes
    value: "src/bernstein/path/to/new_file.py"
  - type: test_passes
    value: "uv run pytest tests/unit/test_relevant.py -x -q"

# ── Agent Routing Hints ──
context_files: []              # Files agent MUST read before starting: ["src/foo.py", "docs/bar.md"]
affected_paths: []             # Files likely to be modified (for file locking)
max_tokens: null               # Token budget override (null = use default for scope)
require_review: false          # true = must be reviewed by different model before merge
require_human_approval: false  # true = human must approve before merge (high-risk changes)
---

## Summary

One short paragraph describing WHY this ticket exists and what problem it solves.

## Objective & Definition of Done

Clear statement of what "done" looks like.

- [ ] Observable completion condition 1
- [ ] Observable completion condition 2
- [ ] Observable completion condition 3

## Steps

1. First concrete implementation step
2. Second concrete implementation step
3. Third concrete implementation step

## Affected Paths

- `src/bernstein/path/to/file.py`
- `src/bernstein/path/to/other.py` (new)
- `tests/unit/test_relevant.py`

## Tests & Verification

- `uv run pytest tests/unit/test_relevant.py -x -q`
- `uv run ruff check src/bernstein/path/`
- Manual verification steps if any

## Risks & Edge Cases

- Risk 1: what could go wrong and how to mitigate
- Edge case: unusual input or state that needs handling

## Agent Notes

<!-- Reserved for the implementing agent: progress updates, blockers, commands run -->
