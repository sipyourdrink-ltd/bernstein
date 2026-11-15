## Wait completion awaits journal finalization (#6089)

- Require `_wait_for_run_completion()` to confirm the active foreground run's journal has recorded `run_completed` (and completed any active `.finalizing` -> `.finalized` phase) before returning a quiescent verdict.
- Add regression tests in `tests/unit/test_run_wait_flag.py`.
