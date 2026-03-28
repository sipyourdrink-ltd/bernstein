# 709 — Competitor Comparison Pages

**Role:** docs
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** #701

## Problem

When developers search "bernstein vs conductor" or "multi-agent orchestration comparison", nothing comes up. Every successful dev tool has SEO-optimized comparison pages. These pages capture high-intent search traffic and convert it to users.

## Design

Create comparison pages for each major competitor:

### Pages to create
- `docs/compare/bernstein-vs-conductor.md`
- `docs/compare/bernstein-vs-dorothy.md`
- `docs/compare/bernstein-vs-parallel-code.md`
- `docs/compare/bernstein-vs-crystal.md`
- `docs/compare/bernstein-vs-stoneforge.md`
- `docs/compare/bernstein-vs-single-agent.md` (the most important one)
- `docs/compare/README.md` (comparison matrix of all)

### Each page includes
- Feature comparison table (honest — show where they win too)
- Architecture comparison (desktop app vs CLI vs MCP server)
- Cost comparison (if applicable)
- "When to use X instead" section (builds trust)
- Benchmark data where available

### Tone
Respectful, factual, engineer-to-engineer. Never disparage competitors. Let the features speak.

## Files to modify

- `docs/compare/` (new directory with 7+ comparison pages)
- `README.md` (link to comparison page)

## Completion signal

- At least 5 comparison pages exist
- Each has a feature matrix, honest pros/cons
- Linked from README
