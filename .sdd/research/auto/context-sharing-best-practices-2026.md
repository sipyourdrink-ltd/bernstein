# Context Sharing Best Practices for Multi-Agent Systems (2025-2026)

Research date: 2026-03-28

## How Major Frameworks Handle Context

### CrewAI
- 4-tier memory: short-term (ChromaDB/RAG), long-term (SQLite3), entity memory, contextual memory
- After each task, extracts discrete facts and stores them; before each task, recalls relevant context
- LLM-analyzed storage with composite scoring (semantic similarity + recency + importance)

### AutoGen
- Conversational: context passes through message thread as shared state
- External memory via Mem0/Zep for cross-session persistence
- save_state/load_state serializes full team state

### LangGraph
- Centralized TypedDict/Pydantic state object flows through every node
- Built-in checkpointing (MemorySaver, SqliteSaver, PostgresSaver)
- Reducer-driven schemas prevent data loss in concurrent updates

### MetaGPT
- Shared Memory Pool + Pub/Sub: agents subscribe to updates from other agents
- SOP-driven: roles pass structured artifacts (user stories, APIs, data structures), not raw conversation
- Closest to Bernstein's philosophy

### OpenHands (formerly OpenDevin)
- Event stream architecture: all interactions as typed events through central hub
- Immutable agents + single mutable conversation state
- Deterministic replay via event-sourced state

### Aider (single-agent, but gold standard for repo context)
- **Repository Map**: tree-sitter AST extraction of class/function signatures + call relationships
- Graph-ranking selects most relevant portions within token budget (default 1k tokens)
- Single highest-ROI technique for reducing orientation overhead

### Devin
- Preliminary plan pattern: responds in seconds with relevant files + plan before execution
- Devin Search: agentic tool for codebase Q&A with citations

### Google ADK
- Context as "compiled view" over structured state (not mutable string buffer)
- Explicit multi-agent context scoping: pass only the right slice to each callee

## Common Patterns

1. **Layered memory hierarchy**: short-term / long-term / entity-domain (universal)
2. **Structured artifacts over conversation**: typed state, event streams, file-based state
3. **Selective injection, not stuffing**: retrieval/ranking to inject only relevant context
4. **Context scoping for sub-agents**: pass minimal necessary context, not full history
5. **Structured briefing documents**: CLAUDE.md, AGENTS.md correlated with 29% runtime reduction
6. **Rolling summarization**: incremental merge > full regeneration

## Best Practices for Bernstein

### A. Three-Layer Context Architecture
- **Hot** (~15% budget): project conventions, role prompt, task with acceptance criteria
- **Warm** (~40% budget): relevant file summaries, dependency info, predecessor task outputs
- **Cold** (~35% reserved): full files, specs — agent pulls via tools only when needed

### B. Repository Map (Aider-style)
Pre-computed, token-budgeted codebase summary: key classes, functions, module boundaries, dependency graph. Inject into every fresh agent. **Highest-ROI investment.**

### C. Task Context Packets
Structured packet per task: ID, description, acceptance criteria, predecessor outputs (summarized), files to touch, relevant knowledge base excerpts, role instructions.

### D. Rolling Summaries for Cross-Task Knowledge
On task completion extract: what was done, what was learned, what failed and why. Store in .sdd/. Inject only relevant summaries into next agent.

### E. Context Budget Accounting
Treat context tokens as finite resource. Optimize for tokens-per-task, not tokens-per-request.

### F. Explicit Context Scoping
Suppress ancestral history. Re-cast prior outputs as narrative context. Mark tool calls from other agents.

## Anti-Patterns to Avoid

1. **Context Stuffing**: dumping everything into prompt degrades performance
2. **Context Explosion in chains**: full history → sub-agent → sub-sub-agent = exponential growth
3. **Monolithic manifest**: single CLAUDE.md doesn't scale beyond modest codebases
4. **Vague task descriptions**: leads to misinterpretation and duplicate work
5. **Full-context regeneration**: loses information vs incremental merging
6. **Treating windows as unlimited**: effective window << advertised maximum
7. **Amnesia tax**: zero knowledge of predecessors = wasted re-exploration tokens
8. **Conversation-as-state**: raw history doesn't scale; structured artifacts do

## Key Papers

- "Codified Context" (arXiv 2602.20478) — AGENTS.md = 29% runtime reduction across 283 sessions
- Factory AI "Context Window Problem" + "Compressing Context" — rolling summaries across 36k sessions
- "Token-Budget-Aware LLM Reasoning" (ACL 2025) — minimize output tokens while maintaining accuracy
- Chroma "Context Rot" — models degrade well before advertised context limits
