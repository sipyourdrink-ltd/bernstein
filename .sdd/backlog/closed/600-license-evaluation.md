# 600 — License Evaluation

**Role:** architect
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

The current PolyForm NonCommercial license blocks enterprise adoption and discourages community contributions. Every major competitor in the multi-agent orchestration space uses permissive licenses (MIT, Apache 2.0). This licensing choice is the single biggest barrier to growth.

## Design

Evaluate three candidate licenses: MIT, Apache 2.0, and AGPL v3. For each, assess impact on enterprise adoption, community contribution rates, and competitive positioning. Research how CrewAI (MIT), LangGraph (MIT), and AutoGen (MIT) benefited from permissive licensing. Produce a decision document with a recommendation, adoption projections, and a migration plan. Include analysis of whether dual licensing (open core + commercial) is viable. The decision doc should be concise and actionable, not academic.

## Files to modify

- `LICENSE`
- `pyproject.toml` (license field)
- `.sdd/decisions/` (new decision doc)

## Completion signal

- Decision document exists with clear recommendation and rationale
- If approved, LICENSE file updated and pyproject.toml reflects new license
