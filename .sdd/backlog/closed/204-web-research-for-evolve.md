# Add web research capability for --evolve mode

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
In --evolve mode, Bernstein needs to research what features to build next. It should:
1. Search the web for competitor analysis, user needs, trending tools
2. Read GitHub repos, HN discussions, Reddit posts about agent orchestration
3. Generate informed improvement tickets based on real market data
4. Not just introspect its own code — look outward

## Implementation

### Tavily integration (direct, no MCP needed for agents)
Tavily API is already available (TAVILY_API_KEY in env). Add a research step to the evolve cycle:

1. Create `src/bernstein/core/researcher.py`:
   - `research_competitors()` — search for "agent orchestration", "multi-agent framework", "AI coding agent" etc.
   - `research_user_needs()` — search GitHub issues, HN "Ask", Reddit for pain points
   - `research_trending()` — what's new in AI dev tools
   - Uses Tavily search API (already have the SDK pattern in `llm.py`)

2. Wire into evolve cycle in orchestrator:
   - Before creating improvement tasks, run research
   - Feed research results into the manager agent's prompt as context
   - Manager creates tasks informed by real market data

### Research prompt for manager
When in evolve mode, the manager gets additional context:
```
## Market Research (auto-generated)
### Competitor landscape
{tavily results about competing tools}
### User pain points
{tavily results about what developers complain about}
### Trending features
{tavily results about new AI dev tool features}

Based on this research, create tasks that:
1. Build features no competitor has
2. Solve real pain points developers face
3. Adopt trending patterns early
```

### Rate limiting
- Max 10 Tavily searches per evolve cycle
- Cache results for 1 hour (don't re-search same queries)
- Store research results in .sdd/researches/auto/

## Files
- src/bernstein/core/researcher.py (new)
- src/bernstein/core/orchestrator.py — integrate research into evolve cycle
- src/bernstein/core/spawner.py — pass research context to manager prompt

## Acceptance criteria
- Tavily search works from the evolve loop
- Research results are cached and logged
- Manager agent receives market context when planning
- Rate limits prevent excessive API usage
- Tests mock Tavily calls


---
**completed**: 2026-03-28 01:34:18
**task_id**: dbb173861c03
**result**: Completed: Add web research capability for --evolve mode
