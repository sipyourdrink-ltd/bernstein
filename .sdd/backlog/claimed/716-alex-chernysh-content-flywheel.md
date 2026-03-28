# 716 — Technical Content Strategy

**Role:** docs
**Priority:** 0 (urgent)
**Scope:** small
**Depends on:** none

## Problem

Successful open-source tools are built through consistent technical content. LangChain, CrewAI, and Aider all grew through founder-led technical writing and community engagement. Bernstein has no content strategy — no blog posts, no architecture walkthroughs, no benchmark publications.

## Design

### Content pillars
1. **"Building in public"** — tweet every feature, every benchmark, every decision
2. **Technical deep-dives** — blog posts on orchestration patterns (3x/month)
3. **Comparisons** — honest takes on Conductor vs Bernstein vs Crystal
4. **Benchmarks** — publish results, methodology, raw data
5. **Hot takes** — opinions on where AI coding is going

### Immediate content (week 1)
1. Twitter thread: "I built a multi-agent orchestrator. Here's what 3 agents can do in 47 seconds that takes one agent 3 minutes."
2. Twitter thread: "Why I chose deterministic orchestration over LLM-based scheduling"
3. Blog post: "The Bernstein Architecture: Zero LLM tokens on coordination"
4. Record demo video and post everywhere

### Platforms
- Twitter/X (primary — where AI devs live)
- YouTube (demo videos, architecture walkthroughs)
- Reddit (r/LocalLLaMA, r/ClaudeAI, r/programming — one post each)
- Dev.to / Hashnode (SEO blog posts)
- HN (Show HN, then comment on relevant threads)

### Growth pipeline
Content → Community adoption → Contributors → Ecosystem growth

## Files to modify

- `docs/content-calendar.md` (if not exists, update)

## Completion signal

- 5 content pieces drafted
- Twitter account has first technical thread
- Blog has first architecture post
