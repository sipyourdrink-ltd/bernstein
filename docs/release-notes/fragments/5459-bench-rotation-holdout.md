# Release notes fragment: Bench suite rotation, private holdout, and contamination check (#5459)

### Features
- **Versioned Manifests & Holdout Binding**: Added `holdout_hash` binding to `BenchSuite` and `SubmissionBundle`, allowing benchmark suites to cryptographically bind private holdout evaluation sets without publishing secret task specifications.
- **Private Holdout Runner & Isolation**: Introduced `HoldoutBenchRunner` with `HoldoutIsolationError` enforcing local-only execution and preventing public emission of holdout datasets and results by construction.
- **Contamination Check & Admission Gate**: Added n-gram fingerprinting (`check_solution_contamination`, `admit_task`) to detect and reject benchmark tasks whose reference solutions exist verbatim or with high n-gram overlap in public corpora.
- **Suite Saturation & Rotation Detection**: Added `check_suite_saturation` and `Leaderboard.check_rotation_due` to track baseline trajectory and trigger rotation alerts when public pass rate exceeds 90% across 3 consecutive baselines.
