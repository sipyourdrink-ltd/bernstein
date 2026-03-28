# 522 — Enhanced evolve visionary: external signals + competitive analysis

**Role:** architect
**Priority:** 2 (high)
**Scope:** medium

## Problem

The creative pipeline (visionary -> analyst -> gate) generates ideas from
internal context only. It doesn't know what competitors are shipping, what the
community is asking for, or what's trending in the ecosystem. The visionary
agent operates in a vacuum.

Ruflo already claims self-learning from every task execution. CrewAI has 44K
stars and strong community. Bernstein needs to evolve TOWARDS market demand,
not just internal code quality.

## Design

### External signal sources (priority order)
1. **GitHub Issues/Discussions** on own repo — community requests
2. **Competitor releases** — CrewAI, AutoGen, LangGraph, Ruflo changelogs
3. **Hacker News / Reddit** — trending topics in AI agents
4. **Web research** (Tavily/Perplexity MCP) — "what do developers want from agent frameworks"
5. **npm/PyPI download trends** — what's gaining adoption

### Signal injection into visionary
- Before each creative cycle, run a "scout" agent that collects external signals
- Summarize into a "market brief" (max 2K tokens)
- Inject as additional context into visionary system prompt:
  - "Here's what competitors shipped this week: ..."
  - "Here's what the community is asking for: ..."
  - "Here's what's trending: ..."

### Visionary prompt additions
- "What feature would make someone switch FROM CrewAI TO Bernstein?"
- "What does the community want that nobody is building?"
- "What's the 'iPhone moment' for agent orchestration?"

### Competitive tracking
- `.sdd/research/competitors/` — auto-updated competitor feature matrices
- Weekly digest: what changed in the competitive landscape
- Feed into evolution priority scoring

## Files to modify
- `src/bernstein/evolution/creative.py` — signal injection
- `templates/roles/visionary/system_prompt.md` — market-aware prompting
- New: `src/bernstein/evolution/scout.py` — external signal collector
- `.sdd/config/evolve.yaml` — signal source configuration

## Completion signal
- Visionary proposals reference specific market gaps
- Scout agent runs before creative cycles
- At least one proposal per cycle addresses competitive positioning


---
**completed**: 2026-03-28 11:38:02
**task_id**: d7a3682d6ada
**result**: Completed: [RETRY 1] 520 — GitHub Issues as evolve coordination layer
