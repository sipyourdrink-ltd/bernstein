# Review pipeline DSL

`bernstein review --pipeline review.yaml` runs a multi-stage, multi-agent
code review against a PR (or, from orchestrator code, a completed task's
diff) driven entirely by a YAML file. It replaces the single-pass
cross-model verifier with an ordered sequence of stages, each running one
or more reviewer agents in parallel, whose votes are aggregated into a
stage verdict and finally a pipeline verdict.

The CLI handler lives in `cli/commands/review_pipeline_cmd.py`; the DSL,
runner, and aggregation logic live in `core/quality/review_pipeline/`
(`schema.py`, `runner.py`, `verdict.py`).

---

## Usage

```
bernstein review --pipeline review.yaml --validate-only
bernstein review --pipeline review.yaml --pr 42 --dry-run
bernstein review --pipeline templates/review/default-3-phase.yaml --pr 42
```

Flags (all part of the existing `bernstein review` command,
`cli/commands/task_cmd.py`):

- `--pipeline PATH` - path to a `review.yaml` pipeline definition. Without
  this flag, `bernstein review` falls back to its legacy behaviour: it
  writes a flag file that the running orchestrator picks up on its next
  tick, prompting the manager agent to inspect the task queue. That legacy
  path is unrelated to the DSL described here.
- `--pr N` - GitHub PR number to review. Required to actually run the
  pipeline (fetches the diff via `gh pr diff N`); omit it to only
  validate or dry-run.
- `--validate-only` - parse and schema-validate the YAML, print a summary,
  exit 0/1. No agents run, no PR is fetched.
- `--dry-run` - print the resolved pipeline (stages, parallelism,
  aggregator, agents) without spawning any agent or calling any LLM.
- `--workdir PATH` (default `.`) - repository root used to run `gh`.

Fetching the diff shells out to `gh pr view` and `gh pr diff`, so the
`gh` CLI must be installed and authenticated against the target repo.

---

## The YAML DSL

A pipeline is an ordered list of stages. Each stage runs N agents in
parallel; a stage's findings are forwarded into the next stage's prompt
context.

```yaml
version: 1
name: default-3-phase
pass_threshold: 0.66
block_on_fail: true

stages:
  - name: cheap-verifiers
    parallelism: 5
    aggregator:
      strategy: majority
    agents:
      - role: lint
        model: google/gemini-flash-1.5
        adapter: gemini
        prompt_template: lint.md
        effort: low
      - role: security
        model: anthropic/claude-haiku-4-5-20251001
        adapter: claude
        prompt_template: security.md
        effort: low

  - name: senior_synthesis
    parallelism: 1
    aggregator:
      strategy: any
    agents:
      - role: senior_reviewer
        model: anthropic/claude-opus-4-5-20250514
        adapter: claude
        prompt_template: senior_synthesis.md
        effort: high
```

A ready-to-use 3-stage pipeline (5 cheap verifiers → senior synthesis →
final gatekeeper) ships at `templates/review/default-3-phase.yaml`.

### Top-level fields (`ReviewPipeline`, `schema.py`)

| Field | Default | Notes |
|---|---|---|
| `version` | `1` | Schema version. |
| `name` | `null` | Optional; audit/docs only. |
| `pass_threshold` | `0.5` | Default pass fraction used by the `weighted` strategy when a stage doesn't override it. |
| `block_on_fail` | `true` | When true, a `request_changes` pipeline verdict blocks the janitor/merge gate, the same way the legacy cross-model verifier does. |
| `stages` | required, min 1 | Sequential - no diamond joins. Stage names must be unique. |

### Stage fields (`StageSpec`)

| Field | Default | Notes |
|---|---|---|
| `name` | required | Unique within the pipeline. |
| `parallelism` | `1` | Max concurrent agents in this stage (1-32); the runner caps it at `len(agents)`. |
| `agents` | required, min 1 | List of `AgentSpec`. |
| `aggregator` | `strategy: majority` | See below. |
| `description` | `null` | Free-text, audit/docs only. |

### Agent fields (`AgentSpec`)

| Field | Default | Notes |
|---|---|---|
| `role` | required | Free-form tag (`lint`, `security`, ...); also a key into `aggregator.weights`. |
| `model` | `null` | Model identifier; `null` lets the cost cascade router choose. |
| `adapter` | `null` | CLI adapter to spawn (`claude`, `gemini`, `codex`, ...). |
| `prompt_template` | `null` | Resolved against `templates/review/` then `templates/prompts/`. |
| `effort` | `low` | `low` / `medium` / `high`. |

The schema is `extra="forbid"` and `strict=True` - unknown fields or
type mismatches fail validation rather than being silently ignored.
`load_pipeline()` re-parses the YAML to attach the originating line
number to validation errors, so `--validate-only` output points at the
offending line rather than just a Pydantic path.

---

## Aggregation strategies

Set per stage via `aggregator.strategy` (`verdict.py:_apply_strategy`).
`pass_score` is always reported as the fraction of approve weight over
total weight, regardless of strategy:

| Strategy | Passes when |
|---|---|
| `any` | At least one agent approves. |
| `all` | Every agent approves. |
| `majority` | Strict majority approves (ties go to `request_changes`). |
| `weighted` | Approve-weighted score (`aggregator.weights`, keyed by role or model, default weight `1.0`) meets or exceeds the effective pass threshold (stage override, else pipeline `pass_threshold`, else `0.5`). |

A pipeline verdict is `request_changes` if any stage whose own strategy
is `all` fails, or if the overall weighted pass fraction across stages
falls below the pipeline's `pass_threshold` (`verdict.py:aggregate_pipeline`).
`should_block_merge()` then blocks the janitor merge gate exactly when the
pipeline verdict is `request_changes` and `block_on_fail` is true - the
same block/allow contract the single-pass cross-model verifier already
uses.

A single-stage, single-agent pipeline using `strategy: any` reproduces
today's legacy single-pass verifier output byte-for-byte
(`runner.py`, module docstring) - the DSL is a strict superset, not a
replacement behaviour.

---

## Prompt construction and stage context

- Stage 1's prompt for each agent reuses the existing cross-model
  verifier's prompt builder (`cross_model_verifier._build_prompt`), so a
  1-stage/1-agent pipeline sends the same prompt the legacy verifier
  always sent.
- From stage 2 onward, each agent's prompt is extended with a
  `## Prior stage findings` section summarising every prior stage's
  verdict, per-agent verdict, feedback, and issues
  (`runner.py:_format_prior_context`).
- The diff itself comes from `diff_from_pr()` (fetched via `gh pr diff`,
  truncated to the same character cap the cross-model verifier uses) or,
  when invoked from orchestrator code rather than the CLI, from
  `diff_from_task()` against a completed task's worktree.

---

## Limitations

- The CLI path (`bernstein review --pipeline ... --pr N`) does not wire
  an `AuditLog` into `run_pipeline_sync()` - stage-level HMAC-chained
  audit entries are only emitted when the pipeline is invoked
  programmatically with an `audit_log` argument (see `runner.py:run_pipeline`,
  the `audit_log` parameter). Running the DSL from the CLI does not by
  itself produce an audit trail beyond whatever the janitor's own gate
  recording does.
- There is no `bernstein review --pipeline ... --task` shortcut in the
  CLI today; `diff_from_task()` exists but is only reachable from
  orchestrator/janitor integration code, not from the `review` command
  itself.
- `--pr` is required to actually execute a pipeline; `--validate-only`
  and `--dry-run` never fetch a diff or contact GitHub.

---

## Source

- `src/bernstein/cli/commands/task_cmd.py` - `bernstein review` Click
  command and its `--pipeline` / `--pr` / `--validate-only` / `--dry-run`
  flags.
- `src/bernstein/cli/commands/review_pipeline_cmd.py` - CLI glue
  (`run_review_pipeline_cli`).
- `src/bernstein/core/quality/review_pipeline/schema.py` - the YAML DSL
  (`ReviewPipeline`, `StageSpec`, `AgentSpec`, `AggregatorConfig`,
  `load_pipeline`).
- `src/bernstein/core/quality/review_pipeline/runner.py` - stage
  execution, prompt assembly, diff sourcing.
- `src/bernstein/core/quality/review_pipeline/verdict.py` - aggregation
  strategies and pipeline-level verdict rollup.
- `templates/review/default-3-phase.yaml` - shipped example pipeline.

See also [Fresh-context review gate](../orchestration/review-gate.md) for
the single-stage gate contract (fresh session, distinct model, restricted
inputs) that one stage of a pipeline can enforce, and
[Quality pipeline](../architecture/quality-pipeline.md) for how the
janitor's structured-signal and gate layers fit around this review layer.
