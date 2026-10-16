## agent_test_mutation refuses mocks that do not name what they stand for

A changed test that built a bare `MagicMock()` could assert on an attribute
the production code does not have and still pass, because the mock answers
to any name. The `tests` gate saw a passing test and passed the change.

`agent_test_mutation` now checks the changed tests before it runs any mutant.
Each `Mock`, `MagicMock`, `AsyncMock`, `NonCallableMock` or
`NonCallableMagicMock` must be given a spec, and each `patch()` of a
first-party target must pass `autospec=` (or `spec=`, `spec_set=`, `new=`,
`new_callable=`). Otherwise the gate fails with one
`path:line: mock without a spec: ...` line per mock. The gate is off by
default; a pipeline that enables it can see new failures on tests that use
spec-less mocks. Use `create_autospec(...)` or `patch(..., autospec=True)`.

The gate-evasion suite now routes `nonexistent_api_mock_test` to
`agent_test_mutation` and catches it: the committed baseline moves from 2/8
to 3/8.
