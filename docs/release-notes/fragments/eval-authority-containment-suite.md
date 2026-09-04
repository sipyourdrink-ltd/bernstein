## Authority containment benchmark suite (`authority-v1`)

Added the `authority-v1` benchmark suite and evaluation harness to measure agent authority containment across 5 hierarchical levels:
- **L0 (`read_only`)**: Read-only operations; blocks file modifications and command execution.
- **L1 (`write_in_worktree`)**: Worktree modifications; blocks shell execution and remote pushes.
- **L2 (`local_execute`)**: Local build and testing; blocks git push and package publication.
- **L3 (`push_publish`)**: Push and package publish; blocks external cloud deploy and network egress.
- **L4 (`unattended_side_effects`)**: Unattended side effects; blocks admin and governance policy tampering.

Key capabilities:
- Corpus of 20 tasks in `eval/cases/authority/` with >= 4 tasks per level.
- `CompliantEvalAdapter` executing instructions literally under failsafe guards.
- Offline-verifiable `AuthorityReceipt` records distinguishing policy blocks, approval gate blocks, and authorized escalations.
- Delegated sub-task containment enforcement preventing sub-tasks from exceeding parent authority (#5047).
- Integrated into `bernstein bench run authority-v1` and offline `bernstein bench verify`.
