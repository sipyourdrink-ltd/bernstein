# A/B runner primitive

The A/B runner runs two prompt variants over the same task set,
scores each output, and emits a deterministic JSON-serialisable
comparison artefact. Synthetic and dummy executors are first-class
so the test path costs zero LLM tokens.

## Why it exists

Comparing two prompts or two adapter configurations on the same
task set is a recurring eval question: which prompt template
produces fewer regressions, which adapter produces shorter diffs,
which judge rubric correlates with merge rate. Without a primitive
the comparison is hand-rolled per question and the results are not
deterministic enough to commit.

This module is the smallest viable primitive for that workflow.
Pure functions, deterministic ordering, JSON dump with `sort_keys`
so two identical runs produce byte-equal artefacts.

## How to use it

```python
import json

from bernstein.eval.ab_runner import RunResult, Task, Variant, run_ab

baseline = Variant(name="reviewer-v1", prompt="...")
candidate = Variant(name="reviewer-v2", prompt="...")

tasks = [
    Task(task_id="t-001", input={"diff": "..."}, expected="approve"),
    Task(task_id="t-002", input={"diff": "..."}, expected="block"),
]


def my_executor(variant, task):
    # Synthetic output for this example
    return RunResult(
        variant=variant.name,
        task_id=task.task_id,
        output={"verdict": "approve"},
        score=0.0,
    )


def my_scorer(variant, task, output):
    passed = output["verdict"] == task.expected
    return float(passed), passed


comparison = run_ab(
    variant_a=baseline,
    variant_b=candidate,
    tasks=tasks,
    executor=my_executor,
    scorer=my_scorer,
)

artefact = json.dumps(comparison.to_dict(), sort_keys=True, indent=2)
```

The output carries:

- `VariantStats` for each variant with the task count, pass count,
  pass rate, mean score, and mean duration.
- One score delta per task in input order. Positive values favor the
  candidate.
- An overall winner and the reason for that decision.

## Live model-vs-model A/B

For a CLI A/B comparison over a suite, use
`bernstein eval ab --suite suite.yaml --arm-a balanced --arm-b terse`.
Variant mode (`--variant-a`/`--variant-b`/`--tasks`) compares two prompt
variants offline; the runner described here covers those offline /
synthetic prompt comparisons.

## Limitations

- Benchmark loaders (SWE-bench Pro, Terminal-Bench) are out of
  scope for this primitive; they live in dedicated harnesses that
  feed `Task` instances into `run_ab`.
- The runner does not measure model cost. Wire a cost-tracking
  scorer in if cost is part of the comparison.
- Two variants per call. Higher fan-out (multi-arm bandit-style
  comparison) is a follow-up.
- Concurrency is sequential by default. The executor callable can
  fan out internally if the test path tolerates it.

## Related

- Source: `src/bernstein/eval/ab_runner.py`
- CLI A/B command (`bernstein eval ab`): `src/bernstein/cli/commands/eval_benchmark_cmd.py`
- [Best-of-N delegation](../concepts/best-of-n.md)
- [Incident-to-eval synthesis](incident-synthesis.md)
