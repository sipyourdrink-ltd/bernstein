# CI fix for PR #7645 / issue #6943

Verdict: branch-caused failure, fixed locally.

## Cause and evidence

Inspected the required checks using read-only GitHub CLI commands:

```sh
gh pr checks 7645 -R sipyourdrink-ltd/bernstein
gh run view 43627694429 -R sipyourdrink-ltd/bernstein --log-failed
```

The run tested branch head `354bfd3dd4440751854bd24c95b1fcd86bcba946`.
[Ubuntu Python 3.13 shard 4](https://github.com/sipyourdrink-ltd/bernstein/actions/runs/43627694429/job/132547468901)
failed in `tests/unit/test_adapter_contract.py::test_module_collected_case_count`
at the assertion comparing the collected count with `_EXPECTED_COLLECTED`.
[CI gate](https://github.com/sipyourdrink-ltd/bernstein/actions/runs/43627694429/job/132550178066)
then failed solely because `test: result=failure`. Repo hygiene passed.

This branch adds `tests/golden/pi_adapter.yaml`. The contract suite discovers
replay-capable golden files dynamically and parameterizes one test per file
(both Pi steps run inside that one case). This adds one collected case, raising
the total from 720 to 721, but the collection guard still expected 720.
Local reproduction before the fix failed with `assert 721 == 720`
(`1 failed in 2.53s`). This is attributable to the branch, not a main/flaky
failure; `CI-NOT-OURS` does not apply.

## Change

Updated `_EXPECTED_COLLECTED` from 720 to 721 and added a comment tying the
increment to the Pi golden replay. The strict collection guard is preserved.
No production code or workflow changes were needed.

## Test results

Used the existing local environment (macOS, Python 3.14.3), with
`UV_OFFLINE=1`, `UV_NO_SYNC=1`, `PYTHONDONTWRITEBYTECODE=1`, and uv cache and
temporary directories under `$PWD/.cache/cifix/`.

| Command | Result |
| --- | --- |
| `uv run pytest tests/unit/test_adapter_contract.py::test_module_collected_case_count --basetemp=.cache/cifix/pytest-before -q` (before fix) | 1 failed: 721 != 720 |
| `uv run pytest tests/unit/test_adapter_contract.py --basetemp=.cache/cifix/pytest-contract-after -q` | 717 passed, 4 skipped in 6.84s |
| `uv run pytest tests/unit/test_adapter_pi.py --basetemp=.cache/cifix/pytest-pi-after -q` | 7 passed in 0.36s |
| `uv run ruff check tests/unit/test_adapter_contract.py` | Passed |
| `uv run ruff format --check tests/unit/test_adapter_contract.py` | Passed |
| `git diff --check` | Passed |

The full contract module includes the collection guard and Pi golden replay.
Hosted Ubuntu/Python 3.13 CI was not rerun. No push, comment, label, live agent
invocation, or provider call was performed. Existing untracked input artifacts
were preserved. The checkout and branch are retained for the owner's next step.
