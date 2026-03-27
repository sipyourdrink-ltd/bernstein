# Implement risk-stratified ApprovalGate

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** high

## Problem
Not all self-modifications carry equal risk. Research consensus on confidence thresholds:
- >=95% confidence on reversible changes: auto-approve
- 85-95%: auto-approve with async audit
- 70-85%: human review within 4h
- <70%: immediate human review
Target: 10-15% escalation rate to humans

## Risk classification
Each UpgradeProposal must be classified:
- L0_CONFIG: targets .sdd/config.yaml, routing rules, batch sizes → auto-apply after schema check
- L1_TEMPLATE: targets templates/roles/*.md, prompt text → sandbox A/B + auto-apply if metrics improve
- L2_LOGIC: targets task routing params, orchestrator config → git worktree + tests + PR + human
- L3_STRUCTURAL: targets .py files, data models → BLOCKED, human only

## Implementation
- RiskClassifier: examines proposal target files to assign risk level
- ApprovalGate: routes proposals through appropriate pipeline based on risk level
- CLI command: `bernstein evolve review` — shows pending proposals for human review
- CLI command: `bernstein evolve approve <id>` — approves a proposal
- All decisions logged to .sdd/evolution/decisions.jsonl

## Files
- src/bernstein/evolution/gate.py (new)
- src/bernstein/cli/main.py (add evolve subcommands)
- tests/unit/test_approval_gate.py (new)

## Completion signals
- path_exists: src/bernstein/evolution/gate.py
- test_passes: uv run pytest tests/unit/test_approval_gate.py -x -q
- file_contains: src/bernstein/evolution/gate.py :: RiskLevel
