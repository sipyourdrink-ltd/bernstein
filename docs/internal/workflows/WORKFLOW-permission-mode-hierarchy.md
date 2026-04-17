# WORKFLOW: Permission Mode Hierarchy
**Version**: 1.0
**Date**: 2026-04-04
**Author**: Workflow Architect
**Status**: Approved
**Implements**: Task 6870e22b592f (originally fc1f0d64d725)

---

## Overview

The permission mode hierarchy controls which rule severities are enforced when
agents invoke tools during orchestrator runs.  Four modes (bypass, plan, auto,
default) form a strict ordering from most permissive to most restrictive.  The
orchestrator resolves the mode **once at startup** and applies it consistently
to every rule evaluation and approval gate for the lifetime of the run.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Operator | Selects the permission mode via CLI flag, config, or env var |
| Orchestrator | Resolves the mode at startup and stores it for the session |
| PermissionRuleEngine | Evaluates tool calls against rules, applying mode relaxation |
| PermissionResolutionMatrix | Resolves final outcome when hooks interact with rules |
| ApprovalGate | Uses mode to decide whether approval can be bypassed |
| Agent | Invokes tools; sees allow/ask/deny outcomes |

---

## Prerequisites

- `OrchestratorConfig.permission_mode` is set (or defaults to `None` -> DEFAULT).
- Permission rules are loaded from `.bernstein/rules.yaml` (optional; empty rules = no restrictions).

---

## Trigger

Orchestrator startup: `Orchestrator.__init__()` calls `resolve_mode(config.permission_mode)`.

---

## Single Source of Truth

**`src/bernstein/core/permission_mode.py`** is the canonical definition.

```python
class PermissionMode(StrEnum):
    BYPASS  = "bypass"   # Rank 0 — most permissive
    PLAN    = "plan"     # Rank 1
    AUTO    = "auto"     # Rank 2
    DEFAULT = "default"  # Rank 3 — most restrictive
```

---

## Legacy Flag Migration

| Legacy flag / config value | Canonical mode |
|---|---|
| `--dangerously-skip-permissions` | BYPASS |
| `dangerously_skip_permissions` | BYPASS |
| `--plan` / `plan_mode: true` | PLAN |
| `--auto` / (no flag, orchestrator) | AUTO |
| (interactive CLI, default) | DEFAULT |

`resolve_mode()` checks canonical values first, then legacy names, then falls
back to DEFAULT with a warning.

---

## Compatibility Matrix: Mode x Severity -> Enforced?

| Mode | critical | high | medium | low |
|---|---|---|---|---|
| **bypass** | enforced | relaxed | relaxed | relaxed |
| **plan** | enforced | enforced | relaxed | relaxed |
| **auto** | enforced | enforced | enforced | relaxed |
| **default** | enforced | enforced | enforced | enforced |

- **Enforced**: the rule's action (deny/ask) applies as-is.
- **Relaxed**: the rule's action is overridden to `allow`.
- **Critical rules are NEVER relaxed**, regardless of mode.

---

## Compatibility Matrix: Mode x Rule Action x Severity -> Effective Action

| Mode | Rule action | Severity | Effective action |
|---|---|---|---|
| bypass | deny | critical | **deny** |
| bypass | deny | high | allow |
| bypass | deny | medium | allow |
| bypass | deny | low | allow |
| bypass | ask | critical | **ask** |
| bypass | ask | high/medium/low | allow |
| plan | deny | critical | **deny** |
| plan | deny | high | **deny** |
| plan | deny | medium | allow |
| plan | deny | low | allow |
| plan | ask | high | **ask** |
| plan | ask | medium/low | allow |
| auto | deny | critical/high/medium | **deny** |
| auto | deny | low | allow |
| auto | ask | medium+ | **ask** |
| auto | ask | low | allow |
| default | deny | any | **deny** |
| default | ask | any | **ask** |
| any | allow | any | allow |

---

## Compatibility Matrix: Mode x Rule Action x Severity x Hook Outcome -> Final Outcome

This is the full resolution chain: raw rule -> mode relaxation -> hook resolution.

| Mode | Rule action | Severity | Hook outcome | Effective rule | Final outcome |
|---|---|---|---|---|---|
| default | deny | high | allow | deny | **DENY** |
| default | ask | medium | allow | ask | **ASK** |
| default | allow | low | deny | allow | **DENY** |
| default | allow | low | allow | allow | **ALLOW** |
| bypass | deny | high | deny | allow | **DENY** |
| bypass | deny | high | allow | allow | **ALLOW** |
| bypass | deny | critical | allow | deny | **DENY** |
| plan | ask | medium | neutral | allow | **ALLOW** |
| plan | ask | high | allow | ask | **ASK** |
| auto | ask | low | neutral | allow | **ALLOW** |
| auto | deny | medium | allow | deny | **DENY** |

### Resolution rules (from `PermissionResolutionMatrix`):

1. Effective rule = DENY -> **DENY** (hooks cannot override)
2. Effective rule = ASK -> **ASK** (hooks cannot bypass human approval)
3. Effective rule = ALLOW + hook = DENY -> **DENY** (hooks can restrict)
4. Effective rule = ALLOW + hook = ALLOW -> **ALLOW**
5. Effective rule = ALLOW + hook = NEUTRAL -> **ALLOW**
6. No rule + hook = DENY -> **DENY**
7. No rule + hook = ALLOW -> **ALLOW**
8. No rule + hook = NEUTRAL -> **ASK** (default to safety)

---

## Default for No-Match

When no rule matches a tool call:

| Mode | Default action |
|---|---|
| default | **ask** (conservative) |
| auto | allow |
| plan | allow |
| bypass | allow |

---

## Workflow Tree

### STEP 1: Resolve Permission Mode
**Actor**: Orchestrator (at startup)
**Action**: Call `resolve_mode(config.permission_mode)` to parse raw string into `PermissionMode` enum.
**Input**: `raw: str | None` from `OrchestratorConfig.permission_mode`
**Output on SUCCESS**: `PermissionMode` stored as `self._permission_mode`
**Output on FAILURE (unrecognised value)**: Log warning, fall back to DEFAULT.

### STEP 2: Agent Invokes Tool
**Actor**: Agent
**Action**: Agent calls a tool (e.g., `Bash`, `Write`, `Read`).
**Input**: `tool_name: str`, `tool_input: dict`

### STEP 3: Evaluate Permission Rules
**Actor**: PermissionRuleEngine
**Action**: `engine.evaluate(tool_name, tool_input, mode=self._permission_mode)`
- Iterate rules in declaration order; first match wins.
- If a rule matches, compute `effective_action(mode, rule.action, rule.severity)`.
- If no rule matches, return `RuleMatch(matched=False)`.
**Output on MATCH**: `RuleMatch(matched=True, action=<effective_action>)`
**Output on NO MATCH**: Apply `default_for_no_match(mode)`.

### STEP 4: Hook Resolution
**Actor**: PermissionResolutionMatrix
**Action**: `matrix.resolve(rule_outcome, hook_outcome)` using the 8-case precedence table.
**Output**: `ResolutionOutcome.ALLOW | ASK | DENY`

### STEP 5: Apply Outcome
- **ALLOW**: Tool executes.
- **ASK**: Escalate to human for approval (interactive only).
- **DENY**: Tool blocked; agent receives error.

### STEP 6: Approval Gate (task completion)
**Actor**: ApprovalGate
**Action**: When a task completes with `approval_required=True`:
- If `permission_mode == BYPASS`: `bypass_enabled=True` -> auto-approve.
- Otherwise: evaluate risk-based approval routing.

---

## State Transitions

```
[config parsed] -> resolve_mode() -> [mode stored on orchestrator]
[tool invoked] -> evaluate rules + mode relaxation -> [effective action]
[effective action] -> hook resolution -> [final outcome: ALLOW|ASK|DENY]
[task completed] -> approval gate + mode check -> [approved|blocked]
```

---

## Handoff Contracts

### OrchestratorConfig -> Orchestrator
**Field**: `permission_mode: str | None`
**Resolution**: `resolve_mode()` maps to `PermissionMode` enum.
**Fallback**: `None` -> `PermissionMode.DEFAULT`.

### Orchestrator -> PermissionRuleEngine
**Parameter**: `mode: PermissionMode` passed to `evaluate()`.
**Effect**: Relaxes rule actions based on the compatibility matrix.

### Orchestrator -> ApprovalGate
**Parameter**: `bypass_enabled: bool` (True when mode == BYPASS).
**Effect**: Skips approval workflow for headless/CI runs.

---

## Test Cases

All tests live in `tests/unit/test_permission_mode.py` (62 tests).

| Test | Class | What it covers |
|---|---|---|
| TC-01: Enum values | TestPermissionModeEnum | All 4 modes exist with correct string values |
| TC-02: Rank ordering | TestPermissionModeEnum | BYPASS < PLAN < AUTO < DEFAULT |
| TC-03: Full matrix | TestCompatibilityMatrix | 16 parametrized cases (4 modes x 4 severities) |
| TC-04: Critical always enforced | TestCompatibilityMatrix | Critical is True in every mode |
| TC-05: Matrix completeness | TestCompatibilityMatrix | Every mode x severity pair is defined |
| TC-06: Effective action | TestEffectiveAction | Enforced deny/ask pass through; relaxed become allow |
| TC-07: Critical never relaxed | TestEffectiveAction | Critical deny stays deny in every mode |
| TC-08: Default for no match | TestDefaultForNoMatch | DEFAULT -> ask; others -> allow |
| TC-09: Canonical parsing | TestResolveMode | "bypass"/"plan"/"auto"/"default" |
| TC-10: Legacy flags | TestResolveMode | "dangerously-skip-permissions" -> BYPASS etc. |
| TC-11: Engine + mode | TestEngineWithMode | Rule engine applies mode relaxation to tool calls |
| TC-12: YAML severity loading | TestYamlSeverityLoading | severity field parsed from rules.yaml |
| TC-13: Config wiring | TestOrchestratorConfigPermissionMode | OrchestratorConfig carries permission_mode |
| TC-14: Hook x Mode matrix | TestHookModeCompatibility | 11 parametrized cases for full chain |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Permission mode is resolved once at startup and does not change mid-run | orchestrator.py:270 | Rules evaluated under wrong mode |
| A2 | Rules are loaded once from .bernstein/rules.yaml and not hot-reloaded | permission_rules.py:294 | Rule changes require restart |
| A3 | Hook outcomes are independent of permission mode | permission_matrix.py | Low — hooks are a separate layer |

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-04 | Initial spec created from implemented code | All code paths verified against tests |
| 2026-04-04 | 62 tests pass covering all matrix cells | No gaps found |
