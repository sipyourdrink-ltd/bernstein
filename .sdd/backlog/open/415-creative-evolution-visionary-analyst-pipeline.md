# 415 — Creative evolution: visionary → analyst → production pipeline

**Role:** architect
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high
**Depends on:** [410]

## Problem

Self-evolve is boring. It rotates through `new_features → test_coverage → code_quality → performance → documentation` and produces incremental improvements: fix a test, add a docstring, tweak a timeout. After 13 cycles the backlog is empty because it ran out of ideas — not because the product is perfect.

The evolution loop lacks **creative vision**. It optimizes what exists but never imagines what SHOULD exist. It's a maintenance bot, not a product mind.

## Design: Three-Stage Creative Pipeline

### Stage 1: Visionary Agent (idea generation)

Spawn a specialized agent with a creative, product-thinking persona. Not a code monkey — a product visionary who thinks about:
- What would make developers LOVE this tool?
- What's the 10x feature nobody asked for?
- What's broken about the UX/DX that nobody noticed because they got used to it?
- What would a competitor build that makes Bernstein irrelevant?
- What's the "one more thing" moment?

**Implementation:**
- New role: `visionary` in `templates/roles/`
- System prompt: Think like a product visionary. You have deep technical knowledge but your job is to imagine, not implement. You challenge assumptions. You think from the USER's perspective, not the code's perspective.
- Input: current codebase state, recent metrics, user-facing features list, competitor landscape
- Output: 3-5 bold ideas as structured proposals with:
  - `title`: one-line pitch
  - `why`: user problem it solves
  - `what`: concrete feature description
  - `impact`: how it changes the user experience (not implementation details)
  - `risk`: what could go wrong
  - `effort_estimate`: S/M/L

### Stage 2: Analyst Agent (evaluation)

Spawn a different agent with an analytical, skeptical persona. Evaluates the visionary's proposals against:
- Technical feasibility (can we build this with current architecture?)
- ROI (effort vs impact — does this move the needle?)
- Risk assessment (does this break existing functionality? Security concerns?)
- User demand signal (do we have evidence users want this?)
- Dependency analysis (what must exist first?)

**Implementation:**
- New role: `analyst` in `templates/roles/`
- System prompt: You are a ruthless analytical mind. Your job is to kill bad ideas and strengthen good ones. You don't care about how cool something sounds — you care about whether it works, whether users need it, and whether the team can ship it. You score each proposal and provide a clear APPROVE / REVISE / REJECT verdict.
- Input: visionary's proposals + codebase state + metrics
- Output: scored evaluation for each proposal:
  - `verdict`: APPROVE / REVISE / REJECT
  - `feasibility_score`: 1-10
  - `impact_score`: 1-10
  - `risk_score`: 1-10 (higher = riskier)
  - `composite_score`: weighted combination
  - `reasoning`: 2-3 sentences
  - `revisions`: specific changes if REVISE
  - `decomposition`: if APPROVE, break into concrete tasks

### Stage 3: Production Gate

Only APPROVED proposals (composite_score >= 7) get converted to tickets:
- Analyst's `decomposition` becomes tasks in `.sdd/backlog/open/`
- Each task includes the visionary's context + analyst's evaluation
- Tasks enter the normal orchestrator flow (manager assigns, agents execute, janitor verifies)
- Track which proposals came from the creative pipeline in metrics

### Integration with Evolution Loop

Add a new focus area to the rotation: `creative_vision`
```
new_features → test_coverage → code_quality → performance → documentation → creative_vision
```

When `creative_vision` is the active focus:
1. Spawn visionary agent → collect proposals
2. Spawn analyst agent → evaluate proposals
3. Convert approved proposals to tasks
4. Next cycle picks them up for execution

### Beyond Self-Evolve: User-Triggered Mode

The same pipeline can run on demand:
```bash
bernstein ideate                    # run visionary → analyst pipeline once
bernstein ideate --persona cto     # use CTO persona instead of default visionary
bernstein ideate --dry-run         # show proposals without creating tasks
```

Persona library (configurable via templates/personas/):
- `visionary` (default): product thinker, user-obsessed
- `cto`: technical architecture, scalability, platform thinking
- `growth`: adoption, onboarding, community, developer experience
- `security`: threat modeling, compliance, hardening
- `contrarian`: deliberately challenges every assumption, finds blind spots

## Files
- templates/roles/visionary/system_prompt.md (new)
- templates/roles/analyst/system_prompt.md (new)
- templates/personas/ (new directory, optional persona overrides)
- src/bernstein/evolution/creative.py (new) — CreativePipeline class
- src/bernstein/evolution/loop.py — add creative_vision focus
- src/bernstein/cli/main.py — add `bernstein ideate` command
- tests/unit/test_creative_pipeline.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_creative_pipeline.py -x -q
- file_contains: src/bernstein/evolution/creative.py :: CreativePipeline
- file_contains: src/bernstein/evolution/creative.py :: VisionaryProposal
- file_contains: src/bernstein/evolution/creative.py :: AnalystVerdict
- path_exists: templates/roles/visionary/system_prompt.md
- path_exists: templates/roles/analyst/system_prompt.md
