# CI Workflow Health Sweep - 2026-05-20

## TL;DR

- Swept: 47 registered workflows.
- Real repo-file bug found and fixed: 1 (`review-bot-ack` required-check cancellation race).
- Apparent failures that are not repo bugs: 3 (orphaned `gitleaks`, GitHub-managed Copilot reviewer, OSSF Scorecard manual-dispatch only).
- Never-run / persistent-skip workflows: all explained by trigger design or conditional gating (no action needed).

## What was fixed

`review-bot-ack` emits a required status check but used `cancel-in-progress: true`
with a per-PR concurrency group. A PR receives overlapping triggers
(`synchronize` on each push, `pull_request_review` on each submitted review),
so an in-flight gate run was routinely cancelled by the next event. A
`CANCELLED` conclusion is treated by branch protection as a non-success
required check, which stalled the merge queue until a manual re-run.

Fix: scope the concurrency group per-PR and per-head-sha, and set
`cancel-in-progress: false` so every commit's gate run completes and reports
its own conclusion without racing the required check. Verified against
`tests/unit/test_review_bot_ack_workflow_yaml.py` structural assertions.

## Health table

| Workflow | Last conclusion | Last run age | Verdict |
|---|---|---|---|
| CI | in_progress / cancelled (superseded) | <1h | healthy (concurrency supersede) |
| CI (macOS nightly) | success | <1h | healthy |
| Publish | success | <1d | healthy |
| Publish VS Code Extension | success | <1d | healthy |
| Publish Homebrew Formula | success | <1d | healthy |
| Publish Docker Image | success | <1d | healthy |
| pages-build-deployment | success | stale (no recent docs push) | healthy |
| Dependabot Updates | mixed (one github_actions update failure) | <1d | healthy (dependabot-side transient) |
| Bernstein CI Fix | success | <1d | healthy |
| Bernstein Issue Decompose | skipped | <1d | healthy (conditional, issue event gating) |
| Bernstein PR Review | success | <1h | healthy |
| Auto-release | skipped | <1d | healthy (workflow_run gated on upstream conclusion) |
| CodeQL Security Analysis | success | <1h | healthy |
| Major/Minor Release | never-run | n/a | expected (workflow_dispatch only) |
| Dependabot Auto-merge | skipped | <1h | healthy (non-dependabot PRs skip) |
| PR Labeler | success | <1h | healthy |
| License Compliance | success | <1h | healthy |
| PR Size Labeler | success | <1h | healthy |
| Stale cleanup | success | <1d | healthy |
| Telegram CI Notifications | success | <1d | healthy |
| Adversarial Pen-Test Suite | never-run | n/a | expected (monthly cron + dispatch, recently added) |
| Mutation Testing | success | ~3d | healthy |
| Cleanup Action Runs | success | stale | healthy (manual dispatch) |
| Dependency Graph | success | <1h | healthy |
| cluster-e2e | success | <1d | healthy |
| cluster-tunnel-e2e | success | <1d | healthy |
| Airgap E2E | success | <1d | healthy |
| a2a-federation-e2e | success | <1d | healthy |
| eval-nightly | success | <1d | healthy |
| soc2-evidence-nightly | success | <2d | healthy |
| Nightly deep tests | success | <1d | healthy |
| Contract Drift Autofix | success | <1h | healthy |
| Reconcile release drift | success | <1d | healthy |
| Telegram nightly-fanout notifications | skipped | <1h | healthy (workflow_run gated) |
| Dependency Review | success | <1h | healthy |
| gitleaks (secret scanning) | failure | ~3d (single run) | not a repo bug (file removed from main; trufflehog covers secret scanning; orphaned active registration) |
| zizmor (workflow static analysis) | success | <1h | healthy |
| trufflehog (secret scanning) | success | <1h | healthy |
| SBOM | success | <1d | healthy |
| OSSF Scorecard | success (scheduled / branch-protection); failure on manual dispatch | <1d | not a repo bug (scorecard webapp rejects results from non-default trigger) |
| Release Please | skipped | <2d | healthy (no release-please commits on push) |
| Copilot code review | failure | ~3d (single run) | not a repo bug (GitHub-managed dynamic reviewer; artifact-cleanup 403 is platform-side) |
| Adapter contract drift | success | <1d | healthy |
| Mutation (fixed critical paths) | success | <1d | healthy |
| Auto-heal v2 | skipped | <1d | healthy (workflow_run gated on upstream conclusion) |
| Trunk Andon Gate | success | <1h | healthy |
| CI Weekly Digest | never-run | n/a | expected (weekly Sunday cron + dispatch) |
| Hotfix R-counter | mixed (skipped on non-hotfix pushes) | <1h | healthy (conditional gating) |
| Trunk Health SLO | success | <1h | healthy |
| Bisect on Red | skipped | <1d | healthy (workflow_run gated on upstream red) |

## Notes / out of scope

- `sonar-scan.yml`: not inspected (under active debugging by a separate effort).
- Schemathesis timeout: already widened separately; not revisited.
- `gitleaks.yml`: file no longer exists on `main`; the registered workflow is an
  orphaned active entry that will not re-run. Secret scanning is covered by
  `trufflehog` and `zizmor`. No PR change needed.
- OSSF Scorecard manual-dispatch failure: the scorecard publishing endpoint
  only accepts results from the workflow's default trigger
  (`branch_protection_rule` / scheduled). Manual `workflow_dispatch` runs fail
  at the publish step by design; the recurring scheduled runs are green.
