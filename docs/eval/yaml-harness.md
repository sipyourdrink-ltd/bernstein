# YAML eval harness

The YAML eval harness is the operator-runnable face of `src/bernstein/eval/`.
It loads a spec, fans the prompts across one or more adapters, scores each
output with a deterministic golden-dataset check plus an optional
LLM-as-judge, and writes a JSON + markdown report alongside a lineage tag.

The harness lives in `src/bernstein/eval/yaml_runner.py`. The CLI surface is
`bernstein eval run <spec.yaml>`, `bernstein eval list`, and
`bernstein eval diff <run-a> <run-b>`.

## Why YAML

Bernstein already had the primitives. The YAML wrapper is integration
glue: the spec is small enough to keep in git next to the prompts and large
enough to reproduce a full eval suite end to end, offline.

## TL;DR

| Step | Command |
| --- | --- |
| Author a spec | edit `eval/specs/my-suite.yaml` |
| Run it | `bernstein eval run eval/specs/my-suite.yaml` |
| List previous runs | `bernstein eval list` |
| Diff two runs | `bernstein eval diff <run-a>.json <run-b>.json` |

## Spec schema

```yaml
name: claude-vs-codex-smoke           # required, human-readable
lineage_tag: claude-vs-codex-smoke    # tag written into the lineage stub
dataset: prompts.jsonl                # optional JSONL with extra prompts
adapters:                             # at least one adapter id
  - claude
  - codex
prompts:                              # inline prompt list, merged with dataset
  - id: hello
    text: |
      Say "hello world" exactly once.
    expected_output_contains:
      - hello world
    expected_output_regex: "^hello world\\s*$"
    tags: [smoke]
judge:                                # optional - omit to skip judge scoring
  model: anthropic/claude-sonnet-4
  provider: openrouter_free
  rubric: |
    Score correctness (0-1). The output should match the assertion exactly.
  weight: 0.4                         # blend factor in the overall score
thresholds:                           # all minimums in [0.0, 1.0]
  golden_pass_rate_min: 0.8
  judge_score_min: 0.7
  overall_score_min: 0.75
```

The schema is enforced by Pydantic v2 with `extra="forbid"` so typos surface
immediately.

### Prompt assertions

A prompt passes the golden check iff:

1. Every entry in `expected_output_contains` is a substring of the output.
2. `expected_output_regex` (if set) matches the output via `re.search`.

Both fields are optional - a prompt with neither always passes the golden
check but can still be scored by the judge.

### Dataset JSONL

Each line is one `PromptSpec` with the same field set. Inline `prompts` and
dataset entries are merged in order; duplicate ids raise an error.

```jsonl
{"id": "d1", "text": "...", "expected_output_contains": ["..."]}
{"id": "d2", "text": "...", "expected_output_regex": "..."}
```

## CLI

### `bernstein eval run <spec.yaml>`

Executes the spec. Without arguments, `eval run` keeps the legacy golden-suite
behaviour. With a positional spec path, it switches to YAML mode.

```
bernstein eval run eval/specs/my-suite.yaml
bernstein eval run eval/specs/my-suite.yaml --output out.json
bernstein eval run eval/specs/my-suite.yaml --no-save        # stdout only
```

The command:

1. Validates the YAML spec.
2. Runs every (prompt, adapter) pair.
3. Aggregates per-adapter golden pass rate, judge mean, and overall score.
4. Applies thresholds; exits non-zero on any failure.
5. Persists JSON + markdown under `.sdd/eval/yaml_runs/` and writes a sibling
   `*.lineage.json` stub.

### `bernstein eval list`

Lists persisted YAML runs newest first:

```
bernstein eval list
```

### `bernstein eval diff <run-a> <run-b>`

Per-adapter delta between two persisted runs. Useful for comparing
`claude-code` against `codex` on the same fixture:

```
bernstein eval diff \
  .sdd/eval/yaml_runs/yaml_run_..._claude.json \
  .sdd/eval/yaml_runs/yaml_run_..._codex.json
```

The diff JSON carries `overall_delta` and `golden_rate_delta` per adapter,
plus an overall `winner` ("a", "b", or "tie" inside the tolerance band).

## Worked example: claude-code vs codex on 20 prompts

```yaml
# eval/specs/claude-vs-codex.yaml
name: claude-vs-codex-smoke
lineage_tag: claude-vs-codex-smoke
adapters:
  - claude
  - codex
dataset: prompts.jsonl                # 20-prompt fixture
judge:
  model: anthropic/claude-sonnet-4
  rubric: "Score correctness (0-1)."
  weight: 0.4
thresholds:
  golden_pass_rate_min: 0.75
  overall_score_min: 0.75
prompts: []                           # all prompts live in the dataset
```

```
bernstein eval run eval/specs/claude-vs-codex.yaml
bernstein eval list
bernstein eval diff <previous>.json <current>.json
```

The persisted JSON has the same shape as the Pydantic dataclasses so it is
trivially loadable from a downstream notebook or CI gate.

## Lineage

Every run writes a lineage stub (`*.lineage.json`) next to its JSON output:

```json
{
  "artefact_path": "/.../yaml_run_....json",
  "content_hash": "sha256:...",
  "lineage_tag": "claude-vs-codex-smoke",
  "ts_ns": 1747707000123456789
}
```

The stub is intentionally minimal so it can be emitted from offline runs
(no HMAC, no signing material). When wired into `LineageRecorder`, the same
`artefact_path` + `content_hash` flow into the full lineage log entry.

## Python API

```python
from pathlib import Path
from bernstein.eval.yaml_runner import YAMLRunner, load_spec, save_report

spec = load_spec(Path("eval/specs/my-suite.yaml"))
runner = YAMLRunner()                      # default: deterministic mock
report = runner.run(spec, base_dir=Path("eval/specs"))
json_path, md_path = save_report(report, state_dir=Path(".sdd"))

assert report.thresholds_passed
```

Real callers inject a `PromptExecutor` that drives the adapter registry
(`bernstein.adapters.registry.AdapterRegistry`) and a `JudgeFn` backed by
`bernstein.eval.judge.EvalJudge`. Both interfaces are simple synchronous
callables so they can be swapped per-environment without touching the
runner.

## Files

- Module: `src/bernstein/eval/yaml_runner.py`
- Tests: `tests/unit/eval/test_yaml_runner.py`
- CLI: `src/bernstein/cli/commands/eval_benchmark_cmd.py` (`eval_group`)
