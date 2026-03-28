# 614 — Competitor Comparison Pages

**Role:** docs
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

There are no comparison pages positioning Bernstein against competitors. Developers searching for "CrewAI vs X" or "LangGraph alternative" won't find Bernstein. Comparison content drives search traffic and helps developers make informed decisions.

## Design

Write comparison pages for: Bernstein vs CrewAI, Bernstein vs LangGraph, Bernstein vs AWS CAO, and Bernstein vs Claude Code Teams. Each page follows a consistent structure: philosophy comparison, architecture comparison, feature matrix table, when to use each tool, and migration path. Be honest about where competitors are stronger — credibility matters more than salesmanship. Focus on Bernstein's differentiators: deterministic orchestration, file-based state, provider-agnostic design, cost governance. Keep the tone technical and factual, not marketing-speak. Host in `docs/comparisons/` and link from README.

## Files to modify

- `docs/comparisons/vs-crewai.md` (new)
- `docs/comparisons/vs-langgraph.md` (new)
- `docs/comparisons/vs-aws-cao.md` (new)
- `docs/comparisons/vs-claude-code-teams.md` (new)
- `README.md` (link to comparisons)

## Completion signal

- Four comparison pages exist with feature matrices and honest assessments
- Each page includes a "when to use X instead" section
- README links to the comparison pages
