# 535 — Zero lock-in showcase: model-agnostic as key differentiator

**Role:** docs
**Priority:** 2 (high)
**Scope:** small

## Problem

Every major AI lab now has its own agent framework (OpenAI Agents SDK, Google
ADK, Anthropic Agent SDK). All lock you into their models. Bernstein is the
only orchestrator that works with ANY CLI agent (Claude, Codex, Gemini, Qwen).
This is massively undermarketed.

Gartner says framework lock-in is a top concern. Enterprise teams want to switch
models without rewriting their orchestration.

## Design

### Demo: same task, 4 different agents
- Record a `bernstein run` session that:
  1. Assigns task A to Claude Code (Anthropic)
  2. Assigns task B to Codex CLI (OpenAI)
  3. Assigns task C to Gemini CLI (Google)
  4. Shows all three completing and merging
- This is the "money shot" for the README

### Adapter comparison page
- Docs page showing: which adapters exist, what each supports
- Feature matrix: Claude vs Codex vs Gemini vs Qwen adapters
- "Bring your own agent" guide: how to add a new adapter

### Cost arbitrage narrative
- "Use free-tier models for simple tasks, premium for complex"
- "Switch providers without changing your workflow"
- "Avoid vendor lock-in: your orchestration outlives any single model"

### README/docs updates
- Hero section: "Works with Claude, Codex, Gemini, Qwen — and any future CLI agent"
- Comparison callout vs Ruflo (Claude-only) and CrewAI (SDK-locked)

## Files to modify
- `README.md` — model-agnostic positioning
- `docs/index.html` — hero section
- New: `docs/adapters.html` — adapter comparison page
- New: `examples/multi-model/` — demo showing all 4 adapters

## Completion signal
- README prominently shows multi-model capability
- Demo script runs tasks on 3+ different agents
- Docs page compares adapters
