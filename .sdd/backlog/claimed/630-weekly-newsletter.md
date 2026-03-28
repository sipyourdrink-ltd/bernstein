# 630 — Weekly Newsletter

**Role:** docs
**Priority:** 3 (medium)
**Scope:** small
**Depends on:** #609

## Problem

There is no regular content output building audience and authority in the agent orchestration space. Consistent content compounds over time. Without a newsletter or blog, there is no channel to nurture potential users between major releases.

## Design

Launch a weekly "Agent Orchestration Insights" blog/newsletter. Content mix: 40% Bernstein development updates and technical deep-dives, 30% agent orchestration landscape analysis (new tools, benchmarks, trends), 30% practical patterns and lessons learned. Use a simple publishing stack: markdown files in the repo rendered via GitHub Pages or a lightweight static site generator. Include a subscription mechanism (email list via Buttondown or similar free tier). First 10 issues should be pre-planned with outlines. Each issue should be 500-1000 words, publishable in under 2 hours. Cross-post key articles to dev.to and Hashnode for broader reach. Positions Alex Chernysh as a domain expert before public launch.

## Files to modify

- `docs/newsletter/issue-template.md` (new)
- `docs/newsletter/issue-plan.md` (new — first 10 issues outlined)
- `docs/newsletter/README.md` (new — publishing process)

## Completion signal

- Newsletter template and publishing process documented
- First 10 issues outlined with titles and key points
- Publishing infrastructure chosen and documented
