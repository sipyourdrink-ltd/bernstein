# CI test-shard timings

`test-shard-durations.json` maps each `tests/unit/**/test_*.py` path to its
last measured subprocess wall time in seconds. `scripts/run_tests.py --shard`
reads this file to duration-balance the four CI shards (issue #4840).

Refresh from a successful merge-group (or push) CI run:

```
uv run python scripts/refresh_test_shard_durations.py --run-id <github-run-id>
```
