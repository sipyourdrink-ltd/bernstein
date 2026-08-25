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
bernstein review --pipeline review.yaml --pr 42 --fix --until-checks-green --max-passes 3
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
- `--fix` - run a fix pass between review passes. See
  [the contour](#the-fix-until-green-contour).
- `--until-checks-green` - withhold approval while the PR's check rollup is
  not green.
- `--max-passes N` (default `3`) - review budget for the contour.
- `--fix-command CMD` - the command a fix pass runs; falls back to
  `$BERNSTEIN_REVIEW_FIX_COMMAND`.

Fetching the diff shells out to `gh pr view` and `gh pr diff`, and the check
rollup goes through the same `gh pr view` helper
(`runner.py:gh_pr_view_json`), so the `gh` CLI must be installed and
authenticated against the target repo. There is no second HTTP path.

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
| `rules` | `null` | Where the review ruleset lives: a path string, or a mapping with `path` / `raise` / `guard`. `null` falls back to `.bernstein/review-rules.md`. |

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

## The review ruleset

A review drifts when its standard lives only in a prompt. A repository can
declare that standard in `.bernstein/review-rules.md` (or wherever the
pipeline's `rules:` key points) with two kinds of entry:

- **raise rules** - a defect class the reviewer must flag;
- **guard rules** - a finding the reviewer must *not* raise, because an
  operator already rejected it as a false positive. Without them every
  unattended pass re-reports the same rejected finding and the fix pass
  chases it.

Bullets under a heading starting with `Raise` or `Guard` become rules;
everything else on the page is prose the parser ignores. A worked example
ships at `templates/review/review-rules.example.md`.

```markdown
## Raise

- A bare `except:` that swallows the traceback.

## Guard

- `assert` inside `tests/` is not a security finding.
```

The pipeline's `rules:` key accepts either form:

```yaml
rules: review/house-rules.md          # a path, resolved against the YAML's dir
```

```yaml
rules:                                 # or a mapping, which may also extend a file
  path: review/house-rules.md
  guard:
    - The vendored parser is exempt from the style rules.
```

`ReviewRuleset.digest` is a SHA-256 over the *canonical* rule set - sorted and
de-duplicated - so reordering the file leaves the digest alone while editing a
rule moves it. That digest goes into every review receipt, so a verdict names
the standard it was produced under, and into the pipeline's audit events.

**With no rules file the pipeline is unchanged.** The ruleset is empty, it
renders to no prompt section (the reviewer prompt is byte-identical to what
the pipeline sent before rulesets existed), and the digest is the stable
digest of the empty set. A `rules:` key that names a file which does not
exist is an error rather than a silent fallback - a typo there would review
against no standard at all.

---

## The fix-until-green contour

`--fix` / `--until-checks-green` turn the single verdict into a bounded loop
(`core/quality/review_pipeline/contour.py`):

1. wait, bounded, for the PR's check rollup to settle;
2. run the pipeline against the PR's current diff;
3. stop when the pipeline approves and - under `--until-checks-green` - the
   checks are green;
4. otherwise run a fix pass whose inputs are the verdict *and* the failing
   checks' log excerpts (fetched with the existing `gh run view --log-failed`
   wrapper), then start the next pass;
5. at `--max-passes`, stop with an explicit `needs-operator` outcome and a
   non-zero exit code - never an approval.

The fix pass is an operator-supplied command: `--fix-command` (or
`$BERNSTEIN_REVIEW_FIX_COMMAND`) is invoked with the rendered prompt's path as
its last argument, and counts as a pass only when it exits 0 *and* `HEAD`
moved. A command that changed nothing cannot change the next rollup, so the
contour stops rather than burning the rest of the budget. `--fix` without a
command reviews once and hands back to the operator; the contour never
approves by default.

### Per-pass receipts

Every pass emits a review receipt through the existing `review-receipt`
machinery, binding the reviewed diff hash, the ruleset digest, the pass index
and the verdict, signed with the install's Ed25519 identity and anchored in
the review lineage spine. Each pass records the previous pass's anchor, so the
passes form a chain:

```
bernstein review-receipt verify --chain --pr <url> \
    --issue issue.md --diff pr.diff --rules .bernstein/review-rules.md
```

`verify --chain` walks every pass offline: each one must recompute, carry the
previous pass's anchor, and name the same ruleset, and `--diff` is checked
against the last pass. Exit codes are the single-receipt ones - 0 verified,
1 no chain, 2 mismatch. Editing a receipt's stored diff hash or ruleset digest
breaks that pass's signature; presenting a different diff or a different rules
file breaks the comparison.

A single-pass receipt keeps the path and the binding it always had: the three
contour fields are omitted from the signed binding while unset, so receipts
emitted before the contour existed still verify.

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
  the `audit_log` parameter). The contour path is the exception: it emits a
  signed, spine-anchored receipt per pass and mirrors each into the audit
  chain.
- The contour does not merge and does not handle auth. The caller supplies an
  authenticated `gh`, and landing the PR stays a separate decision.
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
- `src/bernstein/core/quality/review_pipeline/ruleset.py` - the raise / guard
  rules, their canonical digest, and the prompt section they render to.
- `src/bernstein/core/quality/review_pipeline/contour.py` - the check rollup,
  the bounded wait, the fix pass, the loop, and the per-pass receipt emitter.
- `src/bernstein/core/review/receipt.py` - `verify_review_chain` and the
  per-pass receipt fields.
- `templates/review/default-3-phase.yaml` - shipped example pipeline.
- `templates/review/review-rules.example.md` - shipped example ruleset.

See also [Fresh-context review gate](../orchestration/review-gate.md) for
the single-stage gate contract (fresh session, distinct model, restricted
inputs) that one stage of a pipeline can enforce, and
[Quality pipeline](../architecture/quality-pipeline.md) for how the
janitor's structured-signal and gate layers fit around this review layer.
