# Workflow Registry — Protocol Compatibility Matrix

**Date**: 2026-03-29
**Scope**: Protocol Compatibility Matrix initiative (Task 5ac221ec4f7f)
**Maintainer**: Workflow Architect

---

## Overview

This registry documents all workflows created for the Protocol Compatibility Matrix initiative. The matrix ensures that Bernstein's support for MCP, A2A, and ACP protocols is tested, documented, and enforced before every release.

---

## View 1: Workflows (Master List)

| Workflow | Spec file | Status | Trigger | Primary actor | Last reviewed |
|---|---|---|---|---|---|
| Protocol Compatibility Testing | `WORKFLOW-protocol-compatibility-matrix.md` | Draft | Every PR, every push to main | GitHub Actions (CI matrix) | 2026-03-29 |
| Compatibility Table Generation | `WORKFLOW-compatibility-table-generation.md` | Draft | After Protocol tests pass (workflow_dispatch) | Python script | 2026-03-29 |
| Release Breaking Change Detection | `WORKFLOW-release-breaking-change-detection.md` | Draft | Git tag v* (release publish) | GitHub Actions (gate check) | 2026-03-29 |

**Status definitions**:
- `Draft`: Spec complete, not yet integrated into CI
- `Review`: Spec under review by Reality Checker or other agents
- `Approved`: Reality Checker pass, ready for implementation
- `Active`: Integrated into CI, running on commits
- `Deprecated`: Replaced by another workflow, kept for historical reference

---

## View 2: Components (Code → Workflows)

| Component | File(s) | Workflows it participates in |
|---|---|---|
| CI Matrix Executor | `.github/workflows/ci.yml` (modified) | Protocol Compatibility Testing |
| Test Fixtures | `tests/protocol/versions.json`, `tests/protocol/` directory | Protocol Compatibility Testing |
| Protocol Test Suite | `tests/protocol/*` (TBD) | Protocol Compatibility Testing |
| Version Baseline | `tests/protocol/compatibility-baseline.json` | Protocol Compatibility Testing, Release Breaking Change Detection |
| Compatibility Table Generator | `scripts/generate_compatibility_table.py` (TBD) | Compatibility Table Generation |
| Compatibility Checker | `scripts/compare_compatibility_baseline.py` (TBD) | Release Breaking Change Detection |
| Documentation | `docs/compatibility.md` (TBD), `docs/compatibility-summary.json` (TBD) | Compatibility Table Generation, Release Breaking Change Detection |
| README | `README.md` (modified) | Compatibility Table Generation |
| Publish Workflow | `.github/workflows/publish.yml` (modified) | Release Breaking Change Detection |
| MCP Support | `src/bernstein/core/mcp_registry.py`, `src/bernstein/core/mcp_manager.py` | Protocol Compatibility Testing |
| A2A Support | `src/bernstein/core/a2a.py` | Protocol Compatibility Testing |

---

## View 3: User Journeys (User-facing → Workflows)

### Operator Journeys

| What the operator does | Underlying workflow(s) | Entry point | Observable outcome |
|---|---|---|---|
| Opens a PR with code changes | Protocol Compatibility Testing | GitHub Pull Request | Checks section shows "Protocol Compat Matrix" job, 8 test jobs in parallel |
| Pushes to main (after code merged) | Protocol Compatibility Testing | `git push origin main` | CI runs full matrix, generates compatibility summary |
| Views protocol compatibility status | Compatibility Table Generation | `docs/compatibility.md` | Three markdown tables show which protocol versions work together |
| Reviews README protocol badges | Compatibility Table Generation | `README.md` (Protocol Compatibility section) | Badges show supported protocol versions (e.g., "MCP 1.0, 1.1") |
| Tags a new release | Release Breaking Change Detection | `git tag v1.0.1 && git push --tags` | Publish workflow runs gate check before releasing to PyPI |
| Discovers breaking change before release | Release Breaking Change Detection | GitHub Actions Checks (on tag push) | Release blocked with issue created, breaking changes listed |
| Acknowledges breaking change deliberately | Release Breaking Change Detection | Release notes (add "ACKNOWLEDGE_BREAKING_CHANGES") | Gate passes with warning, release proceeds |

### System-to-System Journeys

| What happens automatically | Underlying workflow(s) | Trigger | Entry point |
|---|---|---|---|
| New protocol version released to PyPI | Protocol Compatibility Testing | GitHub Actions schedule (or manual trigger) | Matrix expands to test new version automatically |
| Compatibility table auto-updates | Compatibility Table Generation | Previous workflow success | Commit pushed to main with updated docs |
| Test results published | Compatibility Table Generation | Protocol tests complete | JSON artifact published, markdown table generated |
| Breaking change detected before publish | Release Breaking Change Detection | Git tag on release commit | Publish workflow blocked, operator notified |

---

## View 4: State Map (Entity States & Transitions)

### PR/Commit States (Protocol Testing)

| State | Entered by | Exited by | Workflows triggering exit |
|---|---|---|---|
| `pending` | PR created / commit pushed | → `testing`, `blocked` | Protocol Compatibility Testing starts |
| `testing` | Matrix job started | → `pass`, `fail` | All 8 matrix jobs complete |
| `pass` | All 8 protocol tests succeed | → `table_gen`, `merged` | Tests passed, can merge to main |
| `fail` | Any matrix job fails (but not timeout/incompatible) | → `retest` | Developer fixes code, pushes again |
| `incompatible` | Protocol version combination found unsupported | → `pass` (if acceptable), `blocked` | Table generation or retry |
| `blocked` | Critical test infrastructure failure | → `pass`, `fail` (after fix) | Infrastructure restored |

### Release States (Breaking Change Detection)

| State | Entered by | Exited by | Workflows triggering exit |
|---|---|---|---|
| `release_pending` | Git tag v* created | → `gate_check`, `publish_queued` | Release Publish workflow triggered |
| `gate_check` | Publish workflow checks compatibility | → `gate_pass`, `gate_fail`, `gate_acknowledged` | Gate check completes |
| `gate_pass` | No breaking changes found | → `publish_queued` | Release Breaking Change Detection succeeds |
| `gate_fail` | Breaking changes found, not acknowledged | → `gate_reviewed` | Operator reviews issue and decides |
| `gate_acknowledged` | Operator adds keyword to release notes | → `publish_queued` | Operator acknowledges breaking changes |
| `publish_queued` | Gate passed or acknowledged | → `published` | Release to PyPI begins |
| `published` | PyPI release succeeds | (terminal) | Release complete |
| `gate_reviewed` | Operator investigates breaking change | → `gate_fail_override`, `fix_attempted` | Operator decision or code fix |
| `fix_attempted` | Developer fixes incompatibility | → `gate_pass` (retest) | New tag pushed with fixes |

---

## Workflow Dependencies & Ordering

```
┌─────────────────────────┐
│  Protocol Compatibility │
│  Testing                │
│  (every PR, every push) │
└────────┬────────────────┘
         │
         ├─→ Test matrix (8 jobs parallel)
         │   ├─ Python 3.12 + MCP 1.0 + A2A 0.2
         │   ├─ Python 3.12 + MCP 1.0 + A2A 0.3
         │   ├─ ... (8 combinations)
         │   └─ Results → artifacts
         │
         └─→ Aggregation & Status Check
             ├─ If PASS (no breaking changes)
             │   └─→ workflow_dispatch
             │       ├─ Compatibility Table Generation
             │       │   ├─ Download results
             │       │   ├─ Generate 3 tables
             │       │   ├─ Update README badges
             │       │   └─ Commit & push
             │       └─ Ends with docs/compatibility.md updated
             │
             └─ If FAIL (breaking change or error)
                 └─→ Block → Manual review required

┌──────────────────────────┐
│  Release Process         │
│  (on git tag v*)         │
└────────┬─────────────────┘
         │
         └─→ Publish Workflow
             ├─ Build
             └─→ Release Breaking Change Detection
                 ├─ Run current tests
                 ├─ Fetch previous baseline
                 ├─ Compare (step 3)
                 ├─ Gate decision (step 4)
                 │   ├─ If PASS → publish to PyPI
                 │   ├─ If FAIL → block release, create issue
                 │   └─ If ACKNOWLEDGED → publish with warning
                 └─ Report findings
```

---

## Critical Paths & SLAs

| Path | Steps | Max duration | Failure mode | Recovery |
|---|---|---|---|---|
| PR → Tests → Merge | Protocol tests, aggregation, status check | 5-10 minutes (120s tests + overhead) | Test timeout | Retry or debug |
| Main push → Docs updated | Protocol tests + table generation | 10-15 minutes | Artifact missing | Re-trigger workflow |
| Release tag → Published | Gate check + PyPI upload | 5-10 minutes (if gate pass) | Breaking change found | Operator review + acknowledge |

---

## Integration Checklist

Before these workflows can be marked `Approved`:

**Prerequisites to implement**:
- [ ] Create `tests/protocol/` directory
- [ ] Create `tests/protocol/versions.json` with MCP/A2A version matrix
- [ ] Create `tests/protocol/compatibility-baseline.json` (initial baseline)
- [ ] Write protocol-specific test fixtures in `tests/protocol/*.py`
- [ ] Create `scripts/generate_compatibility_table.py`
- [ ] Create `scripts/compare_compatibility_baseline.py`
- [ ] Modify `.github/workflows/ci.yml` to include protocol matrix job
- [ ] Modify `.github/workflows/publish.yml` to call release gate check
- [ ] Create `docs/compatibility.md` template (or auto-create on first run)
- [ ] Add protocol compatibility badge section to `README.md`

**Reality Checker pass required**:
- [ ] WORKFLOW-protocol-compatibility-matrix.md
- [ ] WORKFLOW-compatibility-table-generation.md
- [ ] WORKFLOW-release-breaking-change-detection.md

**CI integration required**:
- [ ] All three workflows run on test commits without errors
- [ ] Matrix tests execute in parallel, complete within SLA
- [ ] Compatibility table auto-generates and publishes
- [ ] Release gate blocks on breaking changes as designed

**Documentation required**:
- [ ] README documents protocol version support
- [ ] docs/compatibility.md is auto-generated and published
- [ ] docs/CHANGELOG.md references breaking changes with acknowledgment
- [ ] Release guide documents how to acknowledge breaking changes

---

## Known Gaps & Open Questions

### Critical (blocks approval)
- [ ] Test fixtures for protocol versions don't exist yet
- [ ] Scripts for table generation and baseline comparison don't exist
- [ ] CI workflow integration points not yet implemented

### High (should resolve before Active)
- [ ] Policy for supporting deprecated protocol versions unclear
- [ ] Release acknowledgment mechanism (keyword in release notes) not documented
- [ ] First run baseline initialization not specified

### Medium (resolve before production)
- [ ] Operator journey for breaking change investigation not documented
- [ ] Fallback for badge generation (if shields.io down) needs implementation
- [ ] Cross-protocol incompatibility scenarios (e.g., MCP 1.1 + A2A 0.2) not fully explored

### Low (nice-to-have)
- [ ] Performance benchmarking of matrix tests
- [ ] Multi-language support for compatibility tables
- [ ] Historical trend analysis (when did versions become incompatible?)

---

## Maintenance & Review Schedule

| Task | Frequency | Owner | Last completed |
|---|---|---|---|
| Verify protocol versions still available on PyPI | Monthly | Release Engineer | — |
| Update compatibility baseline on breaking changes | Per release | Release Engineer | — |
| Review this registry for accuracy | Quarterly | Workflow Architect | — |
| Audit test coverage of protocol matrix | Quarterly | QA | — |

---

## Audit Trail

| Date | Event | Details |
|---|---|---|
| 2026-03-29 | Spec draft created | 3 workflows drafted, registry created, ready for Reality Checker review |
| (pending) | Reality Checker review | TBD |
| (pending) | Implementation begins | TBD |
| (pending) | CI integration | TBD |
| (pending) | First release gate test | TBD |

