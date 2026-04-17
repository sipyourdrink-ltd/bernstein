# WORKFLOW: Mandatory Self-Review Before PR
**Version**: 0.1
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Draft
**Implements**: 511b — Mandatory Self-Review Before PR

---

## Overview

Before any agent can open a PR or have its task marked DONE, the orchestrator
runs a mandatory review pass using a **different model** from the one that wrote
the code. The review checks five dimensions: vulnerabilities, style violations,
test coverage gaps, scope violations, and correctness bugs. A failing review
blocks completion and either creates a fix task or retries the original agent
with the reviewer's feedback.

This workflow enhances the existing `cross_model_verifier.py` infrastructure by:
1. Expanding review scope from 2 dimensions (correctness + security) to 5
2. Making the review mandatory (not opt-in)
3. Integrating scope-violation detection (currently in guardrails) into the review
4. Adding test-gap analysis to the review prompt
5. Providing structured, actionable feedback that can be routed back to the agent

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Writing Agent | Completes the task and signals completion |
| Orchestrator (`task_lifecycle.py`) | Runs the self-review gate in `process_completed_tasks()` |
| Cross-Model Reviewer (`cross_model_verifier.py`) | Enhanced — sends diff + context to a different model for 5-dimension review |
| Reviewer LLM | Different model family from the writer — performs the actual review |
| Quality Gates (`quality_gates.py`) | Runs lint/type/test checks before the review (existing, unchanged) |
| Guardrails (`janitor.py`) | Checks secrets/scope pre-review (existing, partially overlaps — see A3) |
| Approval Gate (`approval.py`) | Runs after review passes — creates PR or auto-merges |
| Task Store | Records review results in task history |

---

## Prerequisites

- Task has status `DONE` (agent signaled completion via `POST /tasks/{id}/complete`)
- Orchestrator tick has picked up the task in `process_completed_tasks()`
- Agent worktree is accessible (for `git diff`)
- LLM provider for reviewer model is reachable (OpenRouter or direct)
- `OrchestratorConfig.cross_model_verify.enabled = True` (must become default)

---

## Trigger

The self-review is triggered automatically by the orchestrator during
`process_completed_tasks()` — specifically after janitor verification and
quality gates pass, but **before** the approval gate evaluates.

This is NOT triggered by the agent itself. The agent has no way to skip or
bypass the review.

**Current insertion point in `task_lifecycle.py`**:
```
janitor verification → quality gates → [THIS WORKFLOW] → approval gate → merge/PR
```

---

## Workflow Tree

### STEP 1: Collect Review Context
**Actor**: Orchestrator (`process_completed_tasks` in `task_lifecycle.py`)
**Action**: Gather all inputs needed for the review:
  1. Get git diff from agent worktree (`git diff HEAD~1 -- <owned_files>`)
  2. Get task description, title, and completion signals from task object
  3. Get quality gate results (lint score, test pass/fail, type-check results)
  4. Get list of files changed vs. files in task scope (`task.owned_files`)
  5. Identify writer model from `session.model_config.model`
**Timeout**: 30s (git operations)
**Input**: `{ task: Task, session: AgentSession, worktree_path: Path }`
**Output on SUCCESS**: `ReviewContext { diff: str, task_description: str, owned_files: list[str], changed_files: list[str], writer_model: str, quality_gate_results: QualityGateCheckResult | None, test_output: str }` → GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(no_diff)`: Diff is empty or `"(no diff available)"` → SKIP review, log warning, proceed to approval gate. An empty diff means the agent changed nothing — review has nothing to evaluate.
  - `FAILURE(worktree_gone)`: Worktree path does not exist → SKIP review, log error, proceed to approval gate. Worktree may have been cleaned up by a race with `reap_dead_agents`.
  - `FAILURE(git_timeout)`: git diff took >30s → SKIP review, log warning, proceed to approval gate. Do not block the pipeline on a git operation.

**Observable states during this step**:
  - Customer sees: nothing (internal orchestrator operation)
  - Operator sees: task in `done` state, processing pipeline running
  - Database: `task.status = DONE`, no change yet
  - Logs: `[task_lifecycle] self_review: collecting context for task={task_id} writer={model}`

---

### STEP 2: Select Reviewer Model
**Actor**: Cross-Model Reviewer (`select_reviewer_model` in `cross_model_verifier.py`)
**Action**: Choose a reviewer model from a different model family than the writer.
  - Writer is Claude → Reviewer is Gemini Flash (cheap, fast)
  - Writer is Gemini → Reviewer is Claude Haiku (cheap, fast)
  - Writer is GPT/Codex → Reviewer is Gemini Flash
  - Writer is Qwen → Reviewer is Claude Haiku
  - Explicit override available via `CrossModelVerifierConfig.reviewer_model`
**Timeout**: N/A (pure logic, no IO)
**Input**: `{ writer_model: str, config_override: str | None }`
**Output on SUCCESS**: `reviewer_model: str` → GO TO STEP 3
**Output on FAILURE**: N/A — this step is deterministic and cannot fail. If writer model is unrecognized, falls back to `google/gemini-flash-1.5`.

**Observable states during this step**:
  - Logs: `[cross_model_verifier] selected reviewer={model} for writer={writer_model}`

---

### STEP 3: Build Enhanced Review Prompt
**Actor**: Cross-Model Reviewer (`cross_model_verifier.py` — enhanced `_build_prompt`)
**Action**: Construct a structured review prompt with the 5-dimension checklist:

```
You are a code reviewer. A different AI agent wrote the code below. Review it
for ALL of the following dimensions.

## Task
**Title:** {title}
**Description:** {description}
**Owned files (in-scope):** {owned_files}

## Diff
```diff
{diff}
```

## Quality Gate Results (pre-review)
{quality_gate_summary}

## Review Checklist — evaluate ALL dimensions

### 1. Vulnerabilities (CRITICAL)
- Injection flaws (SQL, command, path traversal)
- Hardcoded secrets, API keys, credentials
- Insecure defaults (open permissions, disabled auth)
- Missing input validation at system boundaries
- OWASP Top 10 violations

### 2. Correctness
- Does the diff accomplish what the task description asks?
- Off-by-one errors, missing error handling for likely failures
- Logic errors, unreachable code, dead branches
- Race conditions or concurrency issues

### 3. Style Violations
- Naming conventions inconsistent with surrounding code
- Overly complex logic that could be simplified
- Dead code, unused imports, commented-out code
- Missing type annotations on public interfaces (if language requires)

### 4. Test Coverage Gaps
- New code paths with no corresponding test
- Edge cases mentioned in the task but not tested
- Error handling paths with no test coverage
- If tests exist: do they actually assert meaningful behavior?

### 5. Scope Violations
- Files changed that are NOT in the owned_files list: {scope_violations}
- Changes unrelated to the task description
- Feature creep — functionality added beyond what was asked

## Output Format
Output a JSON object with exactly these fields:
{
  "verdict": "approve" | "request_changes",
  "dimensions": {
    "vulnerabilities": { "passed": bool, "issues": ["..."] },
    "correctness": { "passed": bool, "issues": ["..."] },
    "style": { "passed": bool, "issues": ["..."] },
    "test_gaps": { "passed": bool, "issues": ["..."] },
    "scope": { "passed": bool, "issues": ["..."] }
  },
  "blocking_issues": ["Only issues severe enough to block merge"],
  "suggestions": ["Non-blocking improvements"],
  "summary": "One-sentence overall assessment"
}
Output ONLY the JSON. No markdown fences. No extra text.
```

**Scope violation pre-detection**: Before sending to the reviewer, the orchestrator
compares `changed_files` (from `git diff --name-only`) against `task.owned_files`.
Any file changed that is NOT in `owned_files` is flagged as a scope violation and
injected into the prompt at `{scope_violations}`. This gives the reviewer concrete
evidence rather than asking it to detect scope issues from the diff alone.

**Diff truncation**: If diff exceeds `config.max_diff_chars` (default 12,000),
truncate with `"... (truncated)"` suffix. Log warning — large diffs are a code
smell and may indicate scope violation.

**Timeout**: N/A (string formatting, no IO)
**Input**: `ReviewContext` from STEP 1
**Output on SUCCESS**: `prompt: str` → GO TO STEP 4

**Observable states during this step**:
  - Logs: `[cross_model_verifier] self_review: prompt built, diff_chars={n}, scope_violations={n}`

---

### STEP 4: Call Reviewer LLM
**Actor**: Reviewer LLM (via `call_llm` in `llm.py`)
**Action**: Send the enhanced review prompt to the selected reviewer model via OpenRouter (or configured provider).
**Timeout**: 60s (reviewer response — configurable via `CrossModelVerifierConfig`)
**Input**: `{ prompt: str, model: str, provider: str, max_tokens: 1024, temperature: 0.0 }`
**Output on SUCCESS**: Raw JSON string from reviewer → GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(timeout)`: Reviewer took >60s → DEFAULT TO APPROVE. Log warning. Rationale: a reviewer outage must never block the pipeline permanently. The quality gates and guardrails already provide a safety net.
  - `FAILURE(llm_error)`: Provider returned HTTP 5xx or connection error → DEFAULT TO APPROVE. Log warning with error details.
  - `FAILURE(rate_limited)`: Provider returned HTTP 429 → DEFAULT TO APPROVE. Log warning. Do not retry — the cost of blocking the pipeline exceeds the value of one review.

**Observable states during this step**:
  - Operator sees: task still in `done` state, `self_review_pending` in processing log
  - Logs: `[cross_model_verifier] self_review: calling reviewer={model} for task={task_id}`
  - On failure: `[cross_model_verifier] self_review: LLM call failed for task={task_id}: {error} — defaulting to approve`

---

### STEP 5: Parse Review Response
**Actor**: Cross-Model Reviewer (`_parse_response` in `cross_model_verifier.py` — enhanced)
**Action**: Parse the reviewer's JSON response into an `EnhancedCrossModelVerdict`:

```python
@dataclass(frozen=True)
class DimensionResult:
    """Result for one review dimension."""
    passed: bool
    issues: list[str]

@dataclass(frozen=True)
class EnhancedCrossModelVerdict:
    """Result of the enhanced 5-dimension cross-model review."""
    verdict: Literal["approve", "request_changes"]
    dimensions: dict[str, DimensionResult]  # keys: vulnerabilities, correctness, style, test_gaps, scope
    blocking_issues: list[str]
    suggestions: list[str]
    summary: str
    reviewer_model: str
```

**Parsing rules**:
1. Strip markdown code fences if present
2. Find first `{` and last `}` — attempt JSON parse
3. Validate required fields exist
4. If `verdict` is missing or unrecognized → default to `"approve"`
5. If `dimensions` is missing → construct from legacy `issues` field (backward compat with existing reviewer output)
6. If entire response is unparseable → DEFAULT TO APPROVE with warning

**Timeout**: N/A (string parsing, no IO)
**Input**: `{ raw_response: str, reviewer_model: str }`
**Output on SUCCESS**: `EnhancedCrossModelVerdict` → GO TO STEP 6
**Output on FAILURE**:
  - `FAILURE(unparseable)`: Response is not valid JSON and no JSON object found → DEFAULT TO APPROVE. Log the first 200 chars of the response for debugging.

**Observable states during this step**:
  - Logs: `[cross_model_verifier] self_review: parsed verdict={verdict} blocking_issues={n} suggestions={n}`

---

### STEP 6: Evaluate Verdict
**Actor**: Orchestrator (`process_completed_tasks` in `task_lifecycle.py`)
**Action**: Decide whether the review blocks completion based on verdict and config.

**Decision tree**:
```
IF verdict == "approve":
    → PASS. Record review results in task history. GO TO approval gate.

IF verdict == "request_changes" AND config.block_on_issues == True:
    IF dimensions.vulnerabilities.passed == False:
        → BLOCK (CRITICAL). GO TO STEP 7a (create fix task with vulnerability findings).
    ELIF blocking_issues is not empty:
        → BLOCK. GO TO STEP 7a (create fix task with blocking issues).
    ELSE:
        → PASS WITH WARNINGS. Record suggestions. GO TO approval gate.
        (Style and test-gap issues alone do not block if no blocking_issues listed)

IF verdict == "request_changes" AND config.block_on_issues == False:
    → PASS WITH WARNINGS. Record all findings in task history. GO TO approval gate.
```

**Blocking severity hierarchy**:
1. **Vulnerabilities** — always block, regardless of `blocking_issues` field. Security is non-negotiable.
2. **Correctness** — block if listed in `blocking_issues`.
3. **Scope violations** — block if listed in `blocking_issues`.
4. **Style** — never block on its own. Logged as suggestions.
5. **Test gaps** — block only if listed in `blocking_issues` (reviewer judged them severe).

**Timeout**: N/A (logic only)
**Input**: `{ verdict: EnhancedCrossModelVerdict, config: CrossModelVerifierConfig }`
**Output on PASS**: Review recorded, task continues to approval gate
**Output on BLOCK**: → GO TO STEP 7a

**Observable states during this step**:
  - Database: Review results appended to `task.metadata["self_review"]`
  - Logs: `[task_lifecycle] self_review: task={task_id} verdict={verdict} blocked={bool} vuln={n} correctness={n} style={n} test_gaps={n} scope={n}`
  - Metrics: `self_review_verdict` event written to `.sdd/metrics/YYYY-MM-DD.jsonl` with dimensions breakdown

---

### STEP 7a: Block — Create Fix Task
**Actor**: Orchestrator (`task_lifecycle.py`)
**Action**: When review blocks completion:
  1. Remove task from `verified` list in TickResult
  2. Add to `verification_failures` with structured failure reasons
  3. Create a targeted fix task with the reviewer's findings injected as context:

```python
fix_task = {
    "title": f"Fix self-review findings: {original_task.title}",
    "description": (
        f"The self-review for task {original_task.id} found blocking issues.\n\n"
        f"## Blocking Issues\n"
        + "\n".join(f"- {issue}" for issue in verdict.blocking_issues)
        + "\n\n## Dimension Results\n"
        + dimension_summary
        + "\n\n## Original Task\n"
        + original_task.description
    ),
    "role": original_task.role,
    "priority": max(1, original_task.priority - 1),  # escalate priority
    "parent_id": original_task.id,
    "tags": ["self-review-fix", f"parent:{original_task.id}"],
}
```

  4. POST fix task to task server: `POST /tasks`
  5. Log the block with full context

**Timeout**: 5s (HTTP POST to local task server)
**Input**: `{ original_task: Task, verdict: EnhancedCrossModelVerdict }`
**Output on SUCCESS**: Fix task created, original task stays in `verification_failures` → pipeline continues for other tasks
**Output on FAILURE**:
  - `FAILURE(server_error)`: Task server returned 5xx → Log error. Original task remains in `verification_failures` and will be retried on next tick via `maybe_retry_task`.
  - `FAILURE(timeout)`: POST to task server took >5s → Same as server_error handling.

**Observable states during this step**:
  - Operator sees: original task failed verification, new fix task in OPEN state
  - Database: original task in `verification_failures`, new fix task with status OPEN
  - Logs: `[task_lifecycle] self_review: BLOCKED task={task_id} — created fix task={fix_task_id} with {n} blocking issues`
  - Metrics: `self_review_block` event with `{task_id, reviewer_model, blocked_dimensions, fix_task_id}`

---

### STEP 7b: Pass — Continue to Approval Gate
**Actor**: Orchestrator (`process_completed_tasks` in `task_lifecycle.py`)
**Action**: Review passed (or passed with warnings). Continue normal pipeline:
  1. Record review results in task metadata
  2. If suggestions exist, append them to task history for operator visibility
  3. Proceed to approval gate evaluation (existing logic, unchanged)
  4. Approval gate creates PR (if mode=pr) or auto-merges (if mode=auto)

**Observable states during this step**:
  - Database: `task.metadata["self_review"] = { verdict, dimensions, reviewer_model, timestamp }`
  - Logs: `[task_lifecycle] self_review: PASSED task={task_id} (suggestions={n})`

---

## State Transitions

```
[task DONE, janitor passed, quality gates passed]
    → (STEP 1-3: collect context, select model, build prompt)
    → (STEP 4: call reviewer LLM)
        → (STEP 4 FAILURE: timeout/error) → DEFAULT APPROVE → approval gate
    → (STEP 5: parse response)
        → (STEP 5 FAILURE: unparseable) → DEFAULT APPROVE → approval gate
    → (STEP 6: evaluate verdict)
        → (approve) → approval gate → PR / merge / review
        → (request_changes, block_on_issues=true, has blocking issues) → STEP 7a: create fix task
        → (request_changes, block_on_issues=false) → approval gate (warnings only)
        → (request_changes, no blocking issues, no vuln) → approval gate (suggestions only)
```

---

## Handoff Contracts

### Orchestrator → Reviewer LLM (via `call_llm`)
**Provider**: OpenRouter (default) or direct provider
**Payload**:
```json
{
  "prompt": "string — enhanced 5-dimension review prompt",
  "model": "string — e.g. google/gemini-flash-1.5",
  "provider": "string — e.g. openrouter",
  "max_tokens": 1024,
  "temperature": 0.0
}
```
**Success response**: Raw text containing JSON object with verdict + dimensions
**Failure response**: `RuntimeError` raised by `call_llm`
**Timeout**: 60s — treated as FAILURE → default approve

### Orchestrator → Task Server (fix task creation)
**Endpoint**: `POST http://127.0.0.1:8052/tasks`
**Payload**:
```json
{
  "title": "Fix self-review findings: {original_title}",
  "description": "string — blocking issues + dimension summary + original description",
  "role": "string — same role as original task",
  "priority": "int — escalated (original - 1, min 1)",
  "parent_id": "string — original task ID",
  "tags": ["self-review-fix", "parent:{original_task_id}"]
}
```
**Success response**:
```json
{
  "id": "string",
  "status": "open",
  "title": "string"
}
```
**Failure response**: HTTP 5xx — log and allow retry on next tick
**Timeout**: 5s

---

## Cleanup Inventory

This workflow creates minimal persistent state. Cleanup is straightforward:

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Review results in task metadata | STEP 6/7b | Never — historical record | N/A (append-only) |
| Fix task (if created) | STEP 7a | Normal task lifecycle | Task completion or failure |
| Metrics event | STEP 6 | Log rotation | Standard `.sdd/metrics/` rotation |

No orphan risk: the only created resource (fix task) enters the normal task lifecycle
and will be claimed, completed, or failed through standard mechanisms.

---

## Configuration

Enhancement to existing `CrossModelVerifierConfig`:

```python
@dataclass(frozen=True)
class CrossModelVerifierConfig:
    enabled: bool = True           # CHANGED: default True (was False)
    reviewer_model: str | None = None
    provider: str = "openrouter"
    max_diff_chars: int = 12_000
    max_tokens: int = 1024         # CHANGED: increased from 512 (5 dimensions need more tokens)
    block_on_issues: bool = True
    review_dimensions: list[str] = field(default_factory=lambda: [
        "vulnerabilities", "correctness", "style", "test_gaps", "scope"
    ])
    always_block_on_vulnerabilities: bool = True  # NEW: vuln findings always block
    timeout_s: float = 60.0        # NEW: explicit timeout (was implicit in call_llm)
```

---

## Interaction with Existing Pipeline Components

### Quality Gates (runs BEFORE this workflow)
Quality gates (lint, type check, tests, mutation) run before the self-review.
Their results are passed INTO the review prompt as context, so the reviewer
LLM can assess whether test failures are related to the agent's changes.

**No change to quality gates logic.** Quality gates continue to block independently.
The self-review is an additional layer, not a replacement.

### Guardrails (overlap with scope detection)
The janitor guardrails already detect scope violations (`_check_scope_violations`)
and secrets (`_check_secrets`). The self-review adds a second opinion from an LLM
that can catch contextual violations the regex-based guardrails miss.

**No change to guardrails.** Both systems run independently. If guardrails catch
a scope violation, the task is already blocked before the self-review runs.
The self-review catches violations that guardrails miss (e.g., changes that are
in-scope by file but out-of-scope by intent).

### Approval Gate (runs AFTER this workflow)
The approval gate (`auto`, `review`, `pr`) is unchanged. It evaluates after
the self-review passes. The self-review results are available in task metadata
for the approval gate to reference in PR descriptions.

**Enhancement**: When creating a PR, include self-review results in the PR body:
```
## Self-Review
- Reviewer: google/gemini-flash-1.5
- Verdict: approve
- Suggestions: 2 (non-blocking)
```

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — approve | Agent completes task, reviewer approves | Task proceeds to approval gate, review recorded in metadata |
| TC-02: Happy path — approve with suggestions | Reviewer approves but lists style suggestions | Task proceeds, suggestions recorded in metadata, not blocking |
| TC-03: Block — vulnerability found | Reviewer finds SQL injection | Task blocked, fix task created with vulnerability details, priority escalated |
| TC-04: Block — correctness issue | Reviewer finds logic bug in blocking_issues | Task blocked, fix task created |
| TC-05: Block — scope violation | Agent changed files outside owned_files, reviewer confirms | Task blocked, fix task created with scope violation details |
| TC-06: Non-blocking — style only | Reviewer requests changes but only style issues, no blocking_issues | Task proceeds with warnings logged |
| TC-07: Non-blocking — test gaps (non-critical) | Reviewer flags missing tests but not in blocking_issues | Task proceeds with suggestions logged |
| TC-08: Reviewer timeout | Reviewer LLM takes >60s | Default to approve, warning logged, task proceeds |
| TC-09: Reviewer error | OpenRouter returns 500 | Default to approve, error logged, task proceeds |
| TC-10: Reviewer rate-limited | OpenRouter returns 429 | Default to approve, warning logged, task proceeds |
| TC-11: Unparseable response | Reviewer returns prose instead of JSON | Default to approve, first 200 chars logged |
| TC-12: Empty diff | Agent signaled completion but changed nothing | Review skipped, warning logged, task proceeds |
| TC-13: Worktree gone | Agent crashed and worktree was cleaned up | Review skipped, error logged, task proceeds |
| TC-14: block_on_issues=false | Config disables blocking | Reviewer finds issues but task proceeds with warnings only |
| TC-15: Large diff truncation | Diff >12K chars | Diff truncated, review proceeds on partial diff, warning logged |
| TC-16: Fix task creation failure | Task server returns 500 when creating fix task | Original task stays in verification_failures, retried on next tick |
| TC-17: Different writer models | Writer is claude/gemini/codex/qwen | Correct reviewer model selected from different family |
| TC-18: Explicit reviewer override | Config specifies reviewer_model | Override model used regardless of writer |
| TC-19: Vulnerability always blocks | block_on_issues=false but vuln found with always_block_on_vulnerabilities=true | Task blocked despite block_on_issues=false |
| TC-20: Review results in PR body | Approval mode=pr, review passed | PR body includes self-review summary section |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `call_llm` supports OpenRouter provider with the reviewer models listed in `_WRITER_TO_REVIEWER` | Verified: `cross_model_verifier.py` already uses this | Low |
| A2 | Agent worktree persists until `process_completed_tasks` runs | Verified: `_preserved_worktrees` in orchestrator keeps worktrees alive | Low — STEP 1 handles worktree_gone |
| A3 | Guardrail scope-violation detection and self-review scope detection can coexist without conflicts | Not verified | Medium — if guardrails already blocked the task, self-review never runs (fine). If guardrails miss a violation, self-review catches it (desired). No conflict path identified. |
| A4 | Reviewer models (Gemini Flash, Claude Haiku) can produce structured JSON output reliably | Partially verified: existing `_parse_response` has fallback parsing for unparseable responses | Medium — unparseable responses default to approve |
| A5 | 1024 max_tokens is sufficient for 5-dimension JSON response | Not verified — current 512 is for 3-field response | Medium — if truncated, JSON may be incomplete, triggering unparseable fallback (approve). Monitor in production. |
| A6 | `task.owned_files` is always populated for scope-violation detection | Not verified — may be empty for tasks without explicit file ownership | Low — if empty, scope detection degrades gracefully (no violations flagged by pre-check, reviewer still evaluates from diff) |

---

## Open Questions

- **Q1**: Should the self-review run on L0 (deterministic) tasks that currently use the fast path? The fast path skips some verification — should it skip self-review too? **Recommendation**: Yes, skip for L0 tasks. They are deterministic by definition and the review adds latency without value.
- **Q2**: Should review results be surfaced in the Textual dashboard (`bernstein live`)? If so, where — in the task detail view or as a separate review log?
- **Q3**: Should there be a cost cap per review? Current estimate: ~$0.001-0.003 per review with Gemini Flash. At 100 tasks/day, that's $0.10-0.30/day. Acceptable, but should be configurable.
- **Q4**: Should the self-review results be included in the agent's "lessons learned" for future task context injection? This would let agents learn from common review findings.

---

## Implementation Guide

### Files to modify

1. **`src/bernstein/core/cross_model_verifier.py`** — Primary changes:
   - Add `EnhancedCrossModelVerdict` and `DimensionResult` dataclasses
   - Expand `_REVIEW_PROMPT_TEMPLATE` to the 5-dimension prompt
   - Update `_parse_response` to handle the enhanced JSON format with backward compat
   - Increase default `max_tokens` from 512 to 1024
   - Add `_detect_scope_violations(changed_files, owned_files)` helper
   - Add `_build_quality_gate_summary(qg_result)` helper

2. **`src/bernstein/core/task_lifecycle.py`** — Integration:
   - Pass quality gate results and scope-violation pre-check into verifier
   - Handle `EnhancedCrossModelVerdict` in the verdict evaluation logic
   - Record review results in `task.metadata["self_review"]`
   - Emit `self_review_verdict` metric event

3. **`src/bernstein/core/models.py`** — Config change:
   - Update `CrossModelVerifierConfig` defaults: `enabled=True`, `max_tokens=1024`
   - Add `always_block_on_vulnerabilities`, `timeout_s`, `review_dimensions` fields

4. **`src/bernstein/core/approval.py`** — PR body enhancement:
   - Include self-review summary in `_pr_body` when review results available in task metadata

### Files NOT to modify
- `quality_gates.py` — no changes, runs independently before self-review
- `janitor.py` — no changes, guardrails run independently
- `spawner.py` — no changes, agents are not aware of the self-review
- `server.py` / routes — no new endpoints needed

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-29 | Initial spec created from discovery of existing `cross_model_verifier.py`, `quality_gates.py`, `approval.py`, and `task_lifecycle.py` | — |
| 2026-03-29 | Existing cross-model verifier is disabled by default and scoped to correctness + security only | Spec requires enabled=True default and 5-dimension scope |
| 2026-03-29 | Existing `_parse_response` defaults to approve on parse failure — this is safe for pipeline liveness | Preserved in spec: all failures default to approve |
| 2026-03-29 | `process_completed_tasks` already has the insertion point between quality gates and approval gate | Spec uses existing insertion point, no architectural change needed |
