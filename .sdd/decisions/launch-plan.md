# Decision: HN Show Launch Plan

**Date:** 2026-03-28
**Status:** Ready — pending pre-launch gates #700 and #701
**Ticket:** #710

---

## Context

Bernstein is ready for public launch. HN Show is the highest-ROI channel for developer tools with a technical audience. A well-executed Show HN can drive 500-2000 GitHub stars in 48 hours if the framing, demo, and repo are all solid.

## Launch channel

**Hacker News — Show HN**

- Primary audience: senior engineers, engineering leads, AI practitioners
- Secondary benefit: SEO, GitHub traffic, community adoption
- Expected outcome: 50-300 points if framing is right; 500-2000 stars in first 48 hours

## Pre-launch gates

All items must pass before submission:

| Gate | Status | Owner |
|------|--------|-------|
| README autoplay GIF demo | pending | #700 |
| `pipx install && bernstein init && bernstein -g "task"` works on fresh machine | pending | #701 |
| Benchmark data in README | done | benchmarks/ |
| Comparison pages exist | done | docs/compare/ |
| YouTube demo video linked | pending | — |
| GitHub topics set | pending | — |
| Repo description < 80 chars | pending | — |

## HN post

Draft: `docs/launch/hn-post.md`

Title: `Show HN: Bernstein – one command, multiple AI coding agents in parallel`

Key framing choices:
- Lead with what it does (parallel agents, one command), not what it is (orchestrator)
- Call out the deterministic orchestrator immediately — this is the technical differentiator that HN cares about
- Include honest benchmark numbers with a link to methodology
- End with human motivation ("tired of babysitting one agent at a time") — HN responds to authenticity

## Response strategy

- Answer every comment within 6 hours
- Be technical and honest; admit limitations
- Share benchmark methodology when asked (link to `benchmarks/README.md`)
- Link to `docs/the-bernstein-way.md` for architecture questions
- Do not oversell; let data speak

## Post timing

Tuesday or Wednesday, 09:00–11:00 ET. Avoid Mondays and Fridays.

## Success metrics (48 hours post)

| Metric | Target |
|--------|--------|
| HN points | ≥ 50 |
| GitHub stars | ≥ 200 |
| GitHub traffic unique visitors | ≥ 1,000 |
| pip installs | ≥ 100 |

## Dependencies

- #700 — GIF demo autoplay in README
- #701 — install path works end-to-end on fresh machine
