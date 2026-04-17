# WORKFLOW: Compatibility Table Generation and Publication

**Version**: 0.1
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Draft
**Implements**: Task 5ac221ec4f7f — Protocol Compatibility Matrix — CI-Enforced Interop Testing

---

## Overview

This workflow generates a human-readable compatibility table from protocol test results and publishes it to `docs/compatibility.md` and the README. It runs after the Protocol Compatibility Matrix workflow completes successfully with no breaking changes.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| GitHub Actions (workflow_dispatch trigger) | Triggered by WORKFLOW-protocol-compatibility-matrix on success |
| Python script (`scripts/generate_compatibility_table.py`) | Transforms test results into markdown table |
| Git automation | Commits and pushes generated documentation |
| Documentation CI | Updates README badges and version compatibility info |

---

## Prerequisites

- `scripts/generate_compatibility_table.py` script exists and is executable
- Test results artifact is available from previous workflow run
- `docs/` directory exists
- Git push permissions on main branch
- README.md exists in repo root

---

## Trigger

**When**: WORKFLOW-protocol-compatibility-matrix completes successfully with status="compatible"
**How**: GitHub Actions `workflow_dispatch` event triggered by previous workflow
**Inputs**: `{ results_artifact: str, commit_sha: str, trigger_event: str }`

---

## Workflow Tree

### STEP 1: Download Test Results Artifact
**Actor**: GitHub Actions (workflow run)
**Action**:
1. Receive `workflow_dispatch` event with inputs
2. Download artifact: `protocol-compat-results.json` from previous run
3. Parse and validate JSON structure
4. Extract test matrix: `{ python: "3.12", mcp: "1.0", a2a: "0.2", status: "pass", duration: 15 }`

**Timeout**: 30s
**Input**: `{ artifact_name: str, run_id: int }`
**Output on SUCCESS**: `{ results: list[dict], summary: dict }` → GO TO STEP 2
**Output on FAILURE**:
- `FAILURE(artifact_not_found)`: Artifact missing or expired → [recovery: fail workflow with "Test results artifact not found", no cleanup]
- `FAILURE(json_invalid)`: Results file corrupted → [recovery: fail workflow with "Results JSON malformed"]
- `FAILURE(download_timeout)`: Download exceeds 30s → [recovery: fail workflow with "Download timeout"]

**Observable states during this step**:
- Operator sees: Workflow download-results job running
- Logs: `[compat-gen] Downloading results from run #12345...` → `[compat-gen] Downloaded 8 test results`

---

### STEP 2: Generate Compatibility Tables
**Actor**: Python script (`scripts/generate_compatibility_table.py`)
**Action**:
1. Read test results JSON
2. Pivot results into three tables:

**Table A: By Protocol Version** (what versions work together)
```markdown
| Python | MCP 1.0 | MCP 1.1 | A2A 0.2 | A2A 0.3 | ACP latest |
|--------|---------|---------|---------|---------|-----------|
| 3.12   | ✓       | ✓       | ✓       | ✓       | ✓         |
| 3.13   | ✓       | ✓       | ✓       | ⚠️*     | ✓         |
```

**Table B: By Combination** (all passing combinations, sorted)
```markdown
| Python | MCP | A2A | ACP | Status | Duration |
|--------|-----|-----|-----|--------|----------|
| 3.12   | 1.0 | 0.2 | latest | ✓ Pass | 15s |
| 3.12   | 1.0 | 0.3 | latest | ✓ Pass | 17s |
```

**Table C: Known Issues** (failing combinations with reasons)
```markdown
| Python | MCP | A2A | ACP | Issue | Workaround |
|--------|-----|-----|-----|-------|-----------|
| 3.13   | 1.1 | 0.3 | latest | Timeout in heartbeat | Use A2A 0.2 |
```

3. Write all three tables to `docs/compatibility.md`
4. Write summary JSON to `docs/compatibility-summary.json` (for badge generation and programmatic consumption)

**Timeout**: 10s
**Input**: `{ results: list[dict], baseline: dict }`
**Output on SUCCESS**: `{ tables_generated: 3, docs_written: dict, summary_json: dict }` → GO TO STEP 3
**Output on FAILURE**:
- `FAILURE(script_error)`: `generate_compatibility_table.py` raises exception → [recovery: log error with traceback, fail workflow]
- `FAILURE(invalid_results_structure)`: Results don't match expected schema → [recovery: log error, fail workflow]

**Observable states during this step**:
- Operator sees: Script execution job running
- Logs: `[compat-gen] Generating 3 compatibility tables...` → `[compat-gen] Tables generated successfully`

---

### STEP 3: Update README with Compatibility Badges
**Actor**: GitHub Actions (bash script)
**Action**:
1. Read `docs/compatibility-summary.json`
2. Extract protocol versions and compatibility status
3. Generate badge URLs for each protocol:
   - `mcp`: `https://img.shields.io/badge/mcp-1.0%2C%201.1-blue`
   - `a2a`: `https://img.shields.io/badge/a2a-0.2%2C%200.3-blue`
   - `acp`: `https://img.shields.io/badge/acp-latest-brightgreen`
4. Update README.md badges section (or insert if missing):
   ```markdown
   ## Protocol Compatibility

   ![MCP](https://img.shields.io/badge/mcp-1.0%2C%201.1-blue)
   ![A2A](https://img.shields.io/badge/a2a-0.2%2C%200.3-blue)
   ![ACP](https://img.shields.io/badge/acp-latest-brightgreen)

   [See full compatibility matrix](docs/compatibility.md)
   ```

**Timeout**: 10s
**Input**: `{ summary_json: dict, readme_path: str }`
**Output on SUCCESS**: `{ badges_inserted: int, readme_updated: bool }` → GO TO STEP 4
**Output on FAILURE**:
- `FAILURE(summary_missing)`: `compatibility-summary.json` not found → [recovery: fallback to text badges, continue]
- `FAILURE(readme_not_found)`: README.md doesn't exist → [recovery: fail workflow with clear error]
- `FAILURE(markdown_parse)`: Cannot parse README.md → [recovery: log error, fail workflow]

**Observable states during this step**:
- Operator sees: Badge update job running
- Logs: `[badge] Updating README badges...` → `[badge] Updated 3 badges`

---

### STEP 4: Commit and Push Documentation
**Actor**: GitHub Actions (git automation)
**Action**:
1. Check git status: files changed = [`docs/compatibility.md`, `docs/compatibility-summary.json`, `README.md`]
2. Configure git identity:
   ```bash
   git config user.name "bernstein[bot]"
   git config user.email "bernstein-bot@users.noreply.github.com"
   ```
3. Add files: `git add docs/compatibility.md docs/compatibility-summary.json README.md`
4. Create commit: `git commit -m "docs: Update protocol compatibility matrix and badges"`
5. Push to main: `git push origin main`
6. Capture commit SHA

**Timeout**: 30s
**Input**: `{ files_to_commit: list[str], branch: str }`
**Output on SUCCESS**: `{ commit_sha: str, push_status: "success", files_committed: int }` → GO TO STEP 5
**Output on FAILURE**:
- `FAILURE(no_changes)`: No files changed (nothing to commit) → [recovery: log "No changes detected", skip commit, continue to STEP 5 as success]
- `FAILURE(git_push)`: Push to main fails (e.g., force-push conflict) → [recovery: retry x2 with 10s backoff → if fails, create issue for manual resolution]
- `FAILURE(auth)`: Git authentication fails → [recovery: fail workflow with "Git auth failed", check token permissions]

**Observable states during this step**:
- Git: New commit appears on main branch with badge/table updates
- GitHub: Commit history shows "bernstein[bot]" author
- Logs: `[git] Committing compatibility updates...` → `[git] Pushed commit abc123def to main`

---

### STEP 5: Verify Documentation Renders
**Actor**: GitHub Actions (verification job)
**Action**:
1. Verify `docs/compatibility.md` exists and contains valid markdown
2. Check that all three tables are present (by searching for markdown table markers)
3. Verify README.md contains badge URLs (by checking for img.shields.io)
4. Log summary of what was published

**Timeout**: 10s
**Input**: `{ docs_path: str }`
**Output on SUCCESS**: `{ verification_passed: bool, docs_size: int, tables_found: int }` → WORKFLOW COMPLETE
**Output on FAILURE**:
- `FAILURE(file_missing)`: `compatibility.md` not found after push → [recovery: log error, fail workflow, no cleanup needed]
- `FAILURE(invalid_markdown)`: Tables are malformed → [recovery: log error, fail workflow]

**Observable states during this step**:
- Operator sees: Verification job running
- GitHub: Rendered markdown is readable at `docs/compatibility.md` in GitHub UI
- Logs: `[verify] Documentation verification passed. 3 tables found, 2 badges updated.`

---

## State Transitions

```
[pending]
  → (all steps 1-5 succeed)
    → [success] (documentation published, workflow complete)

[pending]
  → (artifact not found or download fails)
    → [failure_missing_input] (previous workflow must provide results)

[pending]
  → (git push fails with conflict)
    → [failure_manual_review] (operator must investigate git conflict)

[pending]
  → (script or markdown error)
    → [failure_infrastructure] (retry or fix script)
```

---

## Handoff Contracts

### Previous Workflow (Protocol Matrix) → This Workflow (Table Generation)
**Endpoint**: GitHub Actions `workflow_dispatch` event
**Payload**:
```json
{
  "workflow_id": "compatibility-table-generation.yml",
  "inputs": {
    "results_artifact": "protocol-compat-results.json",
    "commit_sha": "abc123def456",
    "trigger_event": "test_complete"
  }
}
```
**Success**: Workflow run created
**Failure**: Workflow not found or cannot be triggered
**Timeout**: 5s

---

### Script Output → Git Commit
**Payload**: Files generated by `scripts/generate_compatibility_table.py`
```
docs/compatibility.md        — 3 markdown tables
docs/compatibility-summary.json  — JSON summary for badges
```
**Expected structure**:
```json
{
  "generated_at": "2026-03-29T14:40:00Z",
  "mcp_versions": ["1.0", "1.1"],
  "a2a_versions": ["0.2", "0.3"],
  "acp_versions": ["latest"],
  "total_tests": 8,
  "passed": 7,
  "failed": 1,
  "compatibility": { "mcp": { "1.0": true, "1.1": true }, ... }
}
```
**Timeout**: 10s

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| `docs/compatibility.md` | Step 2 | Manual deletion or branch cleanup | Git history preserved |
| `docs/compatibility-summary.json` | Step 2 | Manual deletion or branch cleanup | Git history preserved |
| README.md badges | Step 3 | Manual cleanup if needed | Revert commit if needed |
| Git commit | Step 4 | Manual revert if needed | Revert or amend commit |

No automatic cleanup — documentation is meant to persist and be updated on each test run.

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `scripts/generate_compatibility_table.py` does not exist | High | Step 2 | Must create this script as part of task completion |
| RC-2 | `docs/compatibility.md` does not exist | Medium | All steps | Will be created by this workflow on first run |
| RC-3 | README.md badge section may not exist | Medium | Step 3 | Script should handle insertion of new section if needed |
| RC-4 | Git push permissions may not be configured | High | Step 4 | Verify GitHub Actions has write permissions on main branch |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path | Results downloaded, tables generated, committed | `docs/compatibility.md` updated, README badges updated, commit pushed |
| TC-02: No changes (same results as before) | Results identical to previous run | Workflow skips commit, but completes successfully |
| TC-03: Artifact missing | Results artifact expired or not found | Workflow fails at step 1 with clear error message |
| TC-04: Script error | `generate_compatibility_table.py` raises exception | Workflow fails at step 2, error logged, no commit made |
| TC-05: Git push conflict | Another commit pushed to main during workflow | Retry logic handles conflict, or operator is notified |
| TC-06: README missing | README.md not found | Workflow fails at step 3, operator must restore README |
| TC-07: Malformed markdown | Generated tables have invalid markdown syntax | Step 5 verification fails, operator must investigate script |
| TC-08: New protocol version detected | A2A 0.4 now available and tested | Table includes new version, badges updated |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Results artifact is available from previous workflow run | artifact download step | If artifact expired/missing, workflow fails; mitigation: extend retention |
| A2 | `scripts/generate_compatibility_table.py` is idempotent (same input → same output) | Not verified | Multiple runs may cause duplicate commits; mitigation: add idempotency check |
| A3 | GitHub Actions has write permissions on main branch | GitHub Actions settings | Push will fail; mitigation: verify permissions before first run |
| A4 | Markdown table format is consistent across Python versions | Not verified | Different Python versions may generate different formatting; mitigation: use template-based generation |
| A5 | badge URLs (img.shields.io) remain stable and accessible | External service | Badges may break if service down; mitigation: fallback to text badges |

---

## Open Questions

- Should the compatibility table include test duration or just pass/fail status?
- Should we automatically update the `compatibility-baseline.json` after successful run, or require manual approval?
- Should failed combination details (error messages) be included in the published table, or only in logs?
- How should version deprecation be handled (e.g., dropping support for MCP 1.0)?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-29 | Initial spec created | — |
| (pending) | Reality Checker pass | TBD |

