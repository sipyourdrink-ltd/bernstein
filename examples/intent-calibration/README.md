# Replay recorded intent calibration

Run the example from the repository root.

```sh
uv run python examples/intent-calibration/demo.py
```

Two synthetic rows pair recorded probabilities with observed outcomes under
`intent/v1`. The example gives `GoalDriftReplayAdapter` the same in-scope
edit twice, once with the rows and once with the calibration field omitted.
All inputs are held in memory.

Each receipt makes a JSON round trip. A fresh adapter calls `score_task` on
the restored receipt. The output shows the recomputed `summary` and whether
it matches the original. With the rows, the sample count is two and the Brier
score is 0.0625. With the field omitted, the summary is unavailable and the
sample count is zero. The hard drift gate passes for the recorded edit in
both cases.

```sh
uv run pytest -q tests/unit/examples/test_intent_calibration_demo.py
```
