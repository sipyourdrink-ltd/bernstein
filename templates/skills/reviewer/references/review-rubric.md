# Review rubric

Grade each PR across these axes. Blockers must all be green before merge.

## Correctness (blocking)
- Behavior matches the task description.
- Edge cases and error paths covered.
- No obvious race conditions or resource leaks.

## Tests (blocking)
- New logic has tests; regression tests added for bug fixes.
- Tests actually assert behaviour (not `assert True`).
- Test runner passes: `uv run python scripts/run_tests.py -x`.

## Types (blocking)
- Pyright strict clean.
- No gratuitous `# type: ignore` without an inline justification.

## Security (blocking when risk is high)
- No secrets in diff.
- Input validation at trust boundaries.
- Auth scopes enforced where expected.

## Style (suggestion)
- Ruff clean.
- Docstrings for public API.
- Conventional commit messages.

## Performance (suggestion unless regression)
- Obvious O(n²) where O(n) is trivial.
- New DB queries indexed / bounded.
