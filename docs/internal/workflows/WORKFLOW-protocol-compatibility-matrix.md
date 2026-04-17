# WORKFLOW: Protocol Compatibility Matrix Testing

**Version**: 1.0
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Approved
**Implements**: Task 6dc9550a98aa — Protocol Compatibility Matrix — CI-Enforced Interop Testing

---

## Overview

This workflow tests Bernstein's compatibility with multiple versions of MCP, A2A, and ACP protocols across every pull request and tagged release. It produces a matrix of test results that determines whether the release can proceed and generates a public compatibility table.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| GitHub Actions (CI runner) | Triggers and orchestrates the test matrix |
| Test Suite | Executes protocol-specific tests against each version combination |
| Artifact Storage | Persists test results for compatibility table generation |
| Release Workflow | Consumes test results to block incompatible releases |
| Documentation Publisher | Publishes results to `docs/compatibility.md` and README |

---

## Prerequisites

- Git repository with `.github/workflows/ci.yml` configured
- `tests/protocol/` directory exists with version-specific fixtures
- PyPI packages for protocol libraries (mcp, a2a) are available
- `scripts/generate_compatibility_table.py` exists and can be executed
- GitHub token with write permissions for push to main branch

---

## Trigger

**When**: Every push to main and every pull request
**How**: GitHub Actions workflow trigger on `push` and `pull_request` events
**Endpoint**: GitHub Actions (workflows/ci.yml)

---

## Workflow Tree

### STEP 1: Determine Test Matrix Dimensions
**Actor**: GitHub Actions (workflow engine)
**Action**: Read `tests/protocol/versions.json` to determine which protocol versions to test. Compute the Cartesian product of:
- Python versions: [3.12, 3.13]
- MCP versions: [1.0, 1.1]
- A2A versions: [0.2, 0.3]
- ACP versions: [latest]

**Timeout**: 5s
**Input**: `{ python_versions: list[str], mcp_versions: list[str], a2a_versions: list[str], acp_versions: list[str] }`
**Output on SUCCESS**: `{ matrix: list[dict] }` where each dict contains Python version + protocol versions → GO TO STEP 2
**Output on FAILURE**:
- `FAILURE(file_not_found)`: `tests/protocol/versions.json` does not exist → [recovery: return 400 "Matrix config missing", no cleanup needed]
- `FAILURE(invalid_json)`: JSON is malformed → [recovery: log error, fail workflow with 1 message "Invalid versions.json format"]

**Observable states during this step**:
- Customer sees: CI workflow starting
- Operator sees: workflow run "Protocol Compat Matrix" in Checks section
- Database: GitHub Actions job `matrix-setup` status = "running"
- Logs: `[ci] matrix dimensions: 2 python × 2 mcp × 2 a2a × 1 acp = 8 test jobs`

---

### STEP 2: Run Protocol Version Tests (Parallel)
**Actor**: GitHub Actions (matrix job strategy)
**Action**: For each combination in the matrix (8 jobs in parallel):
1. Checkout code
2. Set up Python + uv
3. Install specific protocol versions: `pip install mcp==X.Y a2a==X.Y`
4. Run `uv run pytest tests/protocol/ -v --tb=short`
5. Collect results: `{python: "3.12", mcp: "1.0", a2a: "0.2", passed: N, failed: N, duration: Xs}`
6. Upload to artifact: `protocol-test-results-{python}-{mcp}-{a2a}.json`

**Timeout**: 120s per job (fail if any job exceeds this)
**Input**: `{ python: str, mcp: str, a2a: str }`
**Output on SUCCESS**: `{ passed: int, failed: int, duration: float, test_cases_run: int }` → GO TO STEP 3
**Output on FAILURE**:
- `FAILURE(dependency_install)`: `pip install mcp==X.Y` fails → [recovery: log "Version X.Y not available on PyPI", mark as INCOMPATIBLE, continue to next version, no cleanup]
- `FAILURE(test_timeout)`: Test suite exceeds 120s → [recovery: kill test process, mark as TIMEOUT_FAIL, continue to next job]
- `FAILURE(test_failure)`: Test cases fail → [recovery: capture test output, mark as TEST_FAIL with error details, continue]
- `FAILURE(artifact_upload)`: Cannot upload results → [recovery: retry upload x3 with 10s backoff → if still fails, fail entire workflow]

**Observable states during this step**:
- Customer sees: 8 jobs running in parallel in GitHub Checks
- Operator sees: matrix job status `[✓ 3.12+1.0 in progress] [✓ 3.13+1.0 in progress] ...`
- Logs: `[ci-matrix] job 3.12/mcp1.0/a2a0.2 started` → `[ci-matrix] job 3.12/mcp1.0/a2a0.2 result: PASS (15s)`
- Database: GitHub Actions each job has status "in_progress" → "success" or "failure"

---

### STEP 3: Aggregate Test Results
**Actor**: GitHub Actions (new job, depends on matrix jobs)
**Action**:
1. Download all artifacts from matrix jobs
2. Merge all results into `protocol-compat-results.json`:
```json
{
  "timestamp": "2026-03-29T14:35:00Z",
  "github_run_id": "12345678",
  "commit_sha": "abc123...",
  "results": [
    { "python": "3.12", "mcp": "1.0", "a2a": "0.2", "status": "pass", "duration": 15 },
    { "python": "3.12", "mcp": "1.0", "a2a": "0.3", "status": "pass", "duration": 17 },
    { "python": "3.13", "mcp": "1.0", "a2a": "0.2", "status": "fail", "error": "..." },
    ...
  ],
  "summary": {
    "total_combinations": 8,
    "passed": 7,
    "failed": 1,
    "incompatible": 0,
    "timeout": 0
  }
}
```
3. Upload aggregated results as workflow artifact

**Timeout**: 30s
**Input**: `{ artifact_list: list[str] }`
**Output on SUCCESS**: `{ results_file: str, summary: dict, status: "complete" }` → GO TO STEP 4
**Output on FAILURE**:
- `FAILURE(artifact_download)`: Cannot download one or more artifacts → [recovery: retry x3 with 5s backoff → if fail, abort workflow with error "Failed to download test results"]
- `FAILURE(json_merge_error)`: Results malformed or incompatible structure → [recovery: log which artifact is malformed, fail workflow with error]

**Observable states during this step**:
- Operator sees: Results aggregation job running
- Logs: `[ci] Aggregating 8 test result artifacts...` → `[ci] Aggregated: 7 PASS, 1 FAIL, 0 INCOMPATIBLE`

---

### STEP 4: Determine Compatibility Status
**Actor**: GitHub Actions (status check job)
**Action**:
1. Read aggregated results
2. Compare against baseline compatibility set (defined in `tests/protocol/compatibility-baseline.json`):
   - If any previously-compatible version now fails: **BREAKING_CHANGE**
   - If new version is compatible: **NEW_COMPATIBLE**
   - If new version is incompatible: **KNOWN_INCOMPATIBLE**
3. Set output: `{status: "compatible" | "breaking_change" | "degraded"}`
4. Fail the workflow if status == "breaking_change" (blocks release)

**Timeout**: 10s
**Input**: `{ current_results: dict, baseline: dict }`
**Output on SUCCESS**: `{ status: str, breaking_changes: list, new_compatibilities: list }` → GO TO STEP 5
**Output on FAILURE**:
- `FAILURE(baseline_missing)`: `compatibility-baseline.json` not found → [recovery: create baseline from current results, log warning, continue (first time setup)]
- `FAILURE(comparison_error)`: Baseline and results have incompatible structure → [recovery: fail workflow with "Compatibility data format mismatch"]

**Observable states during this step**:
- Operator sees: Status check job running
- GitHub UI: If breaking_change detected, workflow is marked with ❌ (failure)
- Logs: `[compat-check] Comparing against baseline...` → `[compat-check] ⚠️ BREAKING_CHANGE: mcp 1.1 with a2a 0.2 no longer compatible`

---

### STEP 5: Publish Protocol Compatibility Badge
**Actor**: GitHub Actions (badge generation job)
**Action**:
1. Generate shield.io badge SVG URLs for each protocol:
   - `https://img.shields.io/badge/mcp-1.0%2C%201.1-blue`
   - `https://img.shields.io/badge/a2a-0.2%2C%200.3-blue` (or ❌ if any version fails)
   - `https://img.shields.io/badge/acp-latest-brightgreen`
2. Embed in README.md (or create .github/protocol-compatibility-badges.md for documentation)
3. Commit and push if on main branch

**Timeout**: 15s
**Input**: `{ status_by_protocol: dict, baseline: dict }`
**Output on SUCCESS**: `{ badge_urls: dict, readme_updated: bool }` → GO TO STEP 6
**Output on FAILURE**:
- `FAILURE(badge_generation)`: Cannot generate SVG → [recovery: fallback to text-based badge `[MCP 1.x compatible]`, continue]
- `FAILURE(git_push)`: Cannot push to main → [recovery: log warning, fail workflow — manual investigation needed]

**Observable states during this step**:
- GitHub Repo: README.md contains protocol compatibility badges
- Logs: `[badge] Generating protocol badges...` → `[badge] 3 badges created and pushed`

---

### STEP 6: Trigger Compatibility Table Generation
**Actor**: GitHub Actions (workflow dispatch)
**Action**:
1. If all tests passed and no breaking changes detected:
   - Trigger `WORKFLOW-compatibility-table-generation` workflow via `workflow_dispatch` event
   - Pass: `{ results_artifact: str, commit_sha: str, event: "test_complete" }`
2. If breaking changes detected:
   - Create GitHub annotation on PR/commit: ⚠️ Breaking change detected in protocol compatibility
   - Do NOT trigger table generation (operator must review first)

**Timeout**: 5s
**Input**: `{ status: str, breaking_changes: list }`
**Output on SUCCESS**: `{ trigger_status: "queued" | "skipped", reason: str }` → WORKFLOW COMPLETE
**Output on FAILURE**:
- `FAILURE(workflow_trigger)`: Cannot trigger workflow_dispatch → [recovery: retry x2 with 5s backoff → if fail, log error but don't block workflow]

**Observable states during this step**:
- GitHub UI: "workflow_dispatch" event triggered for table generation (visible in Actions tab)
- Logs: `[dispatch] Triggering WORKFLOW-compatibility-table-generation...`
- Operator sees: If breaking change, GitHub comment on PR with warning

---

## State Transitions

```
[pending]
  → (step 1: matrix OK, step 2: all tests OK, step 3: aggregation OK, step 4: status="compatible")
    → [success] (continue to table generation)

[pending]
  → (step 4: status="breaking_change")
    → [failure_manual_review_required] (operator must investigate)

[pending]
  → (any step timeout or artifact error)
    → [failure_infrastructure] (retry or manual investigation)
```

---

## Handoff Contracts

### CI Matrix Tests → Artifact Upload
**Endpoint**: GitHub Actions `upload-artifact@v7`
**Payload**:
```json
{
  "name": "protocol-test-results-{python}-{mcp}-{a2a}",
  "path": "test-results/",
  "retention-days": 30
}
```
**Success response**:
```json
{
  "artifact_id": "string",
  "size": "bytes"
}
```
**Failure response**:
```json
{
  "ok": false,
  "error": "Artifact upload failed",
  "code": "UPLOAD_ERROR",
  "retryable": true
}
```
**Timeout**: 60s
**On failure**: Retry upload x3 with 10s backoff → if all fail, fail workflow

---

### Test Results → Compatibility Check
**Endpoint**: In-job Python script `scripts/check_compatibility.py`
**Payload**:
```json
{
  "current_results": { "python": "3.12", "mcp": "1.0", "a2a": "0.2", "status": "pass" },
  "baseline_file": "tests/protocol/compatibility-baseline.json"
}
```
**Success response**:
```json
{
  "ok": true,
  "status": "compatible|breaking_change|degraded",
  "breaking_changes": ["mcp 1.1 + a2a 0.2 regression"],
  "new_compatibilities": []
}
```
**Failure response**:
```json
{
  "ok": false,
  "error": "Baseline file not found",
  "code": "MISSING_BASELINE",
  "retryable": false
}
```
**Timeout**: 10s

---

### Compatibility Check → Workflow Dispatch
**Endpoint**: GitHub Actions `workflow_dispatch` event
**Payload**:
```json
{
  "workflow_id": "compatibility-table-generation",
  "inputs": {
    "results_artifact": "protocol-compat-results.json",
    "commit_sha": "abc123",
    "trigger_event": "test_complete"
  }
}
```
**Success response**:
```json
{
  "ok": true,
  "workflow_run_id": "98765432"
}
```
**Failure response**:
```json
{
  "ok": false,
  "error": "Workflow not found",
  "code": "WORKFLOW_NOT_FOUND",
  "retryable": false
}
```
**Timeout**: 5s

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Artifact (`protocol-test-results-*.json`) | Step 2 | GitHub Actions (auto after 30 days) | Artifact expiration |
| Aggregated results artifact | Step 3 | GitHub Actions (auto after 30 days) | Artifact expiration |
| GitHub Actions job logs | Step 2–6 | GitHub Actions (per retention policy) | Log retention cleanup |
| Git commit (badge update) | Step 5 | Manual or via branch cleanup | Commit history |

No explicit cleanup needed — GitHub Actions handles artifact retention automatically.

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `tests/protocol/versions.json` does not exist in current codebase | High | Step 1 | ✅ Created with matrix dimensions (2 Python × 2 MCP × 2 A2A × 1 ACP) |
| RC-2 | `scripts/generate_compatibility_table.py` does not exist | High | Step 6 | ✅ Created; generates markdown table from test results |
| RC-3 | `tests/protocol/` directory does not exist | High | All steps | ✅ Created with conftest.py, __init__.py, and test_protocol_matrix.py |
| RC-4 | No baseline compatibility data exists | Medium | Step 4 | ✅ Created compatibility-baseline.json with initial 8-combination baseline |
| RC-5 | GitHub Actions matrix syntax is correct; no blocking issues found | Low | Step 2 | ✅ Verified; ready for CI integration |
| RC-6 | `scripts/check_compatibility.py` compatibility check script | High | Step 4 | ✅ Created; compares current results against baseline, detects regressions |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path (all compatible) | PR with code changes, all protocols supported | Workflow succeeds, table generation triggered, badges updated |
| TC-02: Breaking change detected | MCP 1.1 test suite fails when previously passed | Workflow fails at step 4, operator is notified via PR comment |
| TC-03: New protocol version available | New A2A version published to PyPI | Matrix detects and tests against new version automatically |
| TC-04: Artifact upload fails | Transient network error during artifact upload | Retry logic kicks in, uploads on second attempt |
| TC-05: Missing versions.json | `tests/protocol/versions.json` not found | Step 1 fails with clear error; workflow blocks |
| TC-06: Matrix timeout | Single test job exceeds 120s | Job marked as TIMEOUT_FAIL, continues to other jobs, summary reflects timeout |
| TC-07: Baseline comparison skipped (first run) | No baseline exists yet | Step 4 creates baseline from current results, workflow succeeds with warning |
| TC-08: Partial failure (3 of 8 pass) | 3 protocol combinations fail, 5 pass | Workflow succeeds, results show breakdown, operator sees failures in logs |
| TC-09: Badge generation fails | SVG generation service unreachable | Fallback to text badge, workflow continues |
| TC-10: Workflow dispatch fails | `workflow_dispatch` event cannot be triggered | Retry x2, then log error and fail workflow (manual investigation required) |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Protocol libraries are available on PyPI (mcp, a2a) | PyPI package registry | If versions unpublished, tests cannot run; mitigation: maintain fallback version list |
| A2 | GitHub Actions has sufficient quota for 8 parallel jobs | GitHub Actions documentation | Jobs may queue or be throttled; mitigation: stagger if needed |
| A3 | Test suite in `tests/protocol/` can complete within 120s | Not verified yet | Tests may timeout; mitigation: optimize test suite or increase timeout |
| A4 | Artifacts persist for at least 30 days on GitHub | GitHub Actions retention policy | Artifacts may expire during investigation; mitigation: download locally first |
| A5 | Main branch requires status checks to pass before merge | Repository settings | Breaking changes may slip through; mitigation: enforce required status checks |

---

## Open Questions

- Should protocol compatibility be a hard blocker on releases, or just a warning?
- Which protocol versions should be considered "officially supported" vs. "best effort"?
- Should the compatibility matrix test against agent adapters (Claude Code, Codex, etc.) or just protocol libraries?
- How should the compatibility baseline be initialized and maintained over time?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-29 | Initial spec created | — |
| 2026-03-29 | Reality Checker pass complete | All RC findings resolved; created tests/protocol/ directory structure, versions.json, baseline, and scripts |
| 2026-03-29 | Spec marked Approved | Workflow specification is implementable; CI integration can proceed |

