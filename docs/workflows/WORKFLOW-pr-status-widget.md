# WORKFLOW: Embeddable Status Widget for GitHub PR Descriptions
**Version**: 0.1
**Date**: 2026-04-11
**Author**: Workflow Architect
**Status**: Draft
**Implements**: road-006 — Embeddable status widget for GitHub PR descriptions

---

## Overview

When Bernstein creates or updates a PR, the PR body (description) must contain
a self-updating status widget that shows: agents used, cost, quality gate results,
quality score/grade, and time-to-completion. The widget is embedded as a Markdown
section in the PR body — not a separate comment — so every code reviewer sees
Bernstein's value at a glance without scrolling through comments.

This workflow enhances the existing `approval.py:_pr_body` method by:
1. Replacing the sparse metadata block with a structured status widget
2. Pulling data from quality gates (`GateReport`), quality scores (`QualityScore`),
   cost tracking (`CostForecast`), and run reports (`RunReport`)
3. Making the widget idempotent — the same PR body update can be called multiple
   times (e.g., after quality gate re-runs) without duplicating content
4. Using an HTML comment marker (`<!-- bernstein-status-widget -->`) for
   programmatic identification, consistent with `cost_reporter.py`'s existing
   `<!-- bernstein-cost-annotation -->` pattern

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Orchestrator (`task_lifecycle.py`) | Triggers widget generation after task completion |
| Approval Gate (`approval.py`) | Calls `_pr_body` during PR creation; calls widget updater after quality gates |
| Widget Builder (new: `pr_status_widget.py`) | Assembles the Markdown widget from structured data sources |
| Quality Gates (`quality_gates.py`) | Supplies `QualityGatesResult` with per-gate pass/fail/skip |
| Quality Score (`quality_score.py`) | Supplies `QualityScore` with total (0-100), grade (A-F), trend |
| Cost Tracker (`cost_tracker.py`) | Supplies per-agent and per-model cost breakdowns |
| Run Report (`run_report.py`) | Supplies `RunReport` with duration, agent count, timeline |
| GitHub API (`git_pr.py`, `gh` CLI) | Reads and updates PR body via `gh pr edit --body` |
| Check Run Client (`check_runs.py`) | Links check run URL in the widget (existing, read-only) |

---

## Prerequisites

- Task has reached the approval gate (quality gates have run at least once)
- `gh` CLI is installed and authenticated (same requirement as existing PR creation)
- PR has been created (widget is embedded in body on creation, then updated)
- Quality gate results exist in `.sdd/metrics/quality_gates.jsonl`
- Cost data exists in `.sdd/runtime/costs/{run_id}.json`

---

## Trigger

### Initial embed
The widget is first embedded when `ApprovalGate.create_pr()` constructs the
PR body. The `_pr_body` method calls the widget builder to generate the initial
widget section.

### Updates
The widget is updated after:
1. Quality gates complete a re-run (status changes from pending to pass/fail)
2. Cost data is finalized (agent session completes)
3. The PR is re-pushed with new commits (quality gates re-run)

Update is triggered by `update_pr_status_widget()` called from
`process_completed_tasks()` after quality gate evaluation.

---

## Workflow Tree

### STEP 1: Collect Widget Data
**Actor**: Widget Builder (`pr_status_widget.py`)
**Action**: Gather all data needed for the status widget:
  1. Read `QualityGatesResult` for the task from quality gates (per-gate status)
  2. Read `QualityScore` from quality score store (grade, trend)
  3. Read cost data from cost tracker (per-agent breakdown, total cost)
  4. Read agent session metadata (role, model, duration per agent)
  5. Read check run URL if available (from `CheckRunResult.html_url`)
  6. Compute time-to-completion from task timestamps (`created_at` to `completed_at`)
**Timeout**: 5s (all data is local file reads)
**Input**: `{ task: Task, run_id: str, gate_report: GateReport | None, quality_score: QualityScore | None }`
**Output on SUCCESS**: `WidgetData` dataclass -> GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(missing_data)`: Some data sources unavailable -> [recovery: render widget with available data, mark missing sections as "pending"; do NOT block PR creation]

**Observable states during this step**:
  - Customer sees: nothing (PR not yet updated)
  - Operator sees: log line `[pr_status_widget] collecting widget data for task {task_id}`
  - Database: no change
  - Logs: `[pr_status_widget] data collection: gates={present|missing}, score={present|missing}, cost={present|missing}`

---

### STEP 2: Render Widget Markdown
**Actor**: Widget Builder (`pr_status_widget.py`)
**Action**: Assemble the Markdown widget string from `WidgetData`:

```markdown
<!-- bernstein-status-widget -->
<table>
<tr><td>

### Bernstein Run Summary

| Metric | Value |
|---|---|
| Quality | **B** (78/100) improving |
| Cost | **$0.0342** |
| Duration | **2m 14s** |
| Agents | 2 |

<details>
<summary>Quality Gates (4/5 passed)</summary>

| Gate | Status |
|---|---|
| lint | pass |
| type_check | pass |
| tests | pass |
| security_scan | pass |
| coverage_delta | fail (delta -2.1%) |

</details>

<details>
<summary>Agents Used</summary>

| Role | Model | Cost | Duration |
|---|---|---|---|
| backend | claude-sonnet-4-6 | $0.0180 | 1m 42s |
| qa | claude-haiku-4-5 | $0.0162 | 0m 32s |

</details>

</td></tr>
</table>
<!-- /bernstein-status-widget -->
```

**Key rendering rules**:
  - Widget is wrapped in HTML comment markers for idempotent replacement
  - Quality grade uses bold: `**A**`, `**B**`, etc.
  - Cost formatted to 4 decimal places: `$0.0342`
  - Duration formatted as `Xm Ys` (not raw seconds)
  - Gate status uses text labels: `pass`, `fail`, `warn`, `skip`, `timeout`
  - Missing data rendered as `pending` (not omitted)
  - Collapsible `<details>` sections keep the widget compact by default

**Timeout**: <1s (string formatting only)
**Input**: `WidgetData`
**Output on SUCCESS**: `str` (Markdown) -> GO TO STEP 3
**Output on FAILURE**: Not possible (pure string formatting)

**Observable states during this step**:
  - Customer sees: nothing (rendering is in-memory)
  - Operator sees: nothing
  - Database: no change
  - Logs: `[pr_status_widget] rendered widget: {len(markdown)} chars, {agent_count} agents, grade={grade}`

---

### STEP 3: Embed or Update PR Body
**Actor**: Widget Builder via `gh` CLI
**Action**: Insert or replace the widget in the PR body.

**3a: Initial embed (PR creation)**
  - Called from `ApprovalGate.create_pr()` -> `_pr_body()`
  - Widget Markdown is appended to the PR body before the `---` footer
  - No GitHub API call needed — body is passed to `gh pr create --body`

**3b: Update (post-creation)**
  - Read current PR body via `gh pr view {pr_number} --json body --jq .body`
  - Find the `<!-- bernstein-status-widget -->` ... `<!-- /bernstein-status-widget -->` block
  - Replace the block with the new widget Markdown
  - Write updated body via `gh pr edit {pr_number} --body "{new_body}"`
  - If no existing widget marker found, append widget before the footer

**Timeout**: 15s (GitHub API round-trip)
**Input**: `{ pr_number: int, repo: str, widget_markdown: str }`
**Output on SUCCESS**: `True` -> DONE
**Output on FAILURE**:
  - `FAILURE(gh_not_available)`: `gh` CLI not installed or not authenticated -> [recovery: log warning, skip widget update; PR still valid without widget]
  - `FAILURE(pr_not_found)`: PR was closed or deleted -> [recovery: log warning, no action needed]
  - `FAILURE(timeout)`: GitHub API slow -> [recovery: retry 1x with 5s backoff -> log warning and continue; widget is informational, not blocking]
  - `FAILURE(body_too_large)`: PR body exceeds GitHub's 65536 char limit -> [recovery: render compact widget without `<details>` sections, retry]

**Observable states during this step**:
  - Customer sees: PR description updated with status widget (on page refresh)
  - Operator sees: log line `[pr_status_widget] updated PR #{pr_number} widget`
  - Database: no change (GitHub is the store)
  - Logs: `[pr_status_widget] PR #{pr_number} body updated ({len} chars), widget replaced={true|false}`

---

## State Transitions

```
[pr_created_without_widget] -> (step 1-3 succeed on creation) -> [pr_has_widget]
[pr_has_widget] -> (quality gates re-run) -> [pr_has_widget] (widget updated in-place)
[pr_has_widget] -> (PR merged/closed) -> [terminal] (no further updates)
[pr_created_without_widget] -> (gh unavailable) -> [pr_without_widget] (degraded, still functional)
```

---

## Handoff Contracts

### Widget Builder -> GitHub API (PR body update)
**Endpoint**: `gh pr edit {pr_number} --body {body}` (CLI, not REST)
**Payload**: Full PR body string with embedded widget
**Success response**: Exit code 0
**Failure response**: Exit code != 0, stderr contains error
**Timeout**: 15s
**ON FAILURE**: Log warning, do not block task completion

### Quality Gates -> Widget Builder
**Payload**:
```python
{
  "gate_report": GateReport,      # from quality_gates.py
  "quality_score": QualityScore,  # from quality_score.py — may be None
}
```
**Contract**: Widget Builder must handle `None` for any field — render "pending"

### Cost Tracker -> Widget Builder
**Payload**:
```python
{
  "total_cost_usd": float,
  "per_agent": list[AgentCostSummary],
  "per_model": list[ModelCostBreakdown],
}
```
**Contract**: Widget Builder must handle empty lists — render "$0.0000"

### Approval Gate -> Widget Builder
**Payload**:
```python
{
  "task": Task,
  "role": str,
  "model": str,
  "cost_usd": float,
  "test_summary": str,
  "gate_report": GateReport | None,
  "quality_score": QualityScore | None,
  "agents": list[AgentSession],
  "run_id": str,
}
```

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Widget in PR body | Step 3 | PR deletion/close | Automatic (GitHub manages) |

No orphan risk — the widget is embedded in the PR body, not a standalone resource.

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `_pr_body` in `approval.py` (line 355) currently returns a simple string with no widget — must be enhanced | Medium | Step 3a | Implementation must modify `_pr_body` signature to accept widget data |
| RC-2 | `cost_reporter.py` posts a *separate comment* — widget should supersede this or they coexist | Medium | Overview | Decision: coexist. Widget is in body, cost comment remains for historical runs. New runs embed cost in widget. |
| RC-3 | `gh pr edit --body` requires the *entire* body — partial update is not supported | Low | Step 3b | Widget Builder must read-then-write the full body |
| RC-4 | GitHub PR body limit is 65536 characters — large runs with many agents could exceed | Low | Step 3 FAILURE | Compact rendering fallback handles this |
| RC-5 | `QualityScore` is computed in `quality_score.py` but may not be called for every task — depends on config | Medium | Step 1 | Widget renders "pending" when score is None |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — initial embed | PR created with all data available | Widget in PR body with all sections populated |
| TC-02: Happy path — update after gate re-run | Quality gates re-run on same PR | Widget replaced in-place, no duplication |
| TC-03: Missing quality score | `QualityScore` is None | Widget renders quality as "pending", no crash |
| TC-04: Missing cost data | Cost tracker returns empty | Widget renders "$0.0000", no crash |
| TC-05: gh CLI unavailable | `gh` not in PATH | Warning logged, PR created without widget, no crash |
| TC-06: PR body too large | Body exceeds 65536 chars | Compact widget rendered without `<details>` sections |
| TC-07: No existing widget marker | Update called on PR without widget | Widget appended before footer |
| TC-08: Multiple agents | 3+ agents with different roles/models | Agent table shows all agents with costs and durations |
| TC-09: All gates failed | Every quality gate fails | Widget shows 0/N passed, grade F |
| TC-10: Idempotent update | Same data, update called twice | PR body unchanged after second call |
| TC-11: PR closed before update | PR merged/closed between collect and update | Warning logged, no crash |
| TC-12: Concurrent updates | Two tasks complete simultaneously for same PR | Last-write-wins (acceptable — both contain latest data) |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `gh pr edit --body` is available in all `gh` versions Bernstein supports | Not verified — gh 2.0+ | Widget updates silently fail on old gh versions |
| A2 | PR body is read-modify-write safe (no concurrent PR body edits from other tools) | Reasonable for Bernstein-managed PRs | Last-write-wins race; low risk since Bernstein owns these PRs |
| A3 | GitHub renders `<details>` and `<table>` in PR descriptions | Verified: GitHub Markdown supports these | Widget renders correctly |
| A4 | Quality gate results are available synchronously when the widget is built | Verified: gates run before approval gate | Step 1 handles None gracefully |
| A5 | `cost_reporter.py` and the widget can coexist (both write to the same PR) | Design decision | Cost comment is a separate comment, not body — no conflict |

## Open Questions

- Should the widget include a link back to the Bernstein dashboard/TUI for the run? (Requires dashboard to be network-accessible)
- Should the existing `cost_reporter.py` comment be suppressed when the widget is present? (Avoids duplicate cost info)
- Should the widget include the run timeline (ASCII bar chart from `run_report.py`)? (Adds visual value but increases body size)

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-11 | Initial spec created from codebase discovery | — |
| 2026-04-11 | Verified `_pr_body` in approval.py:355 — currently 4-field metadata only | Spec covers full enhancement path |
| 2026-04-11 | Verified `cost_reporter.py` uses HTML comment markers — same pattern adopted | Consistent with existing codebase conventions |
