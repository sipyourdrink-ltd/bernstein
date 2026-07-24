# Golden benchmark harness

The golden harness scores agent runs against a curated, tiered task set using
a multiplicative formula, then persists the run so a later `report` or
`failures` call can inspect it without re-running anything. It is the `eval
run` path taken when no YAML spec is given, and is a different code path from
the [YAML eval harness](yaml-harness.md) (which always takes a spec file).

## How to use it

```
bernstein eval run                       # run the full golden suite
bernstein eval run --tier smoke          # smoke tier only
bernstein eval run --compare             # also diff against the previous run
bernstein eval run --no-save             # don't persist results to disk
bernstein eval report                    # markdown-style summary of the last run
bernstein eval failures                  # failure-taxonomy breakdown of the last run
```

`--tier` accepts `smoke`, `standard`, or `stretch` and selects which golden
tiers feed the scoring pass (`adversarial` tasks are included only when
scoring task results directly via the Python API, not through `--tier`).
`report` and `failures` both read the most recently saved run from
`.sdd/eval/runs/eval_run_*.json` — run `eval run --save` (the default) at
least once before calling either.

## Golden tasks

Tasks are markdown files with YAML frontmatter, resolved in two places:

1. Operator overrides at `.sdd/eval/golden/<tier>/*.md`, when that directory
   exists and is non-empty.
2. Packaged defaults shipped in the wheel under
   `bernstein.eval.golden_data.<tier>`, discovered via `importlib.resources`
   — a fresh install has a working `smoke` tier with no operator setup.

Tiers are `smoke`, `standard`, `stretch`, `adversarial`.

## Scoring

```
Score = (0.5*TaskSuccess + 0.3*CodeQuality + 0.2*Efficiency) * Reliability * Safety
```

`Reliability` and `Safety` are multiplicative gates: a crash, an orphaned
agent process, or a test regression anywhere in the run drives the
corresponding gate toward zero and the overall score with it — one
regression is enough to zero the score regardless of how well the other
tasks scored.

- **TaskSuccess** — fraction of tasks whose completion signals all passed
  and which introduced no test failures.
- **CodeQuality** — mean of the LLM-judge verdict scores, when a judge
  verdict was supplied per task (0.0 when none were).
- **Efficiency** — computed from per-task telemetry (`bernstein.eval.metrics.compute_efficiency`).
- **Reliability** — degrades with crash count, orphan count, and any task
  missing telemetry.
- **Safety** — zero when the failure taxonomy recorded any test regression.

`eval report` prints the composite score plus this five-way breakdown and the
per-tier pass rates (smoke/standard/stretch/adversarial).

## Failure taxonomy

Every failed task is classified into one closed-set category
(`bernstein.eval.taxonomy.FailureCategory`): `orientation_miss`,
`scope_creep`, `test_regression`, `incomplete`, `timeout`, `conflict`,
`context_miss`, `hallucination`. `eval failures` prints a table of
task/category/details plus counts per category for the most recent run —
useful for spotting whether a regression clusters around one failure mode
(e.g. every failure being `scope_creep` points at a task-boundary problem
rather than a model-quality problem).

## Persistence

`eval run --save` (the default) writes
`.sdd/eval/runs/eval_run_<UTC-timestamp>.json` containing the score, the
five-way breakdown, per-tier rates, the failure list, and total cost.
`--compare` loads the previous run from the same directory and prints the
score delta. `report` and `failures` always read the latest file in that
directory — they do not take a run ID argument.

## Limitations

- `eval run` without a spec argument only scores tasks for which telemetry
  and (optionally) a judge verdict were already collected — it does not
  itself spawn agents. The orchestrator's own run must populate
  `.sdd/eval/golden/` telemetry first; the CLI command drives scoring, not
  execution.
- `report` and `failures` operate on the single most recent run file; there
  is no `--run-id` selector.

## Source

- `src/bernstein/eval/harness.py` — `EvalHarness`, `EvalResult`, scoring.
- `src/bernstein/eval/golden.py` — golden task loading.
- `src/bernstein/eval/taxonomy.py` — failure taxonomy.
- `src/bernstein/eval/metrics.py` — efficiency/reliability/safety component math.
- `src/bernstein/cli/commands/eval_benchmark_cmd.py` — `eval run` / `eval report` / `eval failures` commands.

## Related

- [YAML eval harness](yaml-harness.md) — the `eval run <spec.yaml>` path,
  for adapter-vs-adapter comparison against a hand-authored spec.
- [A/B runner primitive](ab-runner.md)
