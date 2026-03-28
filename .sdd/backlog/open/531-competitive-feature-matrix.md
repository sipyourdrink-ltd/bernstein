# 531 — Auto-updated competitive feature matrix on docs site

**Role:** docs
**Priority:** 3 (medium)
**Scope:** small

## Problem

No clear way for potential users to see why Bernstein vs CrewAI vs AutoGen vs
Ruflo. A competitive feature matrix on the docs site would help adoption and
position Bernstein as a serious player.

## Design

### Feature matrix (auto-updated)
| Feature | Bernstein | CrewAI | AutoGen | Ruflo | LangGraph |
|---------|-----------|--------|---------|-------|-----------|
| CLI-native agents | Y | N | N | Y | N |
| Self-evolution | Y | N | N | Y* | N |
| File-based state | Y | N | N | N | N |
| Multi-model routing | Y | Y | Y | Y | Y |
| Distributed cluster | WIP | N | N | Y | N |
| Agent marketplace | Y | N | N | N | N |
| A2A protocol | Y | N | N | N | N |
| Cost optimization | Y | Y | N | Y | N |
| Eval harness | Y | N | N | N | N |

### Auto-update mechanism
- `bernstein evolve` scout agent periodically checks competitor repos
- Updates matrix data in `.sdd/research/competitors/matrix.json`
- Docs site renders from JSON (static site generation)
- CI job updates docs on push

### Positioning
- "The only orchestrator that improves itself"
- "File-based state = git-native = reproducible"
- "CLI-native = works with YOUR existing agent, not a locked-in SDK"

## Files to create
- `.sdd/research/competitors/matrix.json` — structured comparison data
- `docs/comparison.html` — rendered comparison page
- Update: `docs/index.html` — link to comparison

## Completion signal
- Comparison page live on docs site
- Matrix data is accurate and current
- At least 5 competitors compared
